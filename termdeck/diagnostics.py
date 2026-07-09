from __future__ import annotations

import importlib
import json
import platform
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .resource_budget import BudgetConfigError, assess_budgets, load_budgets
from .resources import ResourceSampler
from .session_store import DEFAULT_DB_PATH, SessionStore
from .settings import AppSettings, DEFAULT_CONFIG_PATH, SettingsError
from .task_snapshot import build_task_snapshot, task_snapshot_to_dicts
from .terminator_config import TerminatorConfig
from .tmux_backend import TmuxBackend, TmuxError
from .window_snapshot import build_window_snapshots, window_snapshots_to_dicts


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _command_status(name: str) -> dict[str, object]:
    path = shutil.which(name)
    return {"name": name, "available": bool(path), "path": path or ""}


def _gi_status(namespace: str, version: str) -> dict[str, object]:
    try:
        gi = importlib.import_module("gi")  # type: ignore[assignment]
        gi.require_version(namespace, version)  # type: ignore[attr-defined]
        importlib.import_module(f"gi.repository.{namespace}")
        return {"namespace": namespace, "version": version, "available": True, "error": ""}
    except Exception as exc:  # pragma: no cover - depends on host GUI packages
        return {"namespace": namespace, "version": version, "available": False, "error": str(exc)}


def _safe_path(path: Path | None) -> str:
    return str(path.expanduser()) if path else ""


def _session_counts(records) -> dict[str, object]:
    statuses: dict[str, int] = {}
    tags: dict[str, int] = {}
    windows: dict[str, int] = {}
    visible = 0
    external = 0
    for record in records:
        statuses[record.status] = statuses.get(record.status, 0) + 1
        tags[record.tag] = tags.get(record.tag, 0) + 1
        windows[record.window_title] = windows.get(record.window_title, 0) + 1
        visible += 1 if record.visible else 0
        external += 1 if getattr(record, "external", False) else 0
    return {
        "total": len(records),
        "visible": visible,
        "hidden": len(records) - visible,
        "external": external,
        "statuses": dict(sorted(statuses.items())),
        "tags": dict(sorted(tags.items())),
        "windows": dict(sorted(windows.items())),
    }


def build_diagnostic_payload(*, db_path: str | Path | None = None, config_path: str | Path | None = None) -> dict[str, object]:
    """Build a redaction-safe diagnostic payload for GitHub issues.

    The payload intentionally avoids environment variables and captured terminal
    output.  It reports structure, counts, resource summaries, dependency state,
    and configuration parsing status so maintainers can debug startup, restore,
    and resource-monitoring issues without receiving secrets.
    """

    db_resolved = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
    config_resolved = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    store = SessionStore(db_resolved)
    backend = TmuxBackend()
    sampler = ResourceSampler()
    settings_error = ""
    try:
        settings = AppSettings.load(config_resolved)
    except SettingsError as exc:
        settings_error = str(exc)
        settings = AppSettings(path=config_resolved)

    budget_error = ""
    try:
        budgets = load_budgets()
    except BudgetConfigError as exc:
        budget_error = str(exc)
        budgets = []

    try:
        records = store.list(include_archived=True)
        items = build_task_snapshot(records, backend=backend, sampler=sampler, policy_resolver=settings.policy_for_record)
        windows = build_window_snapshots(items)
        task_rows = task_snapshot_to_dicts(items)
        snapshots_by_id = {item.record.id: item.snapshot for item in items}
        budget_rows = [row.as_dict() for row in assess_budgets(records, snapshots_by_id, budgets)] if budgets else []
    finally:
        store.close()

    try:
        theme = TerminatorConfig.from_default_path().to_theme()
        terminator_status: dict[str, object] = {
            "available": bool(theme.source_path and theme.source_path.exists()),
            "source_path": _safe_path(theme.source_path),
            "profile": theme.profile_name,
            "font": theme.font,
            "scrollback_lines": theme.scrollback_lines,
            "window_width": theme.window_width,
            "window_height": theme.window_height,
        }
    except Exception as exc:  # pragma: no cover - defensive parser guard
        terminator_status = {"available": False, "error": str(exc)}

    tmux_sessions = []
    tmux_error = ""
    if backend.available:
        try:
            names = backend.list_session_names()
            tmux_sessions = names[:100]
        except (TmuxError, AttributeError) as exc:
            tmux_error = str(exc)

    return {
        "schema": "termdeck.diagnostics",
        "schema_version": 1,
        "generated_at": _utc_now(),
        "termdeck": {"version": __version__},
        "platform": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "dependencies": {
            "tmux": _command_status("tmux"),
            "gdk3": _gi_status("Gdk", "3.0"),
            "gtk3": _gi_status("Gtk", "3.0"),
            "vte": _gi_status("Vte", "2.91"),
        },
        "paths": {
            "session_db": _safe_path(db_resolved),
            "config": _safe_path(config_resolved),
            "default_config": _safe_path(DEFAULT_CONFIG_PATH),
        },
        "config": {
            "loaded": config_resolved.exists() and not settings_error,
            "error": settings_error,
            "budgets": settings.budget_summary(),
        },
        "terminator": terminator_status,
        "sessions": {
            "counts": _session_counts(records),
            "rows": [dict(record.__dict__) for record in records],
        },
        "tasks": task_rows,
        "windows": window_snapshots_to_dicts(windows),
        "budgets": {
            "error": budget_error,
            "rows": budget_rows,
        },
        "tmux": {
            "available": backend.available,
            "session_count_sampled": len(tmux_sessions),
            "session_names_sample": tmux_sessions,
            "error": tmux_error,
        },
        "privacy": {
            "captures_included": False,
            "environment_included": False,
            "tmux_names_included": True,
            "note": "diagnostics avoid environment variables and terminal captures by default",
        },
    }


def write_diagnostic_bundle(path: str | Path, *, db_path: str | Path | None = None, config_path: str | Path | None = None) -> dict[str, object]:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_diagnostic_payload(db_path=db_path, config_path=config_path)
    entries = {
        "diagnostics.json": payload,
        "sessions.json": payload["sessions"],
        "tasks.json": payload["tasks"],
        "windows.json": payload["windows"],
        "budgets.json": payload["budgets"],
        "README.txt": "TermDeck diagnostic bundle. It excludes environment variables and terminal captures by default.\n",
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            if isinstance(value, str):
                archive.writestr(name, value)
            else:
                archive.writestr(name, json.dumps(value, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "path": str(output), "entries": sorted(entries)}
