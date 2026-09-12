#!/usr/bin/env python3
"""Exercise a physical GTK account selection through a legacy managed tab."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from smoke_support import (
    install_fixture_launcher_attestation,
    require_private_validation_runtime,
    stream_ready,
)


def _drain_gtk(Gtk, *, until=None, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is None or until():
            return
        time.sleep(0.02)
    raise RuntimeError("GTK/auth state did not settle before timeout")


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _combo_rows(combo) -> list[tuple[str, str]]:
    model = combo.get_model()
    # GtkComboBoxText stores display text in column 0 and the explicit id in 1.
    return [(str(row[1]), str(row[0])) for row in model]


def _write_codex_state_db(
    path: Path,
    *,
    session_id: str,
    rollout: Path,
    cwd: Path,
) -> None:
    """Create the minimal current Codex state contract for exact-resume QA."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.executescript(
            """
            CREATE TABLE _sqlx_migrations (
                version BIGINT PRIMARY KEY,
                description TEXT NOT NULL,
                installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN NOT NULL,
                checksum BLOB NOT NULL,
                execution_time BIGINT NOT NULL
            );
            CREATE TABLE backfill_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL,
                last_watermark TEXT,
                last_success_at INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                cwd TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL
            );
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                input_schema TEXT NOT NULL,
                defer_loading INTEGER NOT NULL,
                namespace TEXT,
                PRIMARY KEY(thread_id, position),
                FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
            );
            CREATE TABLE thread_spawn_edges (id TEXT PRIMARY KEY);
            CREATE TABLE remote_control_enrollments (id TEXT PRIMARY KEY);
            CREATE TABLE external_agent_config_imports (id TEXT PRIMARY KEY);
            """
        )
        connection.executemany(
            """
            INSERT INTO _sqlx_migrations(
                version, description, success, checksum, execution_time
            ) VALUES (?, ?, 1, ?, ?)
            """,
            [
                (version, f"migration {version}", bytes([version]) * 48, version * 10)
                for version in range(1, 47)
            ],
        )
        connection.execute(
            """
            INSERT INTO backfill_state(
                id, status, last_watermark, last_success_at, updated_at
            ) VALUES (1, 'complete', 'sessions/last.jsonl', 1234, 1234)
            """
        )
        connection.execute(
            "INSERT INTO thread_sections(id, name) VALUES (?, 'Pinned')",
            ("01984de2-8f74-7c91-a3b2-5c5e937cf318",),
        )
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd) VALUES (?, ?, ?)",
            (session_id, str(rollout), str(cwd)),
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    runtime_base = require_private_validation_runtime()
    if os.environ.get("TMUX_TMPDIR") != str(runtime_base):
        raise RuntimeError("TMUX_TMPDIR is not the runner-attested validation runtime")
    with tempfile.TemporaryDirectory(prefix="termdeck-auth-selector-") as tmp:
        root = Path(tmp)
        home = root / "home"
        binaries = root / "bin"
        project = root / "project"
        for directory in (home, binaries, project):
            directory.mkdir()

        source_accounts = home / ".codex" / "accounts"
        source_accounts.mkdir(parents=True)
        for account in ("alpha", "beta"):
            (source_accounts / f"{account}.json").write_text(
                json.dumps({"fixture": account}) + "\n",
                encoding="utf-8",
            )
        fake_auth = binaries / "codex-auth"
        fake_auth.write_text(
            """#!/usr/bin/python3
import os
import sys
from pathlib import Path

if len(sys.argv) != 3 or sys.argv[1] != "use":
    raise SystemExit(64)
account = sys.argv[2]
home = Path(os.environ["CODEX_HOME"])
snapshot = home / "accounts" / f"{account}.json"
if not snapshot.is_file():
    raise SystemExit(2)
for marker in (home / "auth.json", home / "current"):
    marker.unlink(missing_ok=True)
(home / "auth.json").symlink_to(Path("accounts") / snapshot.name)
(home / "current").write_text(account + "\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        fake_auth.chmod(0o755)

        probe = project / ".codex-probe.json"
        fake_codex = binaries / "codex"
        fixture_source = binaries / "codex_fixture.c"
        fixture_source.write_text(
            r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested = 0;

static void stop_handler(int signal_number) {
    (void)signal_number;
    stop_requested = 1;
}

static int read_selected(const char *home, char *output, size_t output_size) {
    char path[PATH_MAX];
    FILE *handle;
    size_t length;
    if (snprintf(path, sizeof(path), "%s/current", home) >= (int)sizeof(path)) {
        return -1;
    }
    handle = fopen(path, "r");
    if (handle == NULL) {
        return -1;
    }
    if (fgets(output, (int)output_size, handle) == NULL) {
        fclose(handle);
        return -1;
    }
    fclose(handle);
    length = strlen(output);
    while (length > 0 && (output[length - 1] == '\n' || output[length - 1] == '\r')) {
        output[--length] = '\0';
    }
    return length > 0 ? 0 : -1;
}

int main(int argc, char **argv) {
    const char *home = getenv("CODEX_HOME");
    const char *scope = getenv("TERMDECK_AUTH_SCOPE_ID");
    const char *session = NULL;
    int probe_mode = 0;
    char selected[256] = {0};
    char auth_path[PATH_MAX];
    char cwd[PATH_MAX];
    struct stat auth_stat;
    int index;

    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "resume") == 0 && index + 1 < argc) {
            session = argv[index + 1];
            index++;
        } else if (strcmp(argv[index], "probe") == 0) {
            probe_mode = 1;
        }
    }
    if (home == NULL || scope == NULL || getcwd(cwd, sizeof(cwd)) == NULL) {
        return 70;
    }
    if (read_selected(home, selected, sizeof(selected)) != 0) {
        return 71;
    }
    if (snprintf(auth_path, sizeof(auth_path), "%s/auth.json", home) >= (int)sizeof(auth_path)) {
        return 72;
    }
    if (stat(auth_path, &auth_stat) != 0) {
        return 73;
    }

    if (probe_mode) {
        FILE *probe = fopen(".codex-probe.json", "w");
        if (probe == NULL) {
            return 74;
        }
        if (fprintf(
                probe,
                "{\"codex_home\":\"%s\",\"scope\":\"%s\",\"selected\":\"%s\"}",
                home,
                scope,
                selected
            ) < 0) {
            fclose(probe);
            return 75;
        }
        if (fclose(probe) != 0) {
            return 76;
        }
        return 0;
    }

    if (session == NULL) {
        return 64;
    }

    {
        FILE *generations = fopen(".live-codex-generations.jsonl", "a");
        if (generations == NULL) {
            return 77;
        }
        if (fprintf(
                generations,
                "{\"pid\":%ld,\"selected\":\"%s\",\"session\":\"%s\",\"credential_inode\":%llu}\n",
                (long)getpid(),
                selected,
                session,
                (unsigned long long)auth_stat.st_ino
            ) < 0) {
            fclose(generations);
            return 78;
        }
        if (fflush(generations) != 0 || fsync(fileno(generations)) != 0 || fclose(generations) != 0) {
            return 79;
        }
    }

    {
        time_t now = time(NULL);
        struct tm utc;
        char day[32];
        char rollout_path[PATH_MAX];
        int rollout_fd = -1;
        if (gmtime_r(&now, &utc) != NULL && strftime(day, sizeof(day), "%Y/%m/%d", &utc) > 0) {
            if (snprintf(
                    rollout_path,
                    sizeof(rollout_path),
                    "%s/sessions/%s/rollout-smoke-%s.jsonl",
                    home,
                    day,
                    session
                ) < (int)sizeof(rollout_path)) {
                rollout_fd = open(rollout_path, O_RDONLY | O_CLOEXEC);
            }
        }
        (void)rollout_fd;
    }

    signal(SIGTERM, stop_handler);
    signal(SIGINT, stop_handler);
    signal(SIGHUP, stop_handler);
    while (!stop_requested) {
        pause();
    }
    return 0;
}
''',
            encoding="utf-8",
        )
        compiled = subprocess.run(
            [
                "/usr/bin/cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                str(fake_codex),
                str(fixture_source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if compiled.returncode:
            raise AssertionError(
                "fake Codex fixture did not compile: "
                f"stdout={compiled.stdout!r}, stderr={compiled.stderr!r}"
            )
        fake_codex.chmod(0o755)
        install_fixture_launcher_attestation(fake_codex)

        live_session_id = "019f71b3-fa32-7251-bac3-c628f34dd154"
        # The private Codex wrapper confines writes to the target workspace, so
        # keep generation receipts inside the fixture project just as a real
        # running session would keep its writable artifacts inside its cwd.
        live_generations = project / ".live-codex-generations.jsonl"
        rollout_day = time.strftime("%Y/%m/%d", time.gmtime())
        canonical_rollout = (
            home
            / ".codex"
            / "sessions"
            / rollout_day
            / f"rollout-smoke-{live_session_id}.jsonl"
        )
        canonical_rollout.parent.mkdir(parents=True)
        canonical_rollout.write_text(
            json.dumps(
                {
                    "payload": {
                        "id": live_session_id,
                        "cwd": str(project.resolve()),
                        "source": "cli",
                        "timestamp": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _write_codex_state_db(
            home / ".codex" / "state_5.sqlite",
            session_id=live_session_id,
            rollout=canonical_rollout,
            cwd=project.resolve(),
        )
        source_before = _digest_tree(home / ".codex")

        os.environ["HOME"] = str(home)
        os.environ["NO_AT_BRIDGE"] = "1"
        os.environ["SHELL"] = "/bin/bash"
        os.environ["PATH"] = f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["TERMDECK_CODEX_AUTH_BINARY"] = str(fake_auth)

        from termdeck.codex_auth import CodexAuthManager
        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(db_path=root / "tabs.sqlite3")
        app.register(None)
        app.codex_auth = CodexAuthManager()
        record = app.store.create(
            "legacy-auth-shell",
            "terminal",
            "/bin/bash",
            project,
            window_id="auth-qa",
            window_title="Auth QA",
            visible=True,
        )
        # Cold legacy-scope migration has its own dedicated smoke. This selector
        # smoke starts from a verified private idle shell so it can focus on the
        # mapped dropdown transaction and the running-session exact-resume path.
        record = app.store.ensure_auth_scope(record.id)
        idle_alpha = app.codex_auth.switch(record, "alpha")
        if not idle_alpha.ok:
            raise AssertionError(f"could not prepare idle alpha account: {idle_alpha.error}")
        record = app.store.update(
            record.id,
            selected_account=idle_alpha.state.selected_account,
            pending_account=None,
            auth_restart_required=False,
        )
        app.backend.create_or_resume(record)
        record = app.store.update(
            record.id,
            status="running",
            visible=True,
            last_pid=app.backend.pane_pid(record),
        )

        # A second private tab runs an exact resumable Codex-shaped process.
        # Changing its dropdown must immediately respawn the same tmux pane and
        # resume the same UUID under the selected canonical account.
        live_record = app.store.create(
            "live-auth-codex",
            "terminal",
            "/bin/bash",
            project,
            window_id="auth-qa",
            window_title="Auth QA",
            visible=True,
        )
        live_record = app.store.ensure_auth_scope(live_record.id)
        alpha_switch = app.codex_auth.switch(live_record, "alpha")
        if not alpha_switch.ok:
            raise AssertionError(f"could not prepare alpha account: {alpha_switch.error}")
        live_record = app.store.update(
            live_record.id,
            selected_account=alpha_switch.state.selected_account,
            pending_account=None,
            auth_restart_required=False,
        )
        app.backend.create_or_resume(live_record)
        live_record = app.store.update(
            live_record.id,
            status="running",
            visible=True,
            last_pid=app.backend.pane_pid(live_record),
        )
        shell_deadline = time.monotonic() + 6.0
        while time.monotonic() < shell_deadline:
            pane_pid = app.backend.pane_pid(live_record)
            if pane_pid and app.backend._process_tree_is_idle_shell(pane_pid):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("live fixture shell did not become idle")
        live_command = (
            f"{shlex.quote(str(fake_codex))} resume {live_session_id}"
        )
        app.backend.send_text(
            live_record,
            live_command,
            enter=True,
        )
        deadline = time.monotonic() + 8.0
        while (
            (not live_generations.exists() or len(live_generations.read_text().splitlines()) < 1)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not live_generations.exists():
            pane_pid = app.backend.pane_pid(live_record)
            try:
                capture = app.backend.capture(live_record, lines=120)
            except Exception as exc:
                capture = f"capture error: {exc}"
            process_rows: list[dict[str, object]] = []
            if pane_pid:
                from termdeck.resources import process_tree

                for process_pid in process_tree(pane_pid):
                    try:
                        argv = Path(f"/proc/{process_pid}/cmdline").read_bytes().split(b"\0")
                        argv = [part.decode(errors="replace") for part in argv if part]
                    except OSError:
                        argv = []
                    try:
                        cwd = os.readlink(f"/proc/{process_pid}/cwd")
                    except OSError:
                        cwd = ""
                    process_rows.append({"pid": process_pid, "argv": argv, "cwd": cwd})
            raise AssertionError(
                "exact live Codex fixture did not start: "
                f"command={live_command!r}, pane_pid={pane_pid!r}, "
                f"processes={process_rows!r}, capture={capture!r}"
            )

        try:
            if not app.backend.private_auth_environment_ready(record):
                raise AssertionError("idle selector fixture did not start with private auth")
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: bool(app.windows)
                and record.id in app.windows[0].tabs
                and live_record.id in app.windows[0].tabs
                and stream_ready(app.windows[0].tabs[record.id].widget)
                and stream_ready(app.windows[0].tabs[live_record.id].widget),
            )
            window = app.windows[0]
            toast_messages: list[tuple[str, str]] = []
            original_show_toast = window.show_toast

            def _record_toast(message: str, *, kind: str = "info") -> None:
                toast_messages.append((kind, message))
                original_show_toast(message, kind=kind)

            window.show_toast = _record_toast
            window._focus_session(record.id)
            _drain_gtk(Gtk, until=lambda: window.current_session_id() == record.id)
            window._refresh_auth_selector()
            _drain_gtk(Gtk)
            combo = window._auth_combo
            rows = _combo_rows(combo)
            if (
                len(rows) != 3
                or rows[0][0] != "__none__"
                or rows[0][1]
                not in {"Codex: no account", "Codex: choose account · upgrades terminal"}
                or rows[1:] != [("alpha", "alpha"), ("beta", "beta")]
            ):
                raise AssertionError(f"unexpected selector rows: {rows}")
            if (
                not combo.get_sensitive()
                or combo.get_active_id() not in {"__none__", "alpha"}
            ):
                raise AssertionError(
                    "legacy account selector is not actionable: "
                    f"sensitive={combo.get_sensitive()!r}, active={combo.get_active_id()!r}"
                )

            # Select the mapped production ComboBox row. ``set_active_id``
            # emits the real GTK ``changed`` signal and exercises the complete
            # worker/backend/tab lifecycle without depending on a window
            # manager granting an XTest popup grab.
            combo.set_active_id("beta")
            _drain_gtk(
                Gtk,
                until=lambda: combo.get_active_id() == "beta"
                or bool(window._auth_switch_inflight),
                timeout=5.0,
            )
            try:
                _drain_gtk(
                    Gtk,
                    until=lambda: not window._auth_switch_inflight
                    and (app.store.get(record.id) or record).selected_account == "beta",
                    timeout=30.0,
                )
            except RuntimeError as exc:
                observed = app.store.get(record.id) or record
                raise RuntimeError(
                    f"{exc}; active={combo.get_active_id()!r}, "
                    f"selected={observed.selected_account!r}, "
                    f"pending={observed.pending_account!r}, "
                    f"inflight={sorted(window._auth_switch_inflight)!r}, "
                    f"sensitive={combo.get_sensitive()!r}, rows={_combo_rows(combo)!r}, "
                    f"toasts={toast_messages!r}"
                ) from exc

            updated = app.store.get(record.id)
            assert updated is not None
            if updated.pending_account is not None or updated.auth_restart_required:
                try:
                    environment_ready_after = app.backend.private_auth_environment_ready(updated)
                except Exception as exc:
                    environment_ready_after = f"error: {exc}"
                try:
                    capture_after = app.backend.capture(updated, lines=40)
                except Exception as exc:
                    capture_after = f"error: {exc}"
                raise AssertionError(
                    "verified account switch left pending/restart state: "
                    f"status={updated.status!r}, pid={updated.last_pid!r}, "
                    f"pending={updated.pending_account!r}, "
                    f"restart={updated.auth_restart_required!r}, "
                    f"environment_ready={environment_ready_after!r}, "
                    f"capture={capture_after!r}, toasts={toast_messages!r}"
                )
            if not app.backend.private_auth_environment_ready(updated):
                raise AssertionError("idle shell lost its private auth environment")
            _drain_gtk(
                Gtk,
                until=lambda: bool(
                    window.tabs.get(record.id) is not None
                    and stream_ready(window.tabs[record.id].widget)
                    and app.backend.session_exists(updated)
                ),
                timeout=15.0,
            )
            tab = window.tabs.get(record.id)
            if tab is None or not stream_ready(tab.widget):
                raise AssertionError("account switch removed or detached the selected tab")
            if not app.backend.session_exists(updated):
                raise AssertionError("account switch did not leave the selected tmux session live")
            if app.codex_auth.state(updated).selected_account_key != "beta":
                raise AssertionError("private account readback did not verify beta")
            if app.backend.current_directory(updated) != str(project.resolve()):
                raise AssertionError("idle account switch lost the live working directory")
            if _digest_tree(home / ".codex") != source_before:
                raise AssertionError("account switch mutated the source account catalogue")

            from termdeck.resources import (
                observe_recovery_metadata,
                process_tree_contains_codex,
            )

            def _live_generations() -> list[dict[str, object]]:
                if not live_generations.is_file():
                    return []
                return [
                    json.loads(line)
                    for line in live_generations.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

            window._focus_session(live_record.id)
            window._refresh_auth_selector()
            _drain_gtk(
                Gtk,
                until=lambda: window.current_session_id() == live_record.id
                and window._auth_combo.get_active_id() == "alpha",
            )
            combo = window._auth_combo
            live_before = app.store.get(live_record.id)
            assert live_before is not None
            initial_identity = app.backend._inspect_auth_pane(live_before)
            if initial_identity is None:
                raise AssertionError("live Codex pane identity is unavailable")
            initial_pane_pid = app.backend.pane_pid(live_before)
            observed, initial_recovery, initial_cwd = observe_recovery_metadata(
                initial_pane_pid or 0
            )
            if (
                not observed
                or initial_recovery != f"codex resume {live_session_id}"
                or initial_cwd != str(project.resolve())
            ):
                raise AssertionError(
                    "live fixture does not expose exact Codex recovery: "
                    f"observed={observed!r}, command={initial_recovery!r}, cwd={initial_cwd!r}"
                )
            initial_generations = _live_generations()
            if len(initial_generations) != 1:
                raise AssertionError(
                    f"expected one initial live generation, got {initial_generations!r}"
                )
            initial_generation = initial_generations[0]
            if initial_generation.get("selected") != "alpha":
                raise AssertionError(f"initial live generation used wrong account: {initial_generation!r}")
            if process_tree_contains_codex(initial_pane_pid or 0) is not True:
                raise AssertionError("live fixture is not observed as a running Codex process")

            # The mapped production ComboBox emits its real changed signal. The
            # running process must be replaced immediately in the same pane and
            # exact conversation, not queued for a future launch.
            combo.set_active_id("beta")

            def _live_beta_ready(*, minimum_generations: int) -> bool:
                current_live = app.store.get(live_record.id) or live_before
                generations = _live_generations()
                return bool(
                    not window._auth_switch_inflight
                    and current_live.selected_account == "beta"
                    and current_live.pending_account is None
                    and not current_live.auth_restart_required
                    and combo.get_active_id() == "beta"
                    and len(generations) >= minimum_generations
                    and generations[-1].get("selected") == "beta"
                    and generations[-1].get("session") == live_session_id
                )

            try:
                _drain_gtk(
                    Gtk,
                    until=lambda: _live_beta_ready(minimum_generations=2),
                    timeout=60.0,
                )
            except RuntimeError as exc:
                current_live = app.store.get(live_record.id) or live_before
                pane_pid = app.backend.pane_pid(current_live)
                try:
                    capture = app.backend.capture(current_live, lines=120)
                except Exception as capture_exc:
                    capture = f"capture error: {capture_exc}"
                process_rows: list[dict[str, object]] = []
                if pane_pid:
                    from termdeck.resources import process_tree

                    for process_pid in process_tree(pane_pid):
                        try:
                            argv = Path(f"/proc/{process_pid}/cmdline").read_bytes().split(b"\0")
                            argv = [part.decode(errors="replace") for part in argv if part]
                        except OSError:
                            argv = []
                        try:
                            cwd = os.readlink(f"/proc/{process_pid}/cwd")
                        except OSError:
                            cwd = ""
                        process_rows.append({"pid": process_pid, "argv": argv, "cwd": cwd})
                raise RuntimeError(
                    f"{exc}; selected={current_live.selected_account!r}, "
                    f"pending={current_live.pending_account!r}, "
                    f"restart={current_live.auth_restart_required!r}, "
                    f"active={combo.get_active_id()!r}, "
                    f"inflight={sorted(window._auth_switch_inflight)!r}, "
                    f"generations={_live_generations()!r}, "
                    f"processes={process_rows!r}, capture={capture!r}, "
                    f"toasts={toast_messages!r}"
                ) from exc
            live_after = app.store.get(live_record.id)
            assert live_after is not None
            after_identity = app.backend._inspect_auth_pane(live_after)
            if after_identity is None or after_identity.pane_id != initial_identity.pane_id:
                raise AssertionError("live account handoff changed the tmux pane identity")
            generations_after_switch = _live_generations()
            if generations_after_switch[-1].get("pid") == initial_generation.get("pid"):
                raise AssertionError("live account handoff did not replace the old Codex process")
            if generations_after_switch[-1].get("credential_inode") == initial_generation.get(
                "credential_inode"
            ):
                raise AssertionError("live account handoff retained the alpha credential")
            if process_tree_contains_codex(app.backend.pane_pid(live_after) or 0) is not True:
                raise AssertionError("live account handoff did not leave Codex running")
            if app.codex_auth.state(live_after).selected_account_key != "beta":
                raise AssertionError("live account handoff did not verify beta")
            if not app.backend.private_auth_environment_ready(live_after):
                raise AssertionError("live account handoff lost the private auth environment")

            forbidden_messages = (
                "queued",
                "next Codex launch",
                "could not pause",
                "did not stop completely",
                "did not inherit the private auth environment",
            )
            if any(
                token in message
                for _kind, message in toast_messages
                for token in forbidden_messages
            ):
                raise AssertionError(f"live handoff exposed a non-seamless status: {toast_messages!r}")

            # Change the canonical beta token outside T1000. Every private scope
            # points at this file, so the poller must detect the fingerprint and
            # perform the same exact same-pane handoff automatically.
            beta_account = source_accounts / "beta.json"
            external_replacement = source_accounts / ".beta.json.external"
            external_replacement.write_text(
                json.dumps({"fixture": "beta", "token_version": 2}) + "\n",
                encoding="utf-8",
            )
            os.replace(external_replacement, beta_account)
            expected_inode = os.stat(beta_account).st_ino
            generations_before_external = len(_live_generations())
            _drain_gtk(
                Gtk,
                until=lambda: bool(
                    _live_beta_ready(
                        minimum_generations=generations_before_external + 1
                    )
                    and _live_generations()[-1].get("credential_inode")
                    == expected_inode
                ),
                timeout=60.0,
            )
            live_external = app.store.get(live_record.id)
            assert live_external is not None
            external_identity = app.backend._inspect_auth_pane(live_external)
            if external_identity is None or external_identity.pane_id != initial_identity.pane_id:
                raise AssertionError("external token refresh changed the tmux pane identity")
            if app.codex_auth.state(live_external).selected_account_key != "beta":
                raise AssertionError("external token refresh changed the selected profile")
            if live_external.pending_account is not None or live_external.auth_restart_required:
                raise AssertionError("external token refresh left pending/manual-restart state")
            if process_tree_contains_codex(app.backend.pane_pid(live_external) or 0) is not True:
                raise AssertionError("external token refresh did not resume Codex")

            window._focus_session(record.id)
            _drain_gtk(Gtk, until=lambda: window.current_session_id() == record.id)
            app.backend.send_text(updated, "codex probe", enter=True)
            deadline = time.monotonic() + 30.0
            while not probe.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not probe.is_file():
                raise AssertionError("new Codex launch did not produce an environment probe")
            payload = json.loads(probe.read_text(encoding="utf-8"))
            if payload != {
                "codex_home": updated.codex_home,
                "scope": updated.auth_scope_id,
                "selected": "beta",
            }:
                raise AssertionError(f"new Codex launch saw wrong private auth: {payload}")

            print(
                json.dumps(
                    {
                        "ok": True,
                        "mapped_combo_selection": "beta",
                        "idle_private_terminal_switched": True,
                        "selected_tab_remained_open": True,
                        "cwd_preserved": str(project.resolve()),
                        "source_catalogue_unchanged": True,
                        "new_codex_private_scope_verified": True,
                        "running_codex_switched_immediately": True,
                        "exact_conversation_uuid_preserved": live_session_id,
                        "same_tmux_pane_preserved": initial_identity.pane_id,
                        "external_token_change_detected": True,
                        "external_token_applied_without_manual_restart": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            current = app.store.get(record.id) or record
            live_current = app.store.get(live_record.id) or live_record
            for cleanup_record in (current, live_current):
                try:
                    app.backend.terminate(cleanup_record)
                except Exception:
                    pass
            for top in list(Gtk.Window.list_toplevels()):
                try:
                    top.destroy()
                except Exception:
                    pass
            app.store.close()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
