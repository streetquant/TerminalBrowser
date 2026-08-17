from __future__ import annotations

import argparse
import json
import sqlite3
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .activity import activity_to_dicts, filter_activities, render_activity_table, scan_activities, sort_activities
from .codex_auth import CodexAuthError, CodexAuthManager
from .browser_snapshot import BrowserSnapshotError, browser_snapshot_json, import_browser_snapshot, load_snapshot, restore_plan
from .build_info import BuildIdentityError, release_status
from .command_palette import build_palette_items, search_palette, serialise_palette_items
from .capture_search import render_output_search_table, search_session_output
from .control_plan import build_control_plan
from .desktop_integration import build_install_plan, install_desktop, uninstall_desktop
from .diagnostics import build_diagnostic_payload, write_diagnostic_bundle
from .freeze_diagnostics import build_freeze_diagnostic_payload
from .auth_scope_gc import plan_auth_scope_gc
from .health import classify_session, health_badge, should_pause_for_attention
from .persistence import persist_live_sessions, persistence_candidates
from .quickstart import build_quickstart
from .redaction import redact_text
from .release_manager import (
    ReleaseError,
    build_release,
    install_release,
    managed_release_status,
    rollback_release,
    semantic_runtime_snapshot,
    verify_wheel,
)
from .resource_budget import BudgetConfigError, assess_budgets, default_budgets, load_budgets, render_budget_table, sample_budget_json
from .resources import ResourceSampler, format_bytes
from .session_lifecycle import archive_records, hide_records, lifecycle_payload, move_records, pin_records, recently_closed_records, rename_record, reopen_closed_records, retag_records, show_records, unpin_records
from .session_reconcile import reconcile_store
from .session_launcher import create_new_session, launch_payload, resolve_launch_fields
from .session_search import render_search_table, search_sessions
from .session_store import DEFAULT_DB_PATH, SessionStore, utc_now
from .settings import sample_settings_json
from .shutdown_service import (
    ShutdownServiceError,
    install_system_checkpoint_service,
    install_user_checkpoint_service,
    uninstall_system_checkpoint_service,
    uninstall_user_checkpoint_service,
)
from .shortcuts import build_shortcut_report, render_shortcut_table, shortcut_entries, shortcut_entries_to_dicts, shortcut_profile_names
from .task_snapshot import build_task_snapshot, filter_task_snapshot, render_task_table, sort_task_snapshot, task_snapshot_to_dicts
from .terminator_config import TerminatorConfig, load_terminator_theme
from .tmux_backend import (
    AuthTransactionBusy,
    TmuxBackend,
    TmuxError,
    auth_transaction_lock,
    execute_auth_change,
)
from .tmux_discovery import adopt_tmux_sessions, discover_tmux_sessions
from .window_snapshot import build_window_snapshots, filter_window_snapshots, render_window_table, sort_window_snapshots, window_snapshots_to_dicts
from .workspace_launcher import WorkspaceLaunchError, open_workspace_file
from .workspace_recipe import WorkspaceRecipeError, load_recipe, recipe_export_json, recipe_summary_json, sample_recipe_json


def _has_gi_namespace(namespace: str, version: str) -> bool:
    try:
        import gi  # type: ignore
        gi.require_version(namespace, version)
        __import__("gi.repository", fromlist=[namespace])
        return True
    except Exception:
        return False


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _select_records(
    store: SessionStore,
    selectors: list[str] | tuple[str, ...] | None = None,
    *,
    tag: str | None = None,
    all_sessions: bool = False,
    window_id: str | None = None,
):
    # Restrict the candidate set first. Otherwise an exact id, duplicate tab
    # name, shared tag, or --all can escape the requested browser window.
    records = store.list(include_archived=True, window_id=window_id)
    selected = []
    seen: set[str] = set()

    def add(record):
        if record.id not in seen:
            selected.append(record)
            seen.add(record.id)

    if all_sessions:
        for record in records:
            add(record)
    if tag:
        for record in records:
            if record.tag == tag:
                add(record)
    for selector in selectors or []:
        for record in records:
            if record.id == selector or record.id.startswith(selector) or record.name == selector or record.tmux_name == selector or record.tag == selector:
                add(record)
    return selected


def _print_control_result(action: str, rows: list[dict[str, object]]) -> None:
    _print_json({"action": action, "count": len(rows), "sessions": rows})


def _print_lifecycle_result(action: str, results) -> None:
    _print_json(lifecycle_payload(action, results))


def _target_records_from_args(store: SessionStore, args: argparse.Namespace):
    return _select_records(
        store,
        getattr(args, "selectors", None),
        tag=getattr(args, "tag", None),
        all_sessions=bool(getattr(args, "all", False)),
        window_id=getattr(args, "target_window_id", None),
    )


def _control_row(record, action: str, ok: bool, *, pid: int | None = None, detail: str = "") -> dict[str, object]:
    return {"id": record.id, "name": record.name, "tag": record.tag, "pinned": record.pinned, "action": action, "ok": ok, "pid": pid, "detail": detail}


def _split_pinned(records, *, include_pinned: bool):
    """Return actionable records plus protected pinned records.

    Pinned tabs are meant to feel like browser pinned tabs: visible, front-loaded,
    and protected from accidental cleanup or resource automation.  Explicit
    one-off pause/resume/send still works; destructive and automatic actions use
    this helper unless the caller opts in with ``--include-pinned``.
    """

    rows = list(records)
    if include_pinned:
        return rows, []
    protected = [record for record in rows if record.pinned]
    actionable = [record for record in rows if not record.pinned]
    return actionable, protected


def _live_pid(backend: TmuxBackend, record):
    pid = record.last_pid
    if backend.available:
        try:
            live_pid = backend.pane_pid(record)
        except TmuxError:
            live_pid = None
        if live_pid is not None:
            pid = live_pid
    return pid


def _plan_for_records(args: argparse.Namespace, action: str, records, *, text: str = "", enter: bool = False):
    backend = TmuxBackend()
    sampler = ResourceSampler()
    return build_control_plan(
        records,
        action,
        text=text,
        enter=enter,
        selectors=getattr(args, "selectors", None),
        tag=getattr(args, "tag", None),
        all_sessions=bool(getattr(args, "all", False)),
        pid_resolver=lambda record: _live_pid(backend, record),
        snapshot_resolver=sampler.snapshot,
    )


def _budget_assessments_from_args(args: argparse.Namespace):
    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    try:
        budgets = load_budgets(getattr(args, "path", None))
        records = store.list(
            include_archived=bool(getattr(args, "all", False)),
            visible_only=bool(getattr(args, "visible_only", False)),
            include_stopped=not bool(getattr(args, "running_only", False)),
        )
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        snapshots = {}
        for record in records:
            pid = _live_pid(backend, record)
            snapshots[record.id] = sampler.snapshot(pid)
        return assess_budgets(records, snapshots, budgets)
    finally:
        store.close()


def _should_only_plan(args: argparse.Namespace, plan) -> bool:
    return bool(getattr(args, "dry_run", False)) or (plan.requires_confirmation and not bool(getattr(args, "yes", False)))



def cmd_search(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    try:
        records = store.list(
            include_archived=bool(getattr(args, "all", False)),
            visible_only=bool(getattr(args, "visible_only", False)),
            include_stopped=not bool(getattr(args, "running_only", False)),
        )
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]

        def capture(record, lines: int) -> str:
            try:
                return backend.capture(record, lines=lines)
            except TmuxError as exc:
                return f"tmux capture unavailable: {exc}"

        result = search_sessions(
            records,
            args.query,
            include_output=bool(getattr(args, "include_output", False)),
            capture_resolver=capture if bool(getattr(args, "include_output", False)) else None,
            lines=int(getattr(args, "lines", 120)),
            redact=not bool(getattr(args, "raw", False)),
            max_results=int(getattr(args, "limit", 100)),
            max_snippets=int(getattr(args, "snippets", 3)),
        )
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(result.as_dict())
    else:
        print(render_search_table(result))
    return 0

def cmd_doctor(args: argparse.Namespace) -> int:
    term_config = TerminatorConfig.from_default_path()
    theme = term_config.to_theme()
    # Require Gdk 3.0 before Gtk so doctor does not leave Gdk 4.0 loaded on dual-stack systems.
    gdk_ok = _has_gi_namespace("Gdk", "3.0")
    gtk_ok = _has_gi_namespace("Gtk", "3.0")
    vte_ok = _has_gi_namespace("Vte", "2.91")
    gui_import = "ok"
    try:
        from . import gtk_app as _gtk_app  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment-specific
        gui_import = f"error: {exc}"
    checks = {
        "python": sys.version.split()[0],
        "tmux": shutil.which("tmux") or "missing",
        "gdk3": "ok" if gdk_ok else "missing",
        "gtk3": "ok" if gtk_ok else "missing",
        "vte": "ok" if vte_ok else "missing",
        "gui_import": gui_import,
        "terminator_config": str(theme.source_path) if theme.source_path and theme.source_path.exists() else "not found; using defaults",
        "session_db": str(DEFAULT_DB_PATH),
        "release": release_status(verify_artifact=False),
    }
    _print_json(checks)
    missing_runtime = (
        checks["tmux"] == "missing"
        or checks["gdk3"] == "missing"
        or checks["gtk3"] == "missing"
        or checks["vte"] == "missing"
        or checks["gui_import"] != "ok"
    )
    if missing_runtime:
        print("\nInstall on Debian/Ubuntu:")
        print("  sudo apt install tmux python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91")
        print("Install on Arch Linux:")
        print("  sudo pacman -S tmux python-gobject gtk3 vte3")
        return 1
    return 0



def cmd_diagnostics(args: argparse.Namespace) -> int:
    """Emit a redaction-safe diagnostic report or bundle for GitHub issues."""

    try:
        if getattr(args, "output", None):
            payload = write_diagnostic_bundle(args.output, db_path=args.db, config_path=getattr(args, "config", None))
        else:
            payload = build_diagnostic_payload(db_path=args.db, config_path=getattr(args, "config", None))
    except Exception as exc:
        _print_json({"ok": False, "action": "diagnostics", "error": str(exc)})
        return 1
    if isinstance(payload, dict):
        payload.setdefault("ok", True)
        payload.setdefault("action", "diagnostics")
    _print_json(payload)
    return 0


