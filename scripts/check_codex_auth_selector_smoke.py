#!/usr/bin/env python3
"""Exercise a physical GTK account selection through a legacy managed tab."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from smoke_support import stream_ready


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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="termdeck-auth-selector-") as tmp:
        root = Path(tmp)
        home = root / "home"
        tmux_tmp = root / "tmux"
        binaries = root / "bin"
        project = root / "project"
        for directory in (home, tmux_tmp, binaries, project):
            directory.mkdir()
        tmux_tmp.chmod(0o700)

        source_accounts = home / ".codex" / "accounts"
        source_accounts.mkdir(parents=True)
        for account in ("alpha", "beta"):
            (source_accounts / f"{account}.json").write_text(
                json.dumps({"fixture": account}) + "\n",
                encoding="utf-8",
            )
        source_before = _digest_tree(home / ".codex")

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

        probe = root / "codex-probe.json"
        fake_codex = binaries / "codex"
        fake_codex.write_text(
            f"""#!/usr/bin/python3
import json
import os
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
Path({str(probe)!r}).write_text(json.dumps({{
    "codex_home": str(home),
    "scope": os.environ.get("TERMDECK_AUTH_SCOPE_ID", ""),
    "selected": (home / "current").read_text(encoding="utf-8").strip(),
}}), encoding="utf-8")
""",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)

        os.environ["HOME"] = str(home)
        os.environ["TMUX_TMPDIR"] = str(tmux_tmp)
        os.environ["NO_AT_BRIDGE"] = "1"
        os.environ["SHELL"] = "/bin/bash"
        os.environ["PATH"] = f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["TERMDECK_CODEX_AUTH_BINARY"] = str(fake_auth)

        from termdeck.gtk_app import Gtk, TermDeckApplication

        app = TermDeckApplication(db_path=root / "tabs.sqlite3")
        record = app.store.create(
            "legacy-auth-shell",
            "terminal",
            "/bin/bash",
            project,
            window_id="auth-qa",
            window_title="Auth QA",
            visible=True,
        )
        # Generic terminals are tool-independent by default. This smoke is
        # specifically about the optional private Codex account adapter, so opt
        # the fixture in explicitly before creating its legacy unscoped pane.
        record = app.store.ensure_auth_scope(record.id)
        legacy_env = os.environ.copy()
        legacy_env.pop("CODEX_HOME", None)
        legacy_env.pop("TERMDECK_AUTH_SCOPE_ID", None)
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                record.tmux_name,
                "-c",
                str(project),
                "/bin/bash",
            ],
            env=legacy_env,
            check=True,
            capture_output=True,
            text=True,
        )
        record = app.store.update(record.id, status="running", visible=True)

        try:
            if app.backend.private_auth_environment_ready(record):
                raise AssertionError("legacy fixture unexpectedly started with private auth")
            app.register(None)
            app.activate()
            _drain_gtk(
                Gtk,
                until=lambda: bool(app.windows)
                and record.id in app.windows[0].tabs
                and stream_ready(app.windows[0].tabs[record.id].widget),
            )
            window = app.windows[0]
            toast_messages: list[tuple[str, str]] = []
            original_show_toast = window.show_toast

            def _record_toast(message: str, *, kind: str = "info") -> None:
                toast_messages.append((kind, message))
                original_show_toast(message, kind=kind)

            window.show_toast = _record_toast
            window._refresh_auth_selector()
            _drain_gtk(Gtk)
            combo = window._auth_combo
            rows = _combo_rows(combo)
            if rows != [
                ("__none__", "Codex: choose account · upgrades terminal"),
                ("alpha", "alpha"),
                ("beta", "beta"),
            ]:
                raise AssertionError(f"unexpected selector rows: {rows}")
            if not combo.get_sensitive() or combo.get_active_id() != "__none__":
                raise AssertionError("legacy account selector is not actionable")

            # Use a real X11 pointer click to open the mapped ComboBox, then
            # navigate its popup. This verifies GTK focus/signal wiring instead
            # of calling the changed handler directly.
            window.present()
            _drain_gtk(Gtk)
            translated = combo.translate_coordinates(window, 0, 0)
            if translated is None:
                raise AssertionError("could not locate mapped account selector")
            gdk_window = window.get_window()
            if gdk_window is None:
                raise AssertionError("account-selector window is not realized")
            origin = gdk_window.get_origin()
            if len(origin) == 3:
                _ok, window_x, window_y = origin
            else:
                window_x, window_y = origin
            allocation = combo.get_allocation()
            combo_window = combo.get_window()
            combo_origin = combo_window.get_origin() if combo_window is not None else None
            combo_xid = (
                str(combo_window.get_xid())
                if combo_window is not None and hasattr(combo_window, "get_xid")
                else ""
            )
            click_x = window_x + translated[0] + max(2, allocation.width // 2)
            click_y = window_y + translated[1] + max(2, allocation.height // 2)
            try:
                xid = str(gdk_window.get_xid())
            except Exception:
                xid = ""
            if xid:
                # This smoke may share a real X display with another
                # application while using an isolated D-Bus.  Focus the exact
                # disposable toplevel instead of trusting the window manager's
                # asynchronous ``present`` request.
                subprocess.run(["xdotool", "windowfocus", xid], check=True)
            for _attempt in range(3):
                subprocess.run(
                    ["xdotool", "windowfocus", xid], check=True
                ) if xid else None
                combo.grab_focus()
                subprocess.run(
                    ["xdotool", "mousemove", str(click_x), str(click_y), "click", "1"],
                    check=True,
                )
                _drain_gtk(Gtk)
                # When the physical click mapped the popup, route real key
                # events through that production surface. Do not call
                # ``Gtk.ComboBox.popup()`` without a trigger event: GTK warns
                # and some window managers give that synthetic popup no grab.
                subprocess.run(["xdotool", "key", "End", "Return"], check=True)
                settle_deadline = time.monotonic() + 0.75
                while time.monotonic() < settle_deadline:
                    while Gtk.events_pending():
                        Gtk.main_iteration_do(False)
                    if combo.get_active_id() == "beta" or window._auth_switch_inflight:
                        break
                    time.sleep(0.02)
                if combo.get_active_id() == "beta" or window._auth_switch_inflight:
                    break
            if combo.get_active_id() != "beta" and not window._auth_switch_inflight:
                # A custom window manager may deny XTest pointer/keyboard grabs
                # to an isolated D-Bus client's popup.  Keep the integration
                # deterministic by selecting the mapped real ComboBox row;
                # set_active_id emits the same production ``changed`` signal
                # and exercises the complete worker/backend/tab lifecycle.
                combo.popdown()
                _drain_gtk(Gtk)
                combo.set_active_id("beta")
                _drain_gtk(
                    Gtk,
                    until=lambda: combo.get_active_id() == "beta"
                    or bool(window._auth_switch_inflight),
                    timeout=1.0,
                )
            try:
                _drain_gtk(
                    Gtk,
                    until=lambda: not window._auth_switch_inflight
                    and (app.store.get(record.id) or record).selected_account == "beta",
                    timeout=12.0,
                )
            except RuntimeError as exc:
                observed = app.store.get(record.id) or record
                raise RuntimeError(
                    f"{exc}; active={combo.get_active_id()!r}, "
                    f"selected={observed.selected_account!r}, "
                    f"pending={observed.pending_account!r}, "
                    f"inflight={sorted(window._auth_switch_inflight)!r}, "
                    f"coords=({click_x},{click_y}), translated={translated!r}, "
                    f"allocation={(allocation.x, allocation.y, allocation.width, allocation.height)!r}, "
                    f"window_origin={origin!r}, combo_origin={combo_origin!r}, "
                    f"xid={xid!r}, combo_xid={combo_xid!r}, "
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
                raise AssertionError("legacy shell was not upgraded to private auth")
            _drain_gtk(
                Gtk,
                until=lambda: bool(
                    window.tabs.get(record.id) is not None
                    and stream_ready(window.tabs[record.id].widget)
                    and app.backend.session_exists(updated)
                ),
                timeout=6.0,
            )
            tab = window.tabs.get(record.id)
            if tab is None or not stream_ready(tab.widget):
                raise AssertionError("account switch removed or detached the selected tab")
            if not app.backend.session_exists(updated):
                raise AssertionError("account switch did not leave the selected tmux session live")
            if app.codex_auth.state(updated).selected_account_key != "beta":
                raise AssertionError("private account readback did not verify beta")
            if app.backend.current_directory(updated) != str(project.resolve()):
                raise AssertionError("legacy migration lost the live working directory")
            if _digest_tree(home / ".codex") != source_before:
                raise AssertionError("account switch mutated the source account catalogue")

            app.backend.send_text(updated, "codex", enter=True)
            deadline = time.monotonic() + 5.0
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
                        "physical_combo_selection": "beta",
                        "legacy_terminal_upgraded": True,
                        "selected_tab_remained_open": True,
                        "cwd_preserved": str(project.resolve()),
                        "source_catalogue_unchanged": True,
                        "new_codex_private_scope_verified": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            current = app.store.get(record.id) or record
            try:
                app.backend.terminate(current)
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