def _print_release_error(action: str, exc: Exception) -> int:
    _print_json({"ok": False, "action": action, "error": str(exc)})
    return 2


def cmd_release_info(args: argparse.Namespace) -> int:
    """Report the running build plus the selected managed-release pointer."""

    try:
        payload = {
            "ok": True,
            "action": "release-info",
            "runtime": release_status(
                verify_artifact=bool(getattr(args, "verify", False)),
                managed_root=getattr(args, "root", None),
            ),
            "managed": managed_release_status(
                root=getattr(args, "root", None),
                bin_dir=getattr(args, "bin_dir", None),
                verify=bool(getattr(args, "verify", False)),
            ),
        }
    except (BuildIdentityError, ReleaseError, OSError, ValueError) as exc:
        return _print_release_error("release-info", exc)
    payload["ok"] = bool(payload["runtime"].get("ok")) and bool(
        payload["managed"].get("ok")
    )
    _print_json(payload)
    return 0 if payload["ok"] else 2


def cmd_release_build(args: argparse.Namespace) -> int:
    try:
        payload = build_release(
            args.source,
            args.output,
            allow_dirty=bool(getattr(args, "allow_dirty", False)),
            python=getattr(args, "python", None),
            overwrite=bool(getattr(args, "overwrite", False)),
        )
    except (BuildIdentityError, ReleaseError, OSError, ValueError) as exc:
        return _print_release_error("release-build", exc)
    _print_json(payload)
    return 0


def cmd_release_verify(args: argparse.Namespace) -> int:
    try:
        payload = verify_wheel(args.wheel, manifest_path=args.manifest).as_dict()
    except (BuildIdentityError, ReleaseError, OSError, ValueError) as exc:
        return _print_release_error("release-verify", exc)
    payload.update({"ok": True, "action": "release-verify"})
    _print_json(payload)
    return 0


def cmd_release_install(args: argparse.Namespace) -> int:
    try:
        payload = install_release(
            args.wheel,
            args.manifest,
            root=getattr(args, "root", None),
            bin_dir=getattr(args, "bin_dir", None),
            python=getattr(args, "python", None),
            lock_timeout=float(getattr(args, "lock_timeout", 15.0)),
        )
    except (BuildIdentityError, ReleaseError, OSError, ValueError) as exc:
        return _print_release_error("release-install", exc)
    _print_json(payload)
    return 0


def cmd_release_rollback(args: argparse.Namespace) -> int:
    try:
        payload = rollback_release(
            root=getattr(args, "root", None),
            bin_dir=getattr(args, "bin_dir", None),
            release_id=getattr(args, "release_id", None),
            to_legacy=bool(getattr(args, "legacy", False)),
            lock_timeout=float(getattr(args, "lock_timeout", 15.0)),
        )
    except (BuildIdentityError, ReleaseError, OSError, ValueError) as exc:
        return _print_release_error("release-rollback", exc)
    _print_json(payload)
    return 0


def cmd_release_snapshot(args: argparse.Namespace) -> int:
    try:
        payload = semantic_runtime_snapshot(db_path=getattr(args, "db", None))
    except (ReleaseError, OSError, TmuxError, ValueError) as exc:
        return _print_release_error("release-snapshot", exc)
    payload.update({"ok": True, "action": "release-snapshot"})
    _print_json(payload)
    return 0


def cmd_diagnose_freeze(args: argparse.Namespace) -> int:
    """Emit one bounded, capture-free snapshot for terminal/Codex stalls."""

    try:
        payload = build_freeze_diagnostic_payload(
            db_path=args.db,
            auth_root=getattr(args, "auth_root", None),
        )
    except Exception as exc:
        _print_json({"ok": False, "action": "diagnose-freeze", "error": str(exc)})
        return 1
    payload.setdefault("ok", True)
    payload.setdefault("action", "diagnose-freeze")
    _print_json(payload)
    return 0


def cmd_auth_gc(args: argparse.Namespace) -> int:
    """Report orphan private Codex scopes without mutating their contents."""

    min_age_seconds = max(0.0, float(getattr(args, "min_age_hours", 24.0))) * 3600.0
    try:
        payload = plan_auth_scope_gc(
            db_path=args.db,
            auth_root=getattr(args, "auth_root", None),
            min_age_seconds=min_age_seconds,
        )
    except Exception as exc:
        _print_json({"ok": False, "action": "auth-gc", "error": str(exc)})
        return 1
    payload["mutation_available"] = False
    payload["detail"] = (
        "dry-run only: quarantine remains disabled until its isolated mutation "
        "and rollback path can be exercised end-to-end"
    )
    _print_json(payload)
    return 0


def cmd_install_desktop(args: argparse.Namespace) -> int:
    plan = build_install_plan(command=getattr(args, "command", None), data_home=getattr(args, "data_home", None))
    payload = install_desktop(plan, dry_run=bool(getattr(args, "dry_run", False)), update_database=bool(getattr(args, "update_database", False)))
    _print_json(payload)
    return 0


def cmd_uninstall_desktop(args: argparse.Namespace) -> int:
    payload = uninstall_desktop(data_home=getattr(args, "data_home", None), dry_run=bool(getattr(args, "dry_run", False)))
    _print_json(payload)
    return 0


def cmd_install_checkpoint_service(args: argparse.Namespace) -> int:
    try:
        if bool(getattr(args, "system", False)):
            user = getattr(args, "user", None)
            uid = getattr(args, "uid", None)
            home = getattr(args, "home", None)
            if user is None or uid is None or home is None:
                raise ShutdownServiceError("--system requires --user, --uid, and --home")
            result = install_system_checkpoint_service(
                executable=getattr(args, "executable", None),
                user=user,
                uid=uid,
                home=home,
                required_mounts=tuple(getattr(args, "required_mount", ()) or ()),
                unit_dir=getattr(args, "unit_dir", None),
                enable=not bool(getattr(args, "no_enable", False)),
            )
        else:
            result = install_user_checkpoint_service(
                executable=getattr(args, "executable", None),
                unit_dir=getattr(args, "unit_dir", None),
                enable=not bool(getattr(args, "no_enable", False)),
            )
    except ShutdownServiceError as exc:
        _print_json({"action": "install-checkpoint-service", "ok": False, "error": str(exc)})
        return 1
    payload = result.as_dict()
    payload["ok"] = True
    _print_json(payload)
    return 0


def cmd_uninstall_checkpoint_service(args: argparse.Namespace) -> int:
    try:
        if bool(getattr(args, "system", False)):
            result = uninstall_system_checkpoint_service(
                unit_dir=getattr(args, "unit_dir", None),
                disable=not bool(getattr(args, "no_disable", False)),
            )
        else:
            result = uninstall_user_checkpoint_service(
                unit_dir=getattr(args, "unit_dir", None),
                disable=not bool(getattr(args, "no_disable", False)),
            )
    except ShutdownServiceError as exc:
        _print_json({"action": "uninstall-checkpoint-service", "ok": False, "error": str(exc)})
        return 1
    payload = result.as_dict()
    payload["ok"] = True
    _print_json(payload)
    return 0


def cmd_parse_terminator(args: argparse.Namespace) -> int:
    config = TerminatorConfig.from_path(args.path) if args.path else TerminatorConfig.from_default_path()
    print(config.to_json(args.profile))
    return 0


def cmd_terminator_workspace(args: argparse.Namespace) -> int:
    config = TerminatorConfig.from_path(args.path) if args.path else TerminatorConfig.from_default_path()
    if getattr(args, "list_layouts", False):
        _print_json({
            "source_path": str(config.source_path) if config.source_path else None,
            "layouts": config.layout_names(),
        })
        return 0
    payload = config.to_workspace_json(getattr(args, "layout", None) or "default")
    output = getattr(args, "output", None)
    if output:
        Path(output).expanduser().write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def cmd_quickstart(args: argparse.Namespace) -> int:
    payload = build_quickstart(
        getattr(args, "path", None),
        terminator_path=getattr(args, "terminator_config", None),
        data_home=getattr(args, "data_home", None),
        desktop_command=getattr(args, "desktop_command", None),
    )
    _print_json(payload)
    return 0



def cmd_palette(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        rows = search_palette(args.query or "", build_palette_items(store.list(include_archived=args.all)), limit=getattr(args, "limit", 50))
        _print_json(serialise_palette_items(rows))
    finally:
        store.close()
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    try:
        records = store.list(include_archived=bool(getattr(args, "all", False)), visible_only=bool(getattr(args, "visible_only", False)), include_stopped=not bool(getattr(args, "running_only", False)))
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        items = build_task_snapshot(records, backend=backend, sampler=sampler)
        items = filter_task_snapshot(items, getattr(args, "query", "") or "")
        items = sort_task_snapshot(items, getattr(args, "sort", "health"))
        rows = task_snapshot_to_dicts(items)
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(rows)
    else:
        print(render_task_table(items))
    return 0



def cmd_windows(args: argparse.Namespace) -> int:
    """Show the browser-window overview without launching GTK/VTE."""

    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    try:
        records = store.list(
            include_archived=bool(getattr(args, "all", False)),
            visible_only=bool(getattr(args, "visible_only", False)),
            include_stopped=not bool(getattr(args, "running_only", False)),
        )
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        items = build_task_snapshot(records, backend=backend, sampler=sampler)
        windows = build_window_snapshots(items)
        windows = filter_window_snapshots(windows, getattr(args, "query", "") or "")
        windows = sort_window_snapshots(windows, getattr(args, "sort", "health"))
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(window_snapshots_to_dicts(windows))
    else:
        print(render_window_table(windows))
    return 0

def cmd_activity(args: argparse.Namespace) -> int:
    """Classify captured output from remembered sessions without launching GTK/VTE."""

    store = SessionStore(args.db)
    backend = TmuxBackend()
    try:
        records = store.list(
            include_archived=bool(getattr(args, "all", False)),
            visible_only=bool(getattr(args, "visible_only", False)),
            include_stopped=not bool(getattr(args, "running_only", False)),
        )
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        items = scan_activities(
            records,
            backend=backend,
            lines=getattr(args, "lines", 80),
            raw=bool(getattr(args, "raw", False)),
        )
        items = filter_activities(items, getattr(args, "query", "") or "")
        items = sort_activities(items, getattr(args, "sort", "severity"))
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(activity_to_dicts(items))
    else:
        print(render_activity_table(items))
    return 0


def cmd_search_output(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    try:
        records = _target_records_from_args(store, args)
        if not records:
            records = store.list(
                include_archived=bool(getattr(args, "all", False)),
                visible_only=bool(getattr(args, "visible_only", False)),
                include_stopped=not bool(getattr(args, "running_only", False)),
            )
        summary = search_session_output(
            records,
            getattr(args, "query", "") or "",
            backend=backend,
            lines=getattr(args, "lines", 500),
            context=getattr(args, "context", 1),
            per_session_limit=getattr(args, "limit", 20),
            redact=not bool(getattr(args, "raw", False)),
            include_empty=bool(getattr(args, "include_empty", False)),
            case_sensitive=bool(getattr(args, "case_sensitive", False)),
        )
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(summary.as_dict())
    else:
        print(render_output_search_table(summary))
    return 0 if summary.match_count or bool(getattr(args, "allow_empty", False)) else 1

def cmd_health(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    rows = []
    try:
        for record in store.list(include_archived=getattr(args, "all", False)):
            pid = _live_pid(backend, record)
            snap = sampler.snapshot(pid)
            health = classify_session(record, snap)
            rows.append({
                "id": record.id,
                "name": record.name,
                "tag": record.tag,
                "window": record.window_title,
                "status": record.status,
                "pid": pid,
                "health": health.label,
                "health_level": health.level,
                "health_badge": health_badge(health),
                "reason": health.reason,
                "suggested_action": health.suggested_action,
                "cpu_percent": round(snap.cpu_percent, 1) if snap.exists else 0.0,
                "memory": format_bytes(snap.memory_bytes) if snap.exists else "",
                "memory_bytes": snap.memory_bytes if snap.exists else 0,
                "process_count": snap.process_count if snap.exists else 0,
                "runtime": snap.runtime_label if snap.exists else "",
            })
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json(rows)
        return 0
    if not rows:
        print("No remembered sessions.")
        return 0
    for row in rows:
        print(f"{row['id']}  [{row['tag']}] {row['name']}  {row['health_badge']}  pid={row['pid'] or '-'}  cpu={row['cpu_percent']}%  mem={row['memory'] or '-'}  procs={row['process_count']}")
    return 0


def _run_targeted_control(args: argparse.Namespace, action: str) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    rows: list[dict[str, object]] = []
    try:
        records = _target_records_from_args(store, args)
        if not records:
            _print_control_result(action, rows)
            return 1
        if action == "kill":
            records, protected = _split_pinned(records, include_pinned=bool(getattr(args, "include_pinned", False)))
            rows.extend(
                _control_row(record, action, False, pid=record.last_pid, detail="pinned tab protected; rerun with --include-pinned to stop it")
                for record in protected
            )
            if not records:
                _print_control_result(action, rows)
                return 1
        plan = _plan_for_records(args, action, records)
        if _should_only_plan(args, plan):
            _print_json(plan.as_dict())
            return 0 if getattr(args, "dry_run", False) else 2
        for record in records:
            try:
                if action == "pause":
                    pid = backend.pause(record)
                    if pid is not None:
                        store.update(record.id, status="paused", last_pid=pid)
                        rows.append(_control_row(record, action, True, pid=pid, detail="paused process group"))
                    else:
                        rows.append(_control_row(record, action, False, detail="no live tmux pane found"))
                elif action == "resume":
                    pid = backend.resume(record)
                    if pid is not None:
                        store.update(record.id, status="running", last_pid=pid)
                        rows.append(_control_row(record, action, True, pid=pid, detail="resumed process group"))
                    else:
                        rows.append(_control_row(record, action, False, detail="no live tmux pane found"))
                elif action == "kill":
                    if record.pinned and not bool(getattr(args, "include_pinned", False)):
                        rows.append(_control_row(record, action, False, pid=record.last_pid, detail="pinned tab protected; pass --include-pinned to stop it"))
                        continue
                    backend.terminate(record)
                    store.clear_window_captain(record.window_id, session_id=record.id)
                    store.update(
                        record.id,
                        status="stopped",
                        visible=False,
                        recovery_command=None,
                        recovery_cwd=None,
                    )
                    rows.append(_control_row(record, action, True, pid=record.last_pid, detail="marked stopped"))
            except TmuxError as exc:
                rows.append(_control_row(record, action, False, pid=record.last_pid, detail=str(exc)))
    finally:
        store.close()
    _print_control_result(action, rows)
    return 0 if rows and all(bool(row["ok"]) for row in rows) else 1


def cmd_pause(args: argparse.Namespace) -> int:
    return _run_targeted_control(args, "pause")


def cmd_resume(args: argparse.Namespace) -> int:
    return _run_targeted_control(args, "resume")


def cmd_kill(args: argparse.Namespace) -> int:
    return _run_targeted_control(args, "kill")


def cmd_pause_hot(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    rows: list[dict[str, object]] = []
    try:
        records = []
        health_by_id = {}
        for record in store.list(include_archived=getattr(args, "all", False)):
            if record.pinned and not bool(getattr(args, "include_pinned", False)):
                continue
            pid = _live_pid(backend, record)
            health = classify_session(record, sampler.snapshot(pid))
            if should_pause_for_attention(health):
                records.append(record)
                health_by_id[record.id] = (pid, health)
        if not records:
            _print_control_result("pause-hot", rows)
            return 0
        plan = _plan_for_records(args, "pause-hot", records)
        if _should_only_plan(args, plan):
            _print_json(plan.as_dict())
            return 0 if getattr(args, "dry_run", False) else 2
        for record in records:
            pid, health = health_by_id[record.id]
            try:
                new_pid = backend.pause(record)
                if new_pid is not None:
                    store.update(record.id, status="paused", last_pid=new_pid)
                    rows.append(_control_row(record, "pause-hot", True, pid=new_pid, detail=health.hint))
                else:
                    rows.append(_control_row(record, "pause-hot", False, pid=pid, detail="no live tmux pane found"))
            except TmuxError as exc:
                rows.append(_control_row(record, "pause-hot", False, pid=pid, detail=str(exc)))
    finally:
        store.close()
    _print_control_result("pause-hot", rows)
    return 0 if rows and all(bool(row["ok"]) for row in rows) else 1


def cmd_send(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    rows: list[dict[str, object]] = []
    text = " ".join(args.text).strip()
    try:
        records = _target_records_from_args(store, args)
        if not records or not text:
            _print_control_result("send", rows)
            return 1
        enter = not getattr(args, "no_enter", False)
        plan = _plan_for_records(args, "send", records, text=text, enter=enter)
        if _should_only_plan(args, plan):
            _print_json(plan.as_dict())
            return 0 if getattr(args, "dry_run", False) else 2
        for record in records:
            try:
                backend.send_text(record, text, enter=enter)
                rows.append(_control_row(record, "send", True, pid=record.last_pid, detail="sent text"))
            except TmuxError as exc:
                rows.append(_control_row(record, "send", False, pid=record.last_pid, detail=str(exc)))
    finally:
        store.close()
    _print_control_result("send", rows)
    return 0 if rows and all(bool(row["ok"]) for row in rows) else 1


def cmd_capture(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    try:
        records = _target_records_from_args(store, args)
        if not records:
            return 1
        payload = []
        for record in records:
            try:
                text = backend.capture(record, lines=args.lines)
            except TmuxError as exc:
                text = f"tmux error: {exc}"
            redact_capture = not bool(getattr(args, "raw", False)) or bool(getattr(args, "redact", False))
            redacted = redact_text(text) if redact_capture else None
            payload.append({
                "id": record.id,
                "name": record.name,
                "tag": record.tag,
                "capture": redacted.text if redacted else text,
                "redacted": bool(redacted and redacted.replacements),
                "redaction_labels": list(redacted.labels) if redacted else [],
            })
    finally:
        store.close()
    _print_json(payload)
    return 0


def cmd_adopt_tmux(args: argparse.Namespace) -> int:
    """Adopt already-running tmux sessions into T1000's browser registry."""

    backend = TmuxBackend()
    if not backend.available:
        _print_json({
            "action": "adopt-tmux",
            "ok": False,
            "count": 0,
            "error": "tmux is not installed or not found on PATH",
            "sessions": [],
        })
        return 1

    try:
        discovered = discover_tmux_sessions(backend)
    except TmuxError as exc:
        _print_json({
            "action": "adopt-tmux",
            "ok": False,
            "count": 0,
            "error": str(exc),
            "sessions": [],
        })
        return 1

    if getattr(args, "list_only", False):
        rows = [session.to_dict() for session in discovered]
        selectors = set(getattr(args, "selectors", None) or [])
        if selectors:
            rows = [row for row in rows if row["session_name"] in selectors or row["session_id"] in selectors]
        _print_json({"action": "adopt-tmux", "ok": True, "count": len(rows), "dry_run": True, "sessions": rows})
        return 0

    store = SessionStore(args.db)
    try:
        results = adopt_tmux_sessions(
            store,
            discovered,
            selectors=getattr(args, "selectors", None) or [],
            tag=getattr(args, "tag", None) or "adopted",
            window_id=getattr(args, "window_id", None) or "adopted",
            window_title=getattr(args, "window_title", None) or "Adopted tmux",
            visible=not bool(getattr(args, "hidden", False)),
            include_termdeck=bool(getattr(args, "include_termdeck", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    finally:
        store.close()

    rows = [result.to_dict() for result in results]
    ok = all(result.ok for result in results)
    _print_json({
        "action": "adopt-tmux",
        "ok": ok,
        "count": len(rows),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "sessions": rows,
    })
    return 0 if ok else 1


def _run_lifecycle_targets(args: argparse.Namespace, action: str) -> int:
    store = SessionStore(args.db)
    try:
        records = _target_records_from_args(store, args)
        if not records:
            _print_lifecycle_result(action, [])
            return 1
        if action == "hide":
            results = hide_records(store, records, include_pinned=bool(getattr(args, "include_pinned", False)))
        elif action == "show":
            results = show_records(
                store,
                records,
                window_id=getattr(args, "window_id", None) or "main",
                window_title=getattr(args, "window_title", None) or getattr(args, "window_id", None) or "Main",
                tab_index=getattr(args, "tab_index", None),
            )
        elif action == "move":
            results = move_records(
                store,
                records,
                window_id=getattr(args, "window_id", None) or "main",
                window_title=getattr(args, "window_title", None) or getattr(args, "window_id", None) or "Main",
                tab_index=getattr(args, "tab_index", None),
                visible=not bool(getattr(args, "hidden", False)),
            )
        elif action == "archive":
            results = archive_records(store, records, include_pinned=bool(getattr(args, "include_pinned", False)))
        elif action == "pin":
            results = pin_records(store, records)
        elif action == "unpin":
            results = unpin_records(store, records)
        else:  # pragma: no cover - defensive guard for future actions
            raise ValueError(action)
    finally:
        store.close()
    _print_lifecycle_result(action, results)
    return 0 if results and all(row.ok for row in results) else 1


def cmd_hide(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "hide")


def cmd_show(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "show")


def cmd_move(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "move")


def cmd_archive(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "archive")


def cmd_pin(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "pin")


def cmd_unpin(args: argparse.Namespace) -> int:
    return _run_lifecycle_targets(args, "unpin")


def cmd_rename(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = _select_records(store, [args.selector])
        if len(records) != 1:
            _print_lifecycle_result("rename", [])
            return 1
        result = rename_record(store, records[0], args.name)
    finally:
        store.close()
    _print_lifecycle_result("rename", [result])
    return 0 if result.ok else 1


def cmd_retag(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = _target_records_from_args(store, args)
        if not records:
            _print_lifecycle_result("retag", [])
            return 1
        results = retag_records(store, records, args.new_tag)
    finally:
        store.close()
    _print_lifecycle_result("retag", results)
    return 0 if results and all(row.ok for row in results) else 1


def _recently_closed_row(record) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "tag": record.tag,
        "command": record.command,
        "cwd": record.cwd,
        "status": record.status,
        "pinned": record.pinned,
        "window_id": record.window_id,
        "window_title": record.window_title,
        "tab_index": record.tab_index,
        "last_opened_at": record.last_opened_at,
    }


def _render_recently_closed_table(records) -> str:
    rows = [_recently_closed_row(record) for record in records]
    if not rows:
        return "No recently closed tabs."
    headers = ["ID", "Window", "Name", "Tag", "Pin", "Status", "Closed"]
    values = [
        [
            str(row["id"]),
            str(row["window_title"]),
            str(row["name"]),
            str(row["tag"]),
            "★" if row["pinned"] else "-",
            str(row["status"]),
            str(row["last_opened_at"]),
        ]
        for row in rows
    ]
    widths = [len(header) for header in headers]
    for value_row in values:
        for index, value in enumerate(value_row):
            widths[index] = min(32, max(widths[index], len(value)))

    def clean(value: str, width: int) -> str:
        if len(value) > width:
            return value[: max(1, width - 1)] + "…"
        return value.ljust(width)

    lines = ["  ".join(clean(header, widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for value_row in values:
        lines.append("  ".join(clean(value, widths[index]) for index, value in enumerate(value_row)))
    return "\n".join(lines)


def cmd_recently_closed(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = recently_closed_records(store, limit=getattr(args, "limit", 20), tag=getattr(args, "tag", None))
        rows = [_recently_closed_row(record) for record in records]
    finally:
        store.close()
    if getattr(args, "json", False):
        _print_json({"action": "recently-closed", "count": len(rows), "sessions": rows})
    else:
        print(_render_recently_closed_table(records))
    return 0


def cmd_reopen_closed(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = recently_closed_records(store, limit=getattr(args, "limit", 1), tag=getattr(args, "tag", None))
        results = reopen_closed_records(
            store,
            records,
            window_id=getattr(args, "window_id", None),
            window_title=getattr(args, "window_title", None),
        )
    finally:
        store.close()
    _print_lifecycle_result("reopen-closed", results)
    return 0 if results and all(row.ok for row in results) else 1


def cmd_control_plan(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        if args.action == "pause-hot":
            backend = TmuxBackend()
            sampler = ResourceSampler()
            records = []
            for record in store.list(include_archived=getattr(args, "all", False)):
                if record.pinned and not bool(getattr(args, "include_pinned", False)):
                    continue
                pid = _live_pid(backend, record)
                if should_pause_for_attention(classify_session(record, sampler.snapshot(pid))):
                    records.append(record)
        else:
            records = _target_records_from_args(store, args)
        plan = build_control_plan(records, args.action, text=getattr(args, "text", "") or "", enter=not getattr(args, "no_enter", False) if args.action == "send" else False, selectors=getattr(args, "selectors", None), tag=getattr(args, "tag", None), all_sessions=bool(getattr(args, "all", False)))
    finally:
        store.close()
    _print_json(plan.as_dict())
    return 0


def _command_from_args(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "command_string", None) or getattr(args, "command_text", None)
    if explicit:
        return str(explicit).strip()
    parts = list(getattr(args, "command", []) or [])
    if parts and parts[0] == "--":
        parts = parts[1:]
    return " ".join(parts).strip() or None


def cmd_new(args: argparse.Namespace) -> int:
    try:
        fields = resolve_launch_fields(target=getattr(args, "target", None), command=_command_from_args(args), name=getattr(args, "name", None), tag=getattr(args, "tag", None), cwd=getattr(args, "cwd", None))
    except ValueError as exc:
        _print_json({"action": "new", "count": 0, "ok": False, "error": str(exc), "sessions": []})
        return 2
    store = SessionStore(args.db)
    backend = TmuxBackend()
    try:
        result = create_new_session(store, backend, fields, window_id=getattr(args, "window_id", None) or "main", window_title=getattr(args, "window_title", None) or "Main", tab_index=getattr(args, "tab_index", None), visible=not bool(getattr(args, "hidden", False)), pinned=bool(getattr(args, "pinned", False)), start=not bool(getattr(args, "no_start", False)))
    finally:
        store.close()
    _print_json(launch_payload("new", [result]))
    return 0 if result.ok else 1


def cmd_workspace_open(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        result = open_workspace_file(args.path, store, backend=TmuxBackend(), start=not bool(getattr(args, "no_start", False)), dry_run=bool(getattr(args, "dry_run", False)), reuse=not bool(getattr(args, "no_reuse", False)))
    except WorkspaceRecipeError as exc:
        _print_json({"action": "workspace-open", "count": 0, "ok": False, "error": str(exc), "sessions": []})
        return 2
    except WorkspaceLaunchError as exc:
        _print_json({"action": "workspace-open", "count": 0, "ok": False, "error": str(exc), "sessions": []})
        return 1
    finally:
        store.close()
    payload = result.to_dict()
    payload["action"] = "workspace-open"
    payload["ok"] = True
    _print_json(payload)
    return 0


def cmd_workspace_export(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = store.list(include_archived=bool(getattr(args, "all", False)), visible_only=bool(getattr(args, "visible_only", False)), include_stopped=not bool(getattr(args, "running_only", False)))
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        payload = recipe_export_json(records, name=getattr(args, "name", None) or "termdeck-export", visible_only=bool(getattr(args, "visible_only", False)))
    except WorkspaceRecipeError as exc:
        print(f"workspace export error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    output = getattr(args, "output", None)
    if output:
        Path(output).expanduser().write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def cmd_workspace_plan(args: argparse.Namespace) -> int:
    if args.sample:
        print(sample_recipe_json())
        return 0
    if not args.path:
        print("workspace-plan requires a recipe path unless --sample is used", file=sys.stderr)
        return 2
    try:
        recipe = load_recipe(args.path)
    except WorkspaceRecipeError as exc:
        print(f"workspace recipe error: {exc}", file=sys.stderr)
        return 2
    print(recipe_summary_json(recipe))
    return 0




def cmd_reconcile(args: argparse.Namespace) -> int:
    """Repair stale browser session state against the hidden backend."""

    store = SessionStore(args.db)
    try:
        result = reconcile_store(
            store,
            backend=TmuxBackend(),
            sampler=ResourceSampler(),
            dry_run=bool(getattr(args, "dry_run", False)),
            hide_missing=not bool(getattr(args, "keep_visible", False)),
            include_archived=bool(getattr(args, "all", False)),
            tag=getattr(args, "tag", None),
        )
    finally:
        store.close()
    _print_json(result.as_dict())
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    """Persist exact resumable state for selected live managed tabs."""

    store = SessionStore(args.db)
    try:
        targeted = bool(
            getattr(args, "selectors", None)
            or getattr(args, "tag", None)
        )
        if targeted:
            selected = _target_records_from_args(store, args)
            selected = [
                record
                for record in selected
                if record.status not in {"archived", "stopped"}
                and not getattr(record, "external", False)
            ]
        else:
            selected = persistence_candidates(store)
        result = persist_live_sessions(store, TmuxBackend(), records=selected)
    finally:
        store.close()
    _print_json(result.as_dict())
    return 0 if result.failed_count == 0 else 1


def cmd_restore_plan(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = store.list(include_archived=bool(getattr(args, "all", False)))
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        payload = restore_plan(records, limit=getattr(args, "limit", 50))
    finally:
        store.close()
    _print_json(payload)
    return 0


def cmd_snapshot_export(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        records = store.list(include_archived=bool(getattr(args, "all", False)))
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        payload = browser_snapshot_json(
            records,
            name=getattr(args, "name", None) or "termdeck-browser-snapshot",
            include_hidden=not bool(getattr(args, "visible_only", False)),
            include_stopped=not bool(getattr(args, "running_only", False)),
        )
    finally:
        store.close()
    output = getattr(args, "output", None)
    if output:
        Path(output).expanduser().write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def cmd_snapshot_import(args: argparse.Namespace) -> int:
    try:
        snapshot = load_snapshot(args.path)
    except BrowserSnapshotError as exc:
        _print_json({"action": "snapshot-import", "ok": False, "error": str(exc), "count": 0, "sessions": []})
        return 2
    store = SessionStore(args.db)
    try:
        result = import_browser_snapshot(
            store,
            snapshot,
            mode="replace" if bool(getattr(args, "replace", False)) else "append",
            dry_run=bool(getattr(args, "dry_run", False)),
            visible_only=bool(getattr(args, "visible_only", False)),
            preserve_window_ids=bool(getattr(args, "preserve_window_ids", False)),
            restart_stopped=bool(getattr(args, "restart_stopped", False)),
        )
    except BrowserSnapshotError as exc:
        _print_json({"action": "snapshot-import", "ok": False, "error": str(exc), "count": 0, "sessions": []})
        return 2
    finally:
        store.close()
    payload = result.to_dict()
    payload["action"] = "snapshot-import"
    payload["ok"] = True
    _print_json(payload)
    return 0

def cmd_budget_plan(args: argparse.Namespace) -> int:
    if getattr(args, "sample", False):
        print(sample_budget_json())
        return 0
    try:
        assessments = _budget_assessments_from_args(args)
    except BudgetConfigError as exc:
        _print_json({"action": "budget-plan", "ok": False, "error": str(exc), "sessions": []})
        return 2
    rows = [item.as_dict() for item in assessments if item.over_budget or not bool(getattr(args, "violations_only", False))]
    if getattr(args, "json", False):
        _print_json({"action": "budget-plan", "ok": True, "count": len(rows), "sessions": rows})
    else:
        print(render_budget_table(assessments, violations_only=bool(getattr(args, "violations_only", False))))
    return 0


def cmd_pause_over_budget(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    rows: list[dict[str, object]] = []
    try:
        try:
            budgets = load_budgets(getattr(args, "path", None))
        except BudgetConfigError as exc:
            _print_json({"action": "pause-over-budget", "count": 0, "ok": False, "error": str(exc), "sessions": []})
            return 2
        records = store.list(include_archived=bool(getattr(args, "all", False)), visible_only=bool(getattr(args, "visible_only", False)), include_stopped=not bool(getattr(args, "running_only", False)))
        if getattr(args, "tag", None):
            records = [record for record in records if record.tag == args.tag]
        snapshots = {}
        for record in records:
            pid = _live_pid(backend, record)
            snapshots[record.id] = sampler.snapshot(pid)
        assessments = assess_budgets(records, snapshots, budgets)
        actionable = [item for item in assessments if item.over_budget and item.suggested_action == "pause" and (not item.record.pinned or bool(getattr(args, "include_pinned", False)))]
        skipped_pinned = [item for item in assessments if item.over_budget and item.suggested_action == "pause" and item.record.pinned and not bool(getattr(args, "include_pinned", False))]
        for item in skipped_pinned:
            rows.append(_control_row(item.record, "pause-over-budget", False, pid=item.snapshot.pid, detail="pinned tab protected from budget pause; pass --include-pinned to override"))
        if not actionable:
            _print_control_result("pause-over-budget", rows)
            return 0 if not rows else 1
        target_records = [item.record for item in actionable]
        plan = build_control_plan(
            target_records,
            "pause-over-budget",
            selectors=[],
            tag=getattr(args, "tag", None),
            all_sessions=bool(getattr(args, "all", False)),
            pid_resolver=lambda record: _live_pid(backend, record),
            snapshot_resolver=lambda pid: sampler.snapshot(pid),
        )
        if _should_only_plan(args, plan):
            payload = plan.as_dict()
            payload["budget_reasons"] = {item.record.id: item.reason for item in actionable}
            _print_json(payload)
            return 0 if getattr(args, "dry_run", False) else 2
        reasons = {item.record.id: item.reason for item in actionable}
        for record in target_records:
            try:
                pid = backend.pause(record)
                if pid is not None:
                    store.update(record.id, status="paused", last_pid=pid)
                    rows.append(_control_row(record, "pause-over-budget", True, pid=pid, detail=reasons.get(record.id, "over budget")))
                else:
                    rows.append(_control_row(record, "pause-over-budget", False, pid=record.last_pid, detail="no live tmux pane found"))
            except TmuxError as exc:
                rows.append(_control_row(record, "pause-over-budget", False, pid=record.last_pid, detail=str(exc)))
    finally:
        store.close()
    _print_control_result("pause-over-budget", rows)
    return 0 if rows and all(bool(row["ok"]) for row in rows) else 1



def cmd_settings_sample(args: argparse.Namespace) -> int:
    print(sample_settings_json())
    return 0

def cmd_shortcuts(args: argparse.Namespace) -> int:
    theme = load_terminator_theme(args.path, getattr(args, "terminator_profile", "default"))
    profile = getattr(args, "profile", "browser")
    import_terminator = not getattr(args, "no_terminator", False)
    report = build_shortcut_report(
        theme.keybindings,
        profile=profile,
        import_terminator=import_terminator,
    )
    entries = shortcut_entries(theme.keybindings, profile=profile, import_terminator=import_terminator)
    report["action"] = "shortcuts"
    report["terminator_profile"] = theme.profile_name
    report["source_path"] = str(theme.source_path) if theme.source_path else None
    report["count"] = len(report.get("shortcuts", []))
    report["manifest_count"] = len(entries)
    report["manifest"] = shortcut_entries_to_dicts(entries)
    if getattr(args, "json", False):
        _print_json(report)
    else:
        print(render_shortcut_table(entries))
    return 0

def cmd_sessions(args: argparse.Namespace) -> int:
    store = SessionStore(args.db)
    try:
        _print_json(
            [
                record.__dict__
                for record in store.list(
                    include_archived=args.all,
                    window_id=getattr(args, "window_id", None),
                )
            ]
        )
    finally:
        store.close()
    return 0


def _auth_activation_updates(activation) -> dict[str, object]:
    """Translate an activation receipt into durable session metadata."""

    updates: dict[str, object] = {}
    command = getattr(activation, "command", None)
    cwd = getattr(activation, "cwd", None)
    if command is not None:
        updates["command"] = command
    if cwd is not None:
        updates["cwd"] = cwd
    if bool(getattr(activation, "clear_recovery", False)):
        updates["recovery_command"] = None
        updates["recovery_cwd"] = None
    else:
        recovery_command = getattr(activation, "recovery_command", None)
        if recovery_command is not None:
            updates["recovery_command"] = recovery_command
            updates["recovery_cwd"] = getattr(activation, "recovery_cwd", None)
    status = getattr(activation, "status", None)
    if status is not None:
        updates["status"] = status
        updates["last_pid"] = getattr(activation, "pid", None)
    return updates


def _persist_auth_update_with_retry(
    store: SessionStore,
    session_id: str,
    updates: dict[str, object],
):
    """Keep the scope lock until a transient durable-store failure clears.

    A successful credential/process transaction must not become visible to a
    second picker while SessionStore still describes the previous account.
    CLI callers therefore retain the flock and retry the small SQLite commit;
    Ctrl-C remains the explicit escape for a genuinely permanent storage
    failure.
    """

    delay = 0.05
    warned = False
    while True:
        try:
            return store.update(session_id, **updates)
        except (OSError, sqlite3.Error) as exc:
            if not warned:
                print(
                    "termdeck: could not save Codex account state; retrying while "
                    f"this terminal remains protected: {exc}",
                    file=sys.stderr,
                )
                warned = True
            time.sleep(delay)
            delay = min(1.0, delay * 2)


def cmd_auth(args: argparse.Namespace) -> int:
    """Inspect or switch the private Codex account for exactly one terminal."""

    store = SessionStore(args.db)
    try:
        records = _select_records(store, [args.selector])
        if len(records) != 1:
            _print_json(
                {
                    "action": "auth",
                    "ok": False,
                    "error": "selector must resolve to exactly one remembered terminal",
                    "count": len(records),
                }
            )
            return 2
        record = records[0]
        if record.external:
            _print_json(
                {
                    "action": "auth",
                    "ok": False,
                    "id": record.id,
                    "name": record.name,
                    "error": (
                        "adopted external tmux sessions cannot be retroactively isolated; "
                        "recreate this terminal under T1000 before switching Codex accounts"
                    ),
                }
            )
            return 2
        manager = CodexAuthManager(account_source=store.account_source)
        requested = (getattr(args, "switch_account", None) or "").strip()
        activation = None
        transaction_ok = True
        transaction_error = ""
        if requested:
            backend = TmuxBackend()
            try:
                with auth_transaction_lock(record):
                    previous_state = manager.state(record)
                    transaction = execute_auth_change(
                        backend,
                        manager,
                        record,
                        previous_state,
                        requested,
                    )
                    transaction_ok = transaction.ok
                    transaction_error = transaction.error
                    activation = transaction.activation
                    if transaction.switch_result is None:
                        # Planning/quiescence failed before credentials or
                        # durable session metadata changed. Preserve the whole
                        # prior record, including any pre-existing marker.
                        state = previous_state
                    else:
                        state = transaction.switch_result.state
                        updates: dict[str, object] = {
                            "selected_account": state.selected_account,
                            "pending_account": None,
                            "auth_restart_required": not (
                                activation is not None and activation.ok
                            ),
                            "auth_changed_at": utc_now(),
                        }
                        if activation is not None:
                            updates.update(_auth_activation_updates(activation))
                        record = _persist_auth_update_with_retry(store, record.id, updates)
            except AuthTransactionBusy as exc:
                state = manager.state(record)
                transaction_ok = False
                transaction_error = str(exc)
            action = "auth-switch"
        else:
            state = manager.state(record)
            if state.selected_account != record.selected_account:
                record = store.update(
                    record.id,
                    selected_account=state.selected_account,
                    pending_account=None if (state.selected_account or record.selected_account) else record.pending_account,
                    auth_restart_required=bool(record.auth_restart_required or state.selected_account != record.selected_account),
                    auth_changed_at=utc_now(),
                )
            action = "auth"
        payload = state.to_dict()
        payload.update(
            {
                "action": action,
                "ok": transaction_ok,
                "id": record.id,
                "name": record.name,
                "auth_scope_id": record.auth_scope_id,
                "pending_account": record.pending_account,
                "auth_restart_required": record.auth_restart_required,
                "auth_changed_at": record.auth_changed_at,
                "auth_activation_ok": activation.ok if activation is not None else None,
                "auth_activation_restarted": activation.restarted if activation is not None else None,
                "auth_activation_detail": activation.detail if activation is not None else None,
            }
        )
        if not transaction_ok:
            payload["error"] = transaction_error or (
                str(getattr(activation, "detail", ""))
                if activation is not None
                else "Codex account transaction failed"
            )
        _print_json(payload)
        return 0 if transaction_ok else 1
    except CodexAuthError as exc:
        _print_json({"action": "auth", "ok": False, "error": str(exc)})
        return 1
    finally:
        store.close()


def cmd_gui(args: argparse.Namespace) -> int:
    from .gtk_app import run
    return run(db_path=args.db, workspace_path=getattr(args, "workspace", None))


def _add_targeting_args(
    parser: argparse.ArgumentParser,
    *,
    include_window_id: bool = True,
) -> None:
    parser.add_argument("selectors", nargs="*", help="session id/prefix, exact name, tmux name, or tag")
    parser.add_argument("--tag", default=None, help="target all sessions under this tag")
    parser.add_argument("--all", action="store_true", help="target all remembered sessions")
    if include_window_id:
        parser.add_argument(
            "--window-id",
            dest="target_window_id",
            default=None,
            help="restrict targets to one T1000 window",
        )
    parser.add_argument("--db", type=Path, default=None)


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="optional T1000 JSON config path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="t1000", description="Named terminal windows with directory-backed tabs")
    parser.add_argument("--version", action="version", version=f"t1000 {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="check system dependencies")
    doctor.set_defaults(func=cmd_doctor)

    diagnostics = sub.add_parser("diagnostics", help="create a redaction-safe diagnostic report or ZIP bundle")
    diagnostics.add_argument("--db", type=Path, default=None)
    _add_config_arg(diagnostics)
    diagnostics.add_argument("--output", type=Path, default=None, help="write a ZIP diagnostic bundle instead of printing the full JSON report")
    diagnostics.set_defaults(func=cmd_diagnostics)

    release = sub.add_parser(
        "release",
        help="build, verify, install, inspect, or roll back immutable local releases",
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)

    release_info = release_sub.add_parser(
        "info",
        help="report the running build and managed-release pointer",
    )
    release_info.add_argument("--root", type=Path, default=None, help="override the managed release root")
    release_info.add_argument("--bin-dir", type=Path, default=None, help="override the stable launcher directory")
    release_info.add_argument("--verify", action="store_true", help="hash and verify the installed wheel/tree")
    release_info.set_defaults(func=cmd_release_info)

    release_build = release_sub.add_parser(
        "build",
        help="build twice offline and emit a reproducible wheel plus manifest",
    )
    release_build.add_argument("source", nargs="?", type=Path, default=Path("."))
    release_build.add_argument("--output", type=Path, required=True)
    release_build.add_argument("--python", type=Path, default=None)
    release_build.add_argument("--allow-dirty", action="store_true", help="label a non-final dirty-tree candidate explicitly")
    release_build.add_argument("--overwrite", action="store_true")
    release_build.set_defaults(func=cmd_release_build)

    release_verify = release_sub.add_parser(
        "verify",
        help="verify wheel paths, RECORD, bundled build identity, and manifest",
    )
    release_verify.add_argument("wheel", type=Path)
    release_verify.add_argument("manifest", type=Path)
    release_verify.set_defaults(func=cmd_release_verify)

    release_install = release_sub.add_parser(
        "install",
        help="atomically publish a verified content-addressed user release",
    )
    release_install.add_argument("wheel", type=Path)
    release_install.add_argument("manifest", type=Path)
    release_install.add_argument("--root", type=Path, default=None)
    release_install.add_argument("--bin-dir", type=Path, default=None)
    release_install.add_argument("--python", type=Path, default=None)
    release_install.add_argument("--lock-timeout", type=float, default=15.0)
    release_install.set_defaults(func=cmd_release_install)

    release_rollback = release_sub.add_parser(
        "rollback",
        help="atomically select the previous verified release or preserved launcher",
    )
    release_rollback.add_argument("--root", type=Path, default=None)
    release_rollback.add_argument("--bin-dir", type=Path, default=None)
    rollback_target = release_rollback.add_mutually_exclusive_group()
    rollback_target.add_argument("--release-id", default=None)
    rollback_target.add_argument("--legacy", action="store_true")
    release_rollback.add_argument("--lock-timeout", type=float, default=15.0)
    release_rollback.set_defaults(func=cmd_release_rollback)

    release_snapshot = release_sub.add_parser(
        "snapshot",
        help="hash persistent tmux/Codex session identity for non-destructive release gates",
    )
    release_snapshot.add_argument("--db", type=Path, default=None)
    release_snapshot.set_defaults(func=cmd_release_snapshot)

    diagnose_freeze = sub.add_parser(
        "diagnose-freeze",
        help="report bounded I/O pressure, blocked tasks, and Codex state footprint",
    )
    diagnose_freeze.add_argument("--db", type=Path, default=None)
    diagnose_freeze.add_argument("--auth-root", type=Path, default=None)
    diagnose_freeze.set_defaults(func=cmd_diagnose_freeze)

    auth_gc = sub.add_parser(
        "auth-gc",
        help="dry-run orphan private Codex scope analysis without modifying state",
    )
    auth_gc.add_argument("--db", type=Path, default=None)
    auth_gc.add_argument("--auth-root", type=Path, default=None)
    auth_gc.add_argument("--min-age-hours", type=float, default=24.0)
    auth_gc.set_defaults(func=cmd_auth_gc)

    settings_sample = sub.add_parser("settings-sample", help="print a sample T1000 settings JSON file")
    settings_sample.set_defaults(func=cmd_settings_sample)

    shortcuts = sub.add_parser("shortcuts", help="show browser, Terminator, tab, and terminal passthrough shortcuts")
    shortcuts.add_argument("--path", type=Path, default=None, help="optional Terminator config path")
    shortcuts.add_argument("--terminator-profile", default="default", help="Terminator profile name used for imported keybindings")
    shortcuts.add_argument("--profile", choices=shortcut_profile_names(), default="browser", help="T1000 shortcut profile")
    shortcuts.add_argument("--no-terminator", action="store_true", help="do not import Terminator keybinding overrides")
    shortcuts.add_argument("--json", action="store_true")
    shortcuts.set_defaults(func=cmd_shortcuts)

    quickstart = sub.add_parser("quickstart", help="show first-run setup, directory-tab defaults, Terminator import, and next actions")
    quickstart.add_argument("path", nargs="?", type=Path, default=Path.cwd(), help="directory to open as a tab")
    quickstart.add_argument("--terminator-config", type=Path, default=None, help="override Terminator config path")
    quickstart.add_argument("--data-home", type=Path, default=None, help="override XDG data home for launcher checks")
    quickstart.add_argument("--desktop-command", default=None, help="override command used in the suggested desktop launcher")
    quickstart.set_defaults(func=cmd_quickstart)

    install_desktop_parser = sub.add_parser("install-desktop", help="install a local desktop/app-menu launcher")
    install_desktop_parser.add_argument("--dry-run", action="store_true", help="show the files that would be written")
    install_desktop_parser.add_argument("--command", default=None, help="override the executable used in the desktop launcher")
    install_desktop_parser.add_argument("--data-home", type=Path, default=None, help="override XDG data home for testing or portable installs")
    install_desktop_parser.add_argument("--update-database", action="store_true", help="run update-desktop-database when available")
    install_desktop_parser.set_defaults(func=cmd_install_desktop)

    uninstall_desktop_parser = sub.add_parser("uninstall-desktop", help="remove the local desktop/app-menu launcher")
    uninstall_desktop_parser.add_argument("--dry-run", action="store_true")
    uninstall_desktop_parser.add_argument("--data-home", type=Path, default=None)
    uninstall_desktop_parser.set_defaults(func=cmd_uninstall_desktop)

    install_checkpoint_service = sub.add_parser(
        "install-checkpoint-service",
        help="install and enable the user shutdown checkpoint hook",
    )
    install_checkpoint_service.add_argument("--executable", type=Path, default=None)
    install_checkpoint_service.add_argument("--unit-dir", type=Path, default=None)
    install_checkpoint_service.add_argument("--system", action="store_true", help="install the strict system-level shutdown-ordered service")
    install_checkpoint_service.add_argument("--user", default=None, help="target user name for --system")
    install_checkpoint_service.add_argument("--uid", type=int, default=None, help="target numeric uid for --system")
    install_checkpoint_service.add_argument("--home", type=Path, default=None, help="target home directory for --system")
    install_checkpoint_service.add_argument("--required-mount", action="append", type=Path, default=[], help="mount path that must remain available during checkpointing")
    install_checkpoint_service.add_argument("--no-enable", action="store_true")
    install_checkpoint_service.set_defaults(func=cmd_install_checkpoint_service)

    uninstall_checkpoint_service = sub.add_parser(
        "uninstall-checkpoint-service",
        help="disable and remove the user shutdown checkpoint hook",
    )
    uninstall_checkpoint_service.add_argument("--unit-dir", type=Path, default=None)
    uninstall_checkpoint_service.add_argument("--system", action="store_true", help="remove the strict system-level service")
    uninstall_checkpoint_service.add_argument("--no-disable", action="store_true")
    uninstall_checkpoint_service.set_defaults(func=cmd_uninstall_checkpoint_service)

    parse = sub.add_parser("parse-terminator", help="print imported Terminator theme as JSON")
    parse.add_argument("--path", type=Path, default=None)
    parse.add_argument("--profile", default="default")
    parse.set_defaults(func=cmd_parse_terminator)

    terminator_workspace = sub.add_parser("terminator-workspace", help="convert a Terminator layout into a T1000 workspace recipe")
    terminator_workspace.add_argument("--path", type=Path, default=None, help="Terminator config path; defaults to ~/.config/terminator/config")
    terminator_workspace.add_argument("--layout", default="default", help="Terminator layout name")
    terminator_workspace.add_argument("--list-layouts", action="store_true", help="list available Terminator layout names")
    terminator_workspace.add_argument("--output", type=Path, default=None, help="write workspace JSON to this path")
    terminator_workspace.set_defaults(func=cmd_terminator_workspace)

    palette = sub.add_parser("palette", help="list command-palette actions")
    palette.add_argument("query", nargs="?", default="")
    palette.add_argument("--db", type=Path, default=None)
    palette.add_argument("--limit", type=int, default=50)
    palette.add_argument("--all", action="store_true")
    palette.set_defaults(func=cmd_palette)

    search = sub.add_parser("search", help="typo-tolerant search across tab metadata and optional recent output")
    search.add_argument("query", help="search text; all terms must match, with conservative typo tolerance")
    search.add_argument("--db", type=Path, default=None)
    search.add_argument("--tag", default=None)
    search.add_argument("--visible-only", action="store_true")
    search.add_argument("--running-only", action="store_true")
    search.add_argument("--all", action="store_true")
    search.add_argument("--include-output", action="store_true", help="also fuzzy-search recent terminal output via safe capture")
    search.add_argument("--lines", type=int, default=120)
    search.add_argument("--snippets", type=int, default=3)
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--raw", action="store_true", help="do not redact captured output before matching or printing snippets")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    new = sub.add_parser("new", help="create a directory-backed terminal tab from the CLI")
    new.add_argument("target", nargs="?", default=None, help="directory to open; defaults to the current directory")
    new.add_argument("command", nargs="*", help="optional command words; use --command for exact strings")
    new.add_argument("--command", dest="command_string", default=None, help="command string to run")
    new.add_argument("--name", default=None, help="optional tab name; defaults to the directory basename")
    new.add_argument("--cwd", type=Path, default=None, help="directory to open; overrides the positional target")
    new.add_argument("--window-id", default="main")
    new.add_argument("--window-title", default="Main")
    new.add_argument("--tab-index", type=int, default=None)
    new.add_argument("--hidden", action="store_true", help="remember the tab without showing it in the window")
    new.add_argument("--pinned", action="store_true", help="pin the tab so it stays visible and resists cleanup actions")
    new.add_argument("--no-start", action="store_true", help="remember the tab without starting its hidden process")
    new.add_argument("--db", type=Path, default=None)
    new.set_defaults(func=cmd_new)

    auth = sub.add_parser("auth", help="inspect or switch one terminal's isolated Codex account")
    auth.add_argument("selector", help="session id/prefix, exact name, or tmux name")
    auth.add_argument("--switch", dest="switch_account", default=None, help="saved account alias, email, or key")
    auth.add_argument("--db", type=Path, default=None)
    auth.set_defaults(func=cmd_auth)

    windows = sub.add_parser("windows", help="show aggregate browser-window resource usage without launching GTK")
    windows.add_argument("query", nargs="?", default="", help="filter by window id, title, health, or state summary")
    windows.add_argument("--db", type=Path, default=None)
    windows.add_argument("--tag", default=None)
    windows.add_argument("--visible-only", action="store_true")
    windows.add_argument("--running-only", action="store_true")
    windows.add_argument("--all", action="store_true")
    windows.add_argument("--json", action="store_true")
    windows.add_argument("--sort", choices=["health", "cpu", "ram", "tabs", "title", "running", "attention"], default="health")
    windows.set_defaults(func=cmd_windows)

    activity = sub.add_parser("activity", help="classify captured terminal output without launching GTK")
    activity.add_argument("query", nargs="?", default="", help="filter by id, tag, window, state, reason, action, or preview")
    activity.add_argument("--db", type=Path, default=None)
    activity.add_argument("--tag", default=None)
    activity.add_argument("--visible-only", action="store_true")
    activity.add_argument("--running-only", action="store_true")
    activity.add_argument("--all", action="store_true")
    activity.add_argument("--lines", type=int, default=80, help="captured lines per session")
    activity.add_argument("--raw", action="store_true", help="do not redact captured output previews")
    activity.add_argument("--json", action="store_true")
    activity.add_argument("--sort", choices=["severity", "state", "name", "tag", "window", "tab"], default="severity")
    activity.set_defaults(func=cmd_activity)

    tasks = sub.add_parser("tasks", help="show the Tab manager table without launching GTK")
    tasks.add_argument("query", nargs="?", default="", help="filter by id, tag, window, health, state, command, or directory")
    tasks.add_argument("--db", type=Path, default=None)
    tasks.add_argument("--tag", default=None)
    tasks.add_argument("--visible-only", action="store_true")
    tasks.add_argument("--running-only", action="store_true")
    tasks.add_argument("--all", action="store_true")
    tasks.add_argument("--json", action="store_true")
    tasks.add_argument("--sort", choices=["health", "cpu", "ram", "runtime", "name", "tag", "window", "tab", "status", "pinned"], default="health")
    tasks.set_defaults(func=cmd_tasks)

    search_output = sub.add_parser("search-output", help="search captured output across remembered terminal tabs")
    search_output.add_argument("query", help="literal terms to find in recent captured output")
    search_output.add_argument("--to", dest="selectors", action="append", default=[], help="session id/prefix, exact name, tmux name, or tag")
    search_output.add_argument("--tag", default=None)
    search_output.add_argument("--all", action="store_true")
    search_output.add_argument("--visible-only", action="store_true")
    search_output.add_argument("--running-only", action="store_true")
    search_output.add_argument("--lines", type=int, default=500)
    search_output.add_argument("--context", type=int, default=1)
    search_output.add_argument("--limit", type=int, default=20, help="maximum matches per session")
    search_output.add_argument("--include-empty", action="store_true", help="include searched sessions even when they had no matches")
    search_output.add_argument("--case-sensitive", action="store_true")
    search_output.add_argument("--raw", action="store_true", help="show unredacted matching output")
    search_output.add_argument("--json", action="store_true")
    search_output.add_argument("--allow-empty", action="store_true", help="exit 0 even when no match is found")
    search_output.add_argument("--db", type=Path, default=None)
    search_output.set_defaults(func=cmd_search_output)

    health = sub.add_parser("health", help="show browser task-manager health for remembered sessions")
    health.add_argument("--db", type=Path, default=None)
    health.add_argument("--all", action="store_true")
    health.add_argument("--json", action="store_true")
    health.set_defaults(func=cmd_health)

    for command_name, handler, help_text in [("pause", cmd_pause, "pause selected remembered terminal sessions"), ("resume", cmd_resume, "resume selected remembered terminal sessions"), ("kill", cmd_kill, "stop selected sessions and hide their tabs")]:
        control = sub.add_parser(command_name, help=help_text)
        _add_targeting_args(control)
        control.add_argument("--dry-run", action="store_true")
        control.add_argument("--yes", action="store_true")
        if command_name == "kill":
            control.add_argument("--include-pinned", action="store_true", help="also stop pinned/protected tabs")
        control.set_defaults(func=handler)

    pause_hot = sub.add_parser("pause-hot", help="pause sessions over critical resource thresholds")
    pause_hot.add_argument("--db", type=Path, default=None)
    pause_hot.add_argument("--all", action="store_true")
    pause_hot.add_argument("--dry-run", action="store_true")
    pause_hot.add_argument("--yes", action="store_true")
    pause_hot.add_argument("--include-pinned", action="store_true", help="also pause pinned/protected hot sessions")
    pause_hot.set_defaults(func=cmd_pause_hot)

    send = sub.add_parser("send", help="send text to selected sessions without exposing tmux")
    send.add_argument("text", nargs="+", help="text to send")
    send.add_argument("--to", dest="selectors", action="append", default=[])
    send.add_argument("--tag", default=None)
    send.add_argument("--all", action="store_true")
    send.add_argument("--window-id", dest="target_window_id", default=None)
    send.add_argument("--no-enter", action="store_true")
    send.add_argument("--dry-run", action="store_true")
    send.add_argument("--yes", action="store_true")
    send.add_argument("--db", type=Path, default=None)
    send.set_defaults(func=cmd_send)

    control_plan = sub.add_parser("control-plan", help="preview selected-session controls before execution")
    control_plan.add_argument("action", choices=["pause", "resume", "kill", "send", "pause-hot", "capture"])
    _add_targeting_args(control_plan)
    control_plan.add_argument("--text", default="")
    control_plan.add_argument("--no-enter", action="store_true")
    control_plan.set_defaults(func=cmd_control_plan)

    capture = sub.add_parser("capture", help="capture recent output from selected sessions")
    _add_targeting_args(capture)
    capture.add_argument("--lines", type=int, default=120)
    capture.add_argument("--redact", action="store_true", help="redact secrets from captures; kept for compatibility and enabled by default")
    capture.add_argument("--raw", action="store_true", help="print captures without redacting secret-like values")
    capture.set_defaults(func=cmd_capture)

    hide = sub.add_parser("hide", help="hide selected tabs from browser restore without killing their processes")
    _add_targeting_args(hide)
    hide.add_argument("--include-pinned", action="store_true", help="also hide pinned/protected tabs")
    hide.set_defaults(func=cmd_hide)

    show = sub.add_parser("show", help="make selected hidden/remembered sessions visible in a browser window")
    _add_targeting_args(show, include_window_id=False)
    show.add_argument("--window-id", default="main")
    show.add_argument("--window-title", default="Main")
    show.add_argument("--tab-index", type=int, default=None)
    show.set_defaults(func=cmd_show)

    recently_closed = sub.add_parser("recently-closed", help="show hidden tabs that can be reopened like browser tabs")
    recently_closed.add_argument("--db", type=Path, default=None)
    recently_closed.add_argument("--tag", default=None)
    recently_closed.add_argument("--limit", type=int, default=20)
    recently_closed.add_argument("--json", action="store_true")
    recently_closed.set_defaults(func=cmd_recently_closed)

    reopen_closed = sub.add_parser("reopen-closed", help="reopen the most recently hidden browser tab without restarting it")
    reopen_closed.add_argument("--db", type=Path, default=None)
    reopen_closed.add_argument("--tag", default=None)
    reopen_closed.add_argument("--limit", type=int, default=1)
    reopen_closed.add_argument("--window-id", default=None)
    reopen_closed.add_argument("--window-title", default=None)
    reopen_closed.set_defaults(func=cmd_reopen_closed)

    move = sub.add_parser("move", help="move selected remembered tabs to another browser window/tab slot")
    _add_targeting_args(move, include_window_id=False)
    move.add_argument("--window-id", required=True)
    move.add_argument("--window-title", default=None)
    move.add_argument("--tab-index", type=int, default=None)
    move.add_argument("--hidden", action="store_true", help="move but keep hidden from browser restore")
    move.set_defaults(func=cmd_move)

    rename = sub.add_parser("rename", help="rename one remembered browser tab without touching the process")
    rename.add_argument("selector", help="session id/prefix, exact name, or tmux name")
    rename.add_argument("name", help="new tab name")
    rename.add_argument("--db", type=Path, default=None)
    rename.set_defaults(func=cmd_rename)

    retag = sub.add_parser("retag", help="move selected sessions to another workspace/tag")
    retag.add_argument("new_tag", help="new workspace/tag name")
    _add_targeting_args(retag)
    retag.set_defaults(func=cmd_retag)

    archive = sub.add_parser("archive", help="archive selected sessions from normal views without killing their processes")
    _add_targeting_args(archive)
    archive.add_argument("--include-pinned", action="store_true", help="also archive pinned/protected tabs")
    archive.set_defaults(func=cmd_archive)

    pin = sub.add_parser("pin", help="pin selected browser tabs so they stay visible/front-loaded and resist cleanup")
    _add_targeting_args(pin)
    pin.set_defaults(func=cmd_pin)

    unpin = sub.add_parser("unpin", help="remove pinned protection from selected browser tabs")
    _add_targeting_args(unpin)
    unpin.set_defaults(func=cmd_unpin)

    adopt = sub.add_parser("adopt-tmux", help="adopt existing tmux sessions as browser-like T1000 tabs")
    adopt.add_argument("selectors", nargs="*", help="optional tmux session names or session ids to adopt")
    adopt.add_argument("--tag", default="adopted", help="T1000 tag for adopted sessions")
    adopt.add_argument("--window-id", default="adopted", help="browser window id used for the adopted tabs")
    adopt.add_argument("--window-title", default="Adopted tmux", help="browser window title used for the adopted tabs")
    adopt.add_argument("--hidden", action="store_true", help="adopt sessions but keep them out of visible restore")
    adopt.add_argument("--include-termdeck", action="store_true", help="also adopt tmux sessions that already look T1000-managed")
    adopt.add_argument("--dry-run", action="store_true", help="show what would be adopted without writing the session database")
    adopt.add_argument("--list", dest="list_only", action="store_true", help="list discoverable tmux sessions without adopting")
    adopt.add_argument("--db", type=Path, default=None)
    adopt.set_defaults(func=cmd_adopt_tmux)


    reconcile = sub.add_parser("reconcile", help="repair stale remembered tabs before browser-style restore")
    reconcile.add_argument("--db", type=Path, default=None)
    reconcile.add_argument("--tag", default=None)
    reconcile.add_argument("--all", action="store_true", help="include archived sessions in the report")
    reconcile.add_argument("--dry-run", action="store_true", help="show registry repairs without writing them")
    reconcile.add_argument("--keep-visible", action="store_true", help="do not hide tabs whose backend session has disappeared")
    reconcile.set_defaults(func=cmd_reconcile)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="persist exact Codex session IDs and checkpoint other live process trees",
    )
    _add_targeting_args(checkpoint)
    checkpoint.set_defaults(func=cmd_checkpoint)

    restore = sub.add_parser("restore-plan", help="show the browser windows/tabs that would reopen")
    restore.add_argument("--db", type=Path, default=None)
    restore.add_argument("--tag", default=None)
    restore.add_argument("--all", action="store_true")
    restore.add_argument("--limit", type=int, default=50)
    restore.set_defaults(func=cmd_restore_plan)

    snapshot_export = sub.add_parser("snapshot-export", help="export a local browser-session backup with window/tab state")
    snapshot_export.add_argument("--db", type=Path, default=None)
    snapshot_export.add_argument("--name", default="termdeck-browser-snapshot")
    snapshot_export.add_argument("--tag", default=None)
    snapshot_export.add_argument("--visible-only", action="store_true")
    snapshot_export.add_argument("--running-only", action="store_true")
    snapshot_export.add_argument("--all", action="store_true")
    snapshot_export.add_argument("--output", type=Path, default=None)
    snapshot_export.set_defaults(func=cmd_snapshot_export)

    snapshot_import = sub.add_parser("snapshot-import", help="restore a local browser-session backup without starting processes")
    snapshot_import.add_argument("path", type=Path)
    snapshot_import.add_argument("--db", type=Path, default=None)
    snapshot_import.add_argument("--replace", action="store_true", help="archive existing remembered sessions before import")
    snapshot_import.add_argument("--dry-run", action="store_true")
    snapshot_import.add_argument("--visible-only", action="store_true")
    snapshot_import.add_argument("--preserve-window-ids", action="store_true")
    snapshot_import.add_argument("--restart-stopped", action="store_true", help="restore stopped tabs as new, not stopped")
    snapshot_import.set_defaults(func=cmd_snapshot_import)

    budget_plan = sub.add_parser("budget-plan", help="preview configurable per-tag resource budgets")
    budget_plan.add_argument("path", nargs="?", type=Path, default=None, help="budget JSON file; defaults to ~/.config/termdeck/budgets.json or built-ins")
    budget_plan.add_argument("--sample", action="store_true", help="print a sample budget file")
    budget_plan.add_argument("--db", type=Path, default=None)
    budget_plan.add_argument("--tag", default=None)
    budget_plan.add_argument("--visible-only", action="store_true")
    budget_plan.add_argument("--running-only", action="store_true")
    budget_plan.add_argument("--all", action="store_true")
    budget_plan.add_argument("--violations-only", action="store_true")
    budget_plan.add_argument("--json", action="store_true")
    budget_plan.set_defaults(func=cmd_budget_plan)

    pause_budget = sub.add_parser("pause-over-budget", help="pause sessions whose matching budget action is pause")
    pause_budget.add_argument("path", nargs="?", type=Path, default=None, help="budget JSON file")
    pause_budget.add_argument("--db", type=Path, default=None)
    pause_budget.add_argument("--tag", default=None)
    pause_budget.add_argument("--visible-only", action="store_true")
    pause_budget.add_argument("--running-only", action="store_true")
    pause_budget.add_argument("--all", action="store_true")
    pause_budget.add_argument("--dry-run", action="store_true")
    pause_budget.add_argument("--yes", action="store_true")
    pause_budget.add_argument("--include-pinned", action="store_true", help="also pause pinned/protected sessions when a budget requests it")
    pause_budget.set_defaults(func=cmd_pause_over_budget)

    sessions = sub.add_parser("sessions", help="list remembered sessions")
    sessions.add_argument("--db", type=Path, default=None)
    sessions.add_argument("--all", action="store_true")
    sessions.add_argument("--window-id", default=None, help="list sessions from one T1000 window")
    sessions.set_defaults(func=cmd_sessions)

    workspace_open = sub.add_parser("workspace-open", help="materialise a JSON workspace recipe into remembered sessions")
    workspace_open.add_argument("path", type=Path)
    workspace_open.add_argument("--no-start", action="store_true")
    workspace_open.add_argument("--dry-run", action="store_true")
    workspace_open.add_argument("--no-reuse", action="store_true")
    workspace_open.add_argument("--db", type=Path, default=None)
    workspace_open.set_defaults(func=cmd_workspace_open)

    workspace_export = sub.add_parser("workspace-export", help="export remembered sessions as a JSON workspace recipe")
    workspace_export.add_argument("--db", type=Path, default=None)
    workspace_export.add_argument("--name", default="termdeck-export")
    workspace_export.add_argument("--tag", default=None)
    workspace_export.add_argument("--visible-only", action="store_true")
    workspace_export.add_argument("--running-only", action="store_true")
    workspace_export.add_argument("--all", action="store_true")
    workspace_export.add_argument("--output", type=Path, default=None)
    workspace_export.set_defaults(func=cmd_workspace_export)

    workspace = sub.add_parser("workspace-plan", help="validate and preview a JSON workspace recipe")
    workspace.add_argument("path", nargs="?", type=Path, default=None)
    workspace.add_argument("--sample", action="store_true")
    workspace.set_defaults(func=cmd_workspace_plan)

    gui = sub.add_parser("gui", help="launch the GUI")
    gui.add_argument("--db", type=Path, default=None)
    gui.add_argument("--workspace", type=Path, default=None)
    gui.set_defaults(func=cmd_gui)

    parser.add_argument("--workspace", type=Path, default=None, help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_gui, db=None, workspace=None)
    return parser


def _adopt_process_name(name: str = "t1000") -> None:
    """Expose one launch/kill identity so `pkill t1000` and WM_CLASS match."""

    try:
        from gi.repository import GLib

        GLib.set_prgname(name)
    except Exception:
        pass
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(15, name.encode(), 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    invoked_as = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if invoked_as == "termdeck":
        print(
            "This app is now t1000. Launch and kill it by that name only.",
            file=sys.stderr,
        )
        return 2
    _adopt_process_name()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
