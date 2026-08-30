import argparse
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from termdeck.cli import _persist_auth_update_with_retry, _prepare_managed_session_database, _select_records, build_parser, cmd_activity, cmd_archive, cmd_auth, cmd_auth_gc, cmd_budget_plan, cmd_capture, cmd_checkpoint, cmd_control_plan, cmd_diagnose_freeze, cmd_diagnostics, cmd_gui, cmd_health, cmd_hide, cmd_kill, cmd_move, cmd_new, cmd_palette, cmd_pause_over_budget, cmd_pin, cmd_reconcile, cmd_recovery_intent, cmd_recently_closed, cmd_rename, cmd_reopen_closed, cmd_retag, cmd_search, cmd_send, cmd_sessions, cmd_settings_sample, cmd_shortcuts, cmd_show, cmd_tasks, cmd_terminator_workspace, cmd_unpin, cmd_windows, cmd_workspace_export, cmd_workspace_open, cmd_workspace_plan


class CliTests(unittest.TestCase):
    def test_managed_cli_uses_legacy_registry_until_primary_gui_cutover(self):
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed_root = root / "managed" / "t1000-managed"
            legacy = root / "legacy" / "sessions.sqlite3"
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            args = argparse.Namespace(db=None, func=cmd_sessions)
            with mock.patch.dict(
                "os.environ",
                {"T1000_MANAGED_ROOT": str(managed_root)},
                clear=False,
            ), mock.patch("termdeck.cli.legacy_state_dir", return_value=legacy.parent):
                _prepare_managed_session_database(args)
            self.assertEqual(args.db, legacy)

    def test_managed_primary_gui_defers_database_cutover_to_startup(self):
        from unittest import mock

        args = argparse.Namespace(db=None, func=cmd_gui)
        with mock.patch.dict(
            "os.environ",
            {"T1000_MANAGED_ROOT": "/ssd/t1000"},
            clear=False,
        ):
            _prepare_managed_session_database(args)
        self.assertIsNone(args.db)

    def test_explicit_database_never_enters_managed_cutover(self):
        explicit = pathlib.Path("/tmp/explicit.sqlite3")
        args = argparse.Namespace(db=explicit, func=cmd_sessions)
        _prepare_managed_session_database(args)
        self.assertEqual(args.db, explicit)

    def test_auth_store_retry_keeps_retrying_until_commit(self):
        import sqlite3
        from unittest import mock

        saved = object()
        store = mock.Mock()
        store.update.side_effect = [sqlite3.OperationalError("busy"), saved]
        with mock.patch("termdeck.cli.time.sleep") as sleep, contextlib.redirect_stderr(
            io.StringIO()
        ) as errors:
            result = _persist_auth_update_with_retry(store, "session", {"selected_account": "work"})

        self.assertIs(result, saved)
        self.assertEqual(store.update.call_count, 2)
        sleep.assert_called_once_with(0.05)
        self.assertIn("remains protected", errors.getvalue())

    def test_parser_has_single_subcommand_registry_without_templates(self):
        parser = build_parser()
        subparsers = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        self.assertEqual(len(subparsers), 1)
        choices = sorted(subparsers[0].choices)
        self.assertEqual(choices, ['activity', 'adopt-tmux', 'archive', 'auth', 'auth-gc', 'budget-plan', 'capture', 'checkpoint', 'control-plan', 'diagnose-freeze', 'diagnostics', 'doctor', 'gui', 'health', 'hide', 'install-checkpoint-service', 'install-desktop', 'kill', 'move', 'new', 'palette', 'parse-terminator', 'pause', 'pause-hot', 'pause-over-budget', 'pin', 'quickstart', 'recently-closed', 'reconcile', 'recovery-intent', 'recovery-qualification', 'release', 'rename', 'reopen-closed', 'restore-plan', 'resume', 'retag', 'search', 'search-output', 'send', 'sessions', 'settings-sample', 'shortcuts', 'show', 'snapshot-export', 'snapshot-import', 'tasks', 'terminator-workspace', 'uninstall-checkpoint-service', 'uninstall-desktop', 'unpin', 'windows', 'workspace-export', 'workspace-open', 'workspace-plan'])


    def test_shortcuts_command_outputs_json_with_terminator_override(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "terminator.conf"
            cfg.write_text("""[keybindings]
  new_tab = <Alt>t
[profiles]
  [[default]]
    font = Monospace 12
""", encoding="utf-8")
            args = argparse.Namespace(path=cfg, terminator_profile="default", profile="browser", no_terminator=False, json=True)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_shortcuts(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "shortcuts")
            self.assertTrue(any(row["source"] == "terminator" and row["accelerator"] == "<Alt>t" for row in payload["shortcuts"]))


    def test_terminator_workspace_outputs_recipe_json(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "terminator.conf"
            config_path.write_text("""
[layouts]
  [[default]]
    [[[window0]]]
      type = Window
      title = Agents
    [[[terminal0]]]
      type = Terminal
      parent = window0
      title = codex
      custom_command = codex
      use_custom_command = True
""", encoding="utf-8")
            args = argparse.Namespace(path=config_path, layout="default", list_layouts=False, output=None)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_terminator_workspace(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["name"], "terminator-default")
            self.assertEqual(payload["windows"][0]["title"], "Agents")
            self.assertEqual(payload["windows"][0]["tabs"][0]["command"], "codex")

    def test_tasks_command_outputs_master_snapshot_json(self):
        from pathlib import Path
        import os
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                record = store.create("codex-one", "ai", "codex", tmp, window_id="win-1", window_title="Agents", tab_index=0)
                store.update(record.id, status="running", last_pid=os.getpid())
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, all=False, visible_only=False, running_only=False, tag=None, query="codex", json=True, sort="name")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_tasks(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["name"], "codex-one")
            self.assertEqual(payload[0]["window"], "Agents")
            self.assertEqual(payload[0]["focus_key"], "Alt+1")
            self.assertEqual(payload[0]["pause_key"], "Ctrl+Alt+1")


    def test_activity_command_outputs_headless_capture_classification(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        from termdeck.session_store import SessionStore

        class FakeBackend:
            def capture(self, record, lines=80):
                return "Approve command? (yes/no)\nOPENAI_API_KEY=sk-proj-1234567890abcdefghijk\n"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("codex-one", "ai", "codex", tmp, window_id="win-1", window_title="Agents", tab_index=0)
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, all=False, visible_only=False, running_only=False, tag=None, query="input", json=True, sort="severity", lines=80, raw=False)
            buffer = io.StringIO()
            with mock.patch("termdeck.cli.TmuxBackend", return_value=FakeBackend()):
                with contextlib.redirect_stdout(buffer):
                    code = cmd_activity(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload[0]["state"], "needs input")
            self.assertTrue(payload[0]["redacted"])
            self.assertNotIn("sk-proj-1234567890abcdefghijk", payload[0]["preview"])


    def test_new_directory_tab_can_be_remembered_without_starting_tmux(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            args = argparse.Namespace(
                db=db_path,
                target=None,
                command_text=None,
                name="agent-one",
                tag=None,
                cwd=Path(tmp),
                window_id="win-ai",
                window_title="Agents",
                tab_index=None,
                hidden=False,
                no_start=True,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_new(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "new")
            tab = payload["tabs"][0]
            self.assertEqual(tab["name"], "agent-one")
            self.assertEqual(tab["cwd"], str(Path(tmp).resolve()))
            self.assertEqual(tab["source"], "shell")
            self.assertEqual(tab["window_title"], "Agents")
            self.assertEqual(tab["tab_index"], 0)
            self.assertFalse(tab["started"])
            self.assertNotIn("tag", tab)
            self.assertNotIn("tmux_name", tab)


    def test_auth_status_persists_a_verified_private_account_change(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "bash", tmp)
            finally:
                store.close()
            state = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("key-work", email="work@example.com", alias="work", active=True),),
                selected_account="work",
                selected_account_key="key-work",
                fingerprint="verified",
                codex_auth_available=True,
            )
            manager = mock.Mock()
            manager.state.return_value = state
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account=None)
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["selected_account"], "work")
            self.assertTrue(payload["auth_restart_required"])
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "work")
                self.assertTrue(saved.auth_restart_required)
            finally:
                store.close()

    def test_auth_switch_selects_account_for_unselected_new_tab_in_safe_order(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
        )

        calls = []

        class TransactionBackend:
            def plan_auth_change(self, record):
                calls.append("plan")
                return AuthActivationPlan(True, live=True, restart=True, detail="ready")

            def quiesce_auth_change(self, record, plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, record, plan, quiescence):
                calls.append("apply")
                return AuthActivationResult(
                    True,
                    True,
                    recovery_command=recovery,
                    recovery_cwd=tmp,
                    pid=4242,
                    status="running",
                    detail="Codex restarted in this tab with the selected account",
                )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "bash", tmp)
                record = store.update(record.id, status="running")
            finally:
                store.close()
            previous = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work", alias="work", active=False),),
                selected_account=None,
                selected_account_key=None,
                fingerprint="unselected",
                codex_auth_available=True,
            )
            selected = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work", alias="work", active=True),),
                selected_account="work",
                selected_account_key="work",
                fingerprint="verified",
                codex_auth_available=True,
            )
            manager = mock.Mock()
            recovery = "codex --yolo resume 019f71b3-fa32-7251-bac3-c628f34dd154"

            def current_state(_record):
                calls.append("state")
                return previous

            def switch(_record, account_key):
                calls.append(("switch", account_key))
                return CodexSwitchResult(True, selected)

            manager.state.side_effect = current_state
            manager.switch.side_effect = switch
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TransactionBackend()
            ), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 0)
            self.assertEqual(
                calls,
                ["state", "plan", "quiesce", ("switch", "work"), "apply"],
            )
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["selected_account"], "work")
            self.assertTrue(payload["auth_activation_ok"])
            self.assertTrue(payload["auth_activation_restarted"])
            self.assertFalse(payload["auth_restart_required"])
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "work")
                self.assertFalse(saved.auth_restart_required)
                self.assertEqual(saved.recovery_command, recovery)
                self.assertEqual(saved.recovery_cwd, tmp)
                self.assertEqual(saved.last_pid, 4242)
                self.assertEqual(saved.status, "running")
            finally:
                store.close()

    def test_auth_switch_activation_failure_restores_previous_unselected_account(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
        )

        class TransactionBackend:
            def plan_auth_change(self, record):
                return AuthActivationPlan(True, live=True, restart=True, detail="ready")

            def quiesce_auth_change(self, record, plan):
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, record, plan, quiescence):
                return AuthActivationResult(
                    False,
                    False,
                    status="recovery-pending",
                    detail="running Codex session could not be captured exactly",
                    preserve_selected_account=True,
                )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "bash", tmp)
                record = store.update(record.id, status="running")
            finally:
                store.close()
            previous = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work", alias="work", active=False),),
                selected_account=None,
                selected_account_key=None,
                fingerprint="unselected",
                codex_auth_available=True,
            )
            selected = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work", alias="work", active=True),),
                selected_account="work",
                selected_account_key="work",
                fingerprint="verified",
                codex_auth_available=True,
            )
            manager = mock.Mock()
            manager.state.return_value = previous
            manager.switch.return_value = CodexSwitchResult(True, selected)
            manager.restore.return_value = CodexSwitchResult(True, previous)
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TransactionBackend()
            ), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 1)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIsNone(payload["selected_account"])
            self.assertTrue(payload["auth_restart_required"])
            self.assertIn("could not be captured exactly", payload["error"])
            manager.restore.assert_called_once_with(record, None)
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertIsNone(saved.selected_account)
                self.assertTrue(saved.auth_restart_required)
            finally:
                store.close()

    def test_auth_switch_preflight_failure_does_not_change_private_credentials(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import AuthActivationPlan

        calls = []

        class TwoPhaseBackend:
            def plan_auth_change(self, record):
                calls.append(("plan", record.id))
                return AuthActivationPlan(False, detail="live Codex recovery is not exact")

            def quiesce_auth_change(self, record, plan):
                raise AssertionError("a rejected plan must never be quiesced")

            def apply_auth_change(self, record, plan, quiescence):
                raise AssertionError("a rejected plan must never be applied")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "codex", tmp)
                record = store.update(
                    record.id,
                    selected_account="personal",
                    pending_account="abandoned",
                    status="running",
                )
            finally:
                store.close()

            manager = mock.Mock()
            manager.state.return_value = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(
                    CodexAccount("personal-key", alias="personal", active=True),
                    CodexAccount("work-key", alias="work", active=False),
                ),
                selected_account="personal",
                selected_account_key="personal-key",
                fingerprint="before",
                codex_auth_available=True,
            )
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TwoPhaseBackend()
            ), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 1)
            self.assertEqual(calls, [("plan", record.id)])
            manager.state.assert_called_once()
            manager.switch.assert_not_called()
            manager.restore.assert_not_called()
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["selected_account"], "personal")
            self.assertIn("not exact", payload["error"])
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "personal")
                self.assertEqual(saved.pending_account, "abandoned")
                self.assertFalse(saved.auth_restart_required)
            finally:
                store.close()

    def test_auth_switch_restores_previous_account_before_recreating_after_switch_failure(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
        )

        calls = []

        class TwoPhaseBackend:
            def plan_auth_change(self, record):
                calls.append("plan")
                return AuthActivationPlan(True, live=True, restart=True, detail="ready")

            def quiesce_auth_change(self, record, plan):
                calls.append("quiesce")
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, record, plan, quiescence):
                calls.append("apply")
                return AuthActivationResult(
                    True,
                    True,
                    pid=4242,
                    status="running",
                    detail="previous account restored and terminal recreated",
                )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "codex", tmp)
                record = store.update(record.id, selected_account="personal", status="running")
            finally:
                store.close()
            previous = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(
                    CodexAccount("personal-key", alias="personal", active=True),
                    CodexAccount("work-key", alias="work", active=False),
                ),
                selected_account="personal",
                selected_account_key="personal-key",
                fingerprint="before",
                codex_auth_available=True,
            )
            manager = mock.Mock()

            def current_state(_record):
                calls.append("state")
                return previous

            def switch(_record, _account):
                calls.append("switch")
                return CodexSwitchResult(False, previous, "pointer replace failed")

            def restore(_record, account_key):
                calls.append(("restore", account_key))
                return CodexSwitchResult(True, previous)

            manager.state.side_effect = current_state
            manager.switch.side_effect = switch
            manager.restore.side_effect = restore
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TwoPhaseBackend()
            ), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 1)
            self.assertEqual(
                calls,
                [
                    "state",
                    "plan",
                    "quiesce",
                    "switch",
                    ("restore", "personal-key"),
                    "apply",
                ],
            )
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["selected_account"], "personal")
            self.assertFalse(payload["auth_restart_required"])
            self.assertIn("pointer replace failed", payload["error"])
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "personal")
                self.assertIsNone(saved.pending_account)
                self.assertFalse(saved.auth_restart_required)
            finally:
                store.close()

    def test_auth_switch_persists_destructive_legacy_failure_and_clears_stale_recovery(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
        )

        class TwoPhaseBackend:
            def plan_auth_change(self, record):
                return AuthActivationPlan(True, live=True, restart=True, detail="ready")

            def quiesce_auth_change(self, record, plan):
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, record, plan, quiescence):
                return AuthActivationResult(
                    False,
                    False,
                    status="stopped",
                    detail="could not recreate the idle shell",
                    preserve_selected_account=True,
                    command="/bin/bash",
                    cwd=new_cwd,
                    clear_recovery=True,
                )

        with tempfile.TemporaryDirectory() as tmp:
            new_cwd = str(pathlib.Path(tmp) / "live-directory")
            pathlib.Path(new_cwd).mkdir()
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "codex", tmp)
                record = store.update(
                    record.id,
                    selected_account="personal",
                    recovery_command="codex resume 019f71b3-fa32-7251-bac3-c628f34dd154",
                    recovery_cwd=tmp,
                    status="running",
                    last_pid=31337,
                )
            finally:
                store.close()
            previous = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(
                    CodexAccount("personal-key", alias="personal", active=True),
                    CodexAccount("work-key", alias="work", active=False),
                ),
                selected_account="personal",
                selected_account_key="personal-key",
                fingerprint="before",
                codex_auth_available=True,
            )
            selected = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work-key", alias="work", active=True),),
                selected_account="work",
                selected_account_key="work-key",
                fingerprint="selected",
                codex_auth_available=True,
            )
            manager = mock.Mock()
            manager.state.return_value = previous
            manager.switch.return_value = CodexSwitchResult(True, selected)
            manager.restore.return_value = CodexSwitchResult(True, previous)
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TwoPhaseBackend()
            ), contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 1)
            manager.restore.assert_called_once_with(record, "personal-key")
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "personal")
                self.assertTrue(saved.auth_restart_required)
                self.assertEqual(saved.command, "/bin/bash")
                self.assertEqual(saved.cwd, new_cwd)
                self.assertIsNone(saved.recovery_command)
                self.assertIsNone(saved.recovery_cwd)
                self.assertEqual(saved.status, "stopped")
                self.assertIsNone(saved.last_pid)
            finally:
                store.close()

    def test_auth_switch_persists_exact_recovery_after_destructive_failure(self):
        import tempfile
        from unittest import mock
        from termdeck.codex_auth import CodexAccount, CodexAuthState, CodexSwitchResult
        from termdeck.session_store import SessionStore
        from termdeck.tmux_backend import (
            AuthActivationPlan,
            AuthActivationResult,
            AuthQuiesceResult,
        )

        recovery = "codex resume 019f71b3-fa32-7251-bac3-c628f34dd154"

        class TwoPhaseBackend:
            def plan_auth_change(self, record):
                return AuthActivationPlan(True, live=True, restart=True, detail="ready")

            def quiesce_auth_change(self, record, plan):
                return AuthQuiesceResult(True, live=True, terminated=True)

            def apply_auth_change(self, record, plan, quiescence):
                return AuthActivationResult(
                    False,
                    False,
                    recovery_command=recovery,
                    recovery_cwd=tmp,
                    status="recovery-pending",
                    detail="resume failed after the old pane stopped",
                    preserve_selected_account=True,
                )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("agent", "ai", "codex", tmp)
                record = store.update(
                    record.id,
                    selected_account="personal",
                    status="running",
                    last_pid=31337,
                )
            finally:
                store.close()
            previous = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(
                    CodexAccount("personal-key", alias="personal", active=True),
                    CodexAccount("work-key", alias="work", active=False),
                ),
                selected_account="personal",
                selected_account_key="personal-key",
                fingerprint="before",
                codex_auth_available=True,
            )
            selected = CodexAuthState(
                codex_home=record.codex_home,
                accounts=(CodexAccount("work-key", alias="work", active=True),),
                selected_account="work",
                selected_account_key="work-key",
                fingerprint="selected",
                codex_auth_available=True,
            )
            manager = mock.Mock()
            manager.state.return_value = previous
            manager.switch.return_value = CodexSwitchResult(True, selected)
            manager.restore.return_value = CodexSwitchResult(True, previous)
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager", return_value=manager), mock.patch(
                "termdeck.cli.TmuxBackend", return_value=TwoPhaseBackend()
            ), contextlib.redirect_stdout(io.StringIO()):
                code = cmd_auth(args)

            self.assertEqual(code, 1)
            manager.restore.assert_called_once_with(record, "personal-key")
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertEqual(saved.selected_account, "personal")
                self.assertEqual(saved.command, "codex")
                self.assertEqual(saved.cwd, tmp)
                self.assertEqual(saved.recovery_command, recovery)
                self.assertEqual(saved.recovery_cwd, tmp)
                self.assertEqual(saved.status, "recovery-pending")
                self.assertIsNone(saved.last_pid)
            finally:
                store.close()

    def test_auth_rejects_adopted_external_tmux_session(self):
        import tempfile
        from unittest import mock
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = pathlib.Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                record = store.create("external", "adopted", "bash", tmp)
                record = store.update(record.id, external=True)
            finally:
                store.close()
            args = argparse.Namespace(db=db_path, selector=record.id, switch_account="work")
            buffer = io.StringIO()
            with mock.patch.dict(
                "os.environ",
                {"TERMDECK_CODEX_ACCOUNT_SOURCE": str(pathlib.Path(tmp) / "empty-source")},
            ), mock.patch("termdeck.cli.CodexAuthManager") as manager, contextlib.redirect_stdout(buffer):
                code = cmd_auth(args)

            self.assertEqual(code, 2)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("cannot be retroactively isolated", payload["error"])
            manager.assert_not_called()
            store = SessionStore(db_path, account_source=pathlib.Path(tmp) / "empty-source")
            try:
                saved = store.get(record.id)
                self.assertIsNone(saved.pending_account)
                self.assertIsNone(saved.selected_account)
            finally:
                store.close()

    def test_windows_command_outputs_aggregate_json(self):
        from pathlib import Path
        import os
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                first = store.create("api", "dev", "python3 -m http.server", tmp, window_id="win-dev", window_title="Dev", tab_index=0)
                second = store.create("codex", "ai", "codex", tmp, window_id="win-agents", window_title="Agents", tab_index=0)
                store.update(first.id, status="running", last_pid=os.getpid())
                store.update(second.id, status="new", last_pid=None)
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, all=False, visible_only=False, running_only=False, tag=None, query="", json=True, sort="title")
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_windows(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual([row["title"] for row in payload], ["Agents", "Dev"])
            self.assertEqual(payload[0]["tab_count"], 1)
            self.assertIn("cpu_percent", payload[1])
            self.assertIn("state_summary", payload[1])

    def test_diagnostics_command_outputs_json_and_zip_bundle(self):
        from pathlib import Path
        import tempfile
        import zipfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("api", "dev", "python3 -m http.server", tmp, window_id="dev", window_title="Dev", tab_index=0)
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, config=None, output=None)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_diagnostics(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], "termdeck.diagnostics")
            self.assertEqual(payload["sessions"]["counts"]["total"], 1)
            self.assertFalse(payload["privacy"]["captures_included"])

            out_path = Path(tmp) / "termdeck-diagnostics.zip"
            zip_args = argparse.Namespace(db=db_path, config=None, output=out_path)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_diagnostics(zip_args)
            self.assertEqual(code, 0)
            bundle = json.loads(buffer.getvalue())
            self.assertTrue(bundle["ok"])
            self.assertTrue(out_path.exists())
            with zipfile.ZipFile(out_path) as archive:
                self.assertIn("diagnostics.json", archive.namelist())
                self.assertIn("tasks.json", archive.namelist())

    def test_diagnose_freeze_command_emits_bounded_payload(self):
        from unittest import mock

        args = argparse.Namespace(db=pathlib.Path("/tmp/fake-db"), auth_root=pathlib.Path("/tmp/fake-auth"))
        expected = {"schema": "termdeck.freeze-diagnostics.v1"}
        buffer = io.StringIO()
        with mock.patch(
            "termdeck.cli.build_freeze_diagnostic_payload", return_value=dict(expected)
        ) as build, contextlib.redirect_stdout(buffer):
            code = cmd_diagnose_freeze(args)

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "diagnose-freeze")
        build.assert_called_once_with(db_path=args.db, auth_root=args.auth_root)

    def test_auth_gc_is_dry_run_only_in_this_release(self):
        from unittest import mock

        args = argparse.Namespace(
            db=pathlib.Path("/tmp/fake-db"),
            auth_root=pathlib.Path("/tmp/fake-auth"),
            min_age_hours=24.0,
        )
        buffer = io.StringIO()
        with mock.patch(
            "termdeck.cli.plan_auth_scope_gc",
            return_value={"ok": True, "action": "auth-gc-plan", "dry_run": True},
        ) as plan, contextlib.redirect_stdout(buffer):
            code = cmd_auth_gc(args)

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["mutation_available"])
        self.assertIn("dry-run only", payload["detail"])
        plan.assert_called_once_with(
            db_path=args.db,
            auth_root=args.auth_root,
            min_age_seconds=24.0 * 3600.0,
        )

    def test_settings_sample_outputs_loadable_json(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_settings_sample(argparse.Namespace())
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("health", payload)
        self.assertIn("tags", payload)


    def test_reconcile_command_repairs_stale_running_session_json(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                record = store.create("codex-one", "ai", "codex", tmp)
                store.update(record.id, status="running", last_pid=999, visible=True)
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, tag=None, all=False, dry_run=False, keep_visible=False)
            buffer = io.StringIO()
            # This CLI test must not inherit the host's potentially saturated
            # tmux control plane. A bounded query failure deliberately keeps a
            # prior running state, so prove the intended missing-session branch
            # with a deterministic backend observation instead.
            with mock.patch(
                "termdeck.tmux_backend.TmuxBackend.session_exists_many",
                return_value={record.id: False},
            ), contextlib.redirect_stdout(buffer):
                code = cmd_reconcile(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "reconcile")
            self.assertEqual(payload["changed"], 1)
            self.assertEqual(payload["sessions"][0]["after_status"], "stopped")
            self.assertFalse(payload["sessions"][0]["after_visible"])

            store = SessionStore(db_path)
            try:
                refreshed = store.get(record.id)
                self.assertEqual(refreshed.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(refreshed.visible)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_health_command_outputs_empty_json_for_empty_db(self):
        args = argparse.Namespace(db=":memory:", all=False, json=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_health(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue()), [])

    def test_palette_command_outputs_actions(self):
        args = argparse.Namespace(query="master", db=":memory:", all=False, limit=50)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_palette(args)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload[0]["id"], "builtin:master")
        self.assertEqual(payload[0]["kind"], "builtin")


    def test_workspace_export_outputs_recipe_from_session_db(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("api", "dev", "python3 -m http.server", tmp, window_id="win-1", window_title="Dev", tab_index=0)
            finally:
                store.close()
            args = argparse.Namespace(db=db_path, name="demo", tag=None, visible_only=False, running_only=False, all=False, output=None)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_workspace_export(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["name"], "demo")
            self.assertEqual(payload["windows"][0]["title"], "Dev")
            self.assertEqual(payload["windows"][0]["tabs"][0]["name"], "api")


    def test_workspace_open_no_start_materialises_recipe_json(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            recipe_path = Path(tmp) / "workspace.json"
            recipe_path.write_text(
                json.dumps({
                    "name": "demo",
                    "windows": [
                        {"title": "Dev", "tabs": [
                            {"name": "api", "tag": "dev", "command": "python3 -m http.server", "cwd": "."},
                            {"name": "tests", "tag": "tests", "command": "pytest", "cwd": "."},
                        ]}
                    ],
                }),
                encoding="utf-8",
            )
            db_path = Path(tmp) / "sessions.sqlite3"
            args = argparse.Namespace(path=recipe_path, db=db_path, no_start=True, dry_run=False, no_reuse=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_workspace_open(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "workspace-open")
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["count"], 2)
            self.assertFalse(payload["start"])

            store = SessionStore(db_path)
            try:
                rows = store.list(include_archived=True)
                self.assertEqual(len(rows), 2)
                self.assertEqual({row.window_title for row in rows}, {"Dev"})
                self.assertEqual([row.status for row in rows], ["new", "new"])
            finally:
                store.close()

    def test_workspace_open_dry_run_does_not_write_session_db(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            recipe_path = Path(tmp) / "workspace.json"
            recipe_path.write_text('{"name":"dry","tabs":[{"name":"shell","command":"{shell}"}]}', encoding="utf-8")
            db_path = Path(tmp) / "sessions.sqlite3"
            args = argparse.Namespace(path=recipe_path, db=db_path, no_start=True, dry_run=True, no_reuse=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_workspace_open(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["sessions"][0]["status"], "planned-register")

            store = SessionStore(db_path)
            try:
                self.assertEqual(store.list(include_archived=True), [])
            finally:
                store.close()

    def test_workspace_plan_sample_outputs_recipe_json(self):
        args = argparse.Namespace(sample=True, path=None)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_workspace_plan(args)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("windows", payload)

    def test_select_records_targets_by_tag_name_and_prefix(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("codex-one", "ai", "codex", tmp)
                second = store.create("pytest", "tests", "pytest", tmp)
                selected = _select_records(store, [first.id[:6], "tests"])
                self.assertEqual({record.id for record in selected}, {first.id, second.id})
            finally:
                store.close()

    def test_window_targeting_filters_before_ids_names_tags_and_all(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first_a = store.create("duplicate", "shared", "codex", tmp, window_id="win-a")
                second_a = store.create("a-only", "shared", "bash", tmp, window_id="win-a")
                first_b = store.create("duplicate", "shared", "codex", tmp, window_id="win-b")
                store.create("b-only", "other", "bash", tmp, window_id="win-b")

                by_name = _select_records(store, ["duplicate"], window_id="win-a")
                self.assertEqual([record.id for record in by_name], [first_a.id])

                foreign_exact_id = _select_records(store, [first_b.id], window_id="win-a")
                self.assertEqual(foreign_exact_id, [])

                by_tag = _select_records(store, tag="shared", window_id="win-a")
                self.assertEqual({record.id for record in by_tag}, {first_a.id, second_a.id})

                all_in_window = _select_records(store, all_sessions=True, window_id="win-a")
                self.assertEqual({record.id for record in all_in_window}, {first_a.id, second_a.id})
            finally:
                store.close()

    def test_window_id_parser_and_sessions_listing_are_scoped_without_move_regression(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        parser = build_parser()
        send_args = parser.parse_args(["send", "status", "--window-id", "win-a", "--all"])
        self.assertEqual(send_args.target_window_id, "win-a")
        pause_args = parser.parse_args(["pause", "--window-id", "win-a", "--all"])
        self.assertEqual(pause_args.target_window_id, "win-a")
        move_args = parser.parse_args(["move", "tab", "--window-id", "win-b"])
        self.assertEqual(move_args.window_id, "win-b")
        self.assertFalse(hasattr(move_args, "target_window_id"))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                expected = store.create("a", "ai", "codex", tmp, window_id="win-a")
                store.create("b", "ai", "codex", tmp, window_id="win-b")
            finally:
                store.close()

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_sessions(argparse.Namespace(db=db_path, all=True, window_id="win-a"))
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual([row["id"] for row in payload], [expected.id])

    def test_control_plan_command_outputs_safety_json(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("codex-one", "ai", "codex", tmp)
                store.create("codex-two", "ai", "codex", tmp)
            finally:
                store.close()
            args = argparse.Namespace(db=db_path, action="send", selectors=[], tag="ai", all=False, text="status", no_enter=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_control_plan(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "send")
            self.assertEqual(payload["count"], 2)
            self.assertTrue(payload["requires_confirmation"])
            self.assertEqual(payload["risk"], "high")

    def test_bulk_send_without_yes_returns_plan_instead_of_executing(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("codex-one", "ai", "codex", tmp)
                store.create("codex-two", "ai", "codex", tmp)
            finally:
                store.close()
            args = argparse.Namespace(db=db_path, selectors=[], tag="ai", all=False, text=["status"], no_enter=False, dry_run=False, yes=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_send(args)
            self.assertEqual(code, 2)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "send")
            self.assertTrue(payload["requires_confirmation"])



    def test_budget_plan_sample_and_json_outputs(self):
        from pathlib import Path
        import os
        import tempfile
        from termdeck.session_store import SessionStore

        sample_args = argparse.Namespace(sample=True, path=None)
        sample_buffer = io.StringIO()
        with contextlib.redirect_stdout(sample_buffer):
            sample_code = cmd_budget_plan(sample_args)
        self.assertEqual(sample_code, 0)
        self.assertIn("budgets", json.loads(sample_buffer.getvalue()))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            budget_path = Path(tmp) / "budgets.json"
            budget_path.write_text(json.dumps({"budgets": [{"name": "tight-ai", "tag": "ai", "max_memory": "1B", "action": "inspect"}]}), encoding="utf-8")
            store = SessionStore(db_path)
            try:
                row = store.create("codex-one", "ai", "codex", tmp)
                store.update(row.id, status="running", last_pid=os.getpid())
            finally:
                store.close()
            args = argparse.Namespace(path=budget_path, sample=False, db=db_path, tag=None, visible_only=False, running_only=False, all=False, violations_only=True, json=True)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_budget_plan(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "budget-plan")
            self.assertEqual(payload["count"], 1)
            self.assertTrue(payload["sessions"][0]["over_budget"])
            self.assertEqual(payload["sessions"][0]["budget"]["name"], "tight-ai")

    def test_pause_over_budget_requires_confirmation_before_execution(self):
        from pathlib import Path
        import os
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            budget_path = Path(tmp) / "budgets.json"
            budget_path.write_text(json.dumps({"budgets": [{"name": "scratch-limit", "tag": "scratch", "max_memory": "1B", "action": "pause"}]}), encoding="utf-8")
            store = SessionStore(db_path)
            try:
                row = store.create("runaway", "scratch", "python loop.py", tmp)
                store.update(row.id, status="running", last_pid=os.getpid())
            finally:
                store.close()
            args = argparse.Namespace(path=budget_path, db=db_path, tag="scratch", visible_only=False, running_only=False, all=False, dry_run=False, yes=False)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_pause_over_budget(args)
            self.assertEqual(code, 2)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["action"], "pause-over-budget")
            self.assertTrue(payload["requires_confirmation"])
            self.assertIn("budget_reasons", payload)

    def test_capture_redacts_secret_like_output_by_default(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from termdeck.session_store import SessionStore

        class FakeBackend:
            def capture(self, record, lines=120):
                return "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\napi_key=sk_test_1234567890abcdef"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                record = store.create("codex-one", "ai", "codex", tmp)
            finally:
                store.close()

            args = argparse.Namespace(db=db_path, selectors=[record.id], tag=None, all=False, lines=120, redact=False, raw=False)
            buffer = io.StringIO()
            with patch("termdeck.cli.TmuxBackend", lambda: FakeBackend()):
                with contextlib.redirect_stdout(buffer):
                    code = cmd_capture(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload[0]["redacted"])
            self.assertTrue({"Authorization header", "Bearer token"} & set(payload[0]["redaction_labels"]))
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", payload[0]["capture"])
            self.assertNotIn("sk_test_1234567890abcdef", payload[0]["capture"])

    def test_lifecycle_cli_renames_retags_hides_shows_moves_and_archives(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                record = store.create("codex-one", "ai", "codex", tmp, window_id="agents", window_title="Agents", tab_index=0)
                second = store.create("pytest", "tests", "pytest", tmp, window_id="agents", window_title="Agents", tab_index=1)
            finally:
                store.close()

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_rename(argparse.Namespace(db=db_path, selector=record.id, name="codex-main"))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["sessions"][0]["name"], "codex-main")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_retag(argparse.Namespace(db=db_path, new_tag="bugbounty", selectors=[record.id], tag=None, all=False))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["sessions"][0]["tag"], "bugbounty")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_hide(argparse.Namespace(db=db_path, selectors=[record.id], tag=None, all=False))
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(buffer.getvalue())["sessions"][0]["visible"])

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_show(argparse.Namespace(db=db_path, selectors=[record.id], tag=None, all=False, window_id="visible", window_title="Visible", tab_index=None))
            self.assertEqual(code, 0)
            shown = json.loads(buffer.getvalue())["sessions"][0]
            self.assertTrue(shown["visible"])
            self.assertEqual(shown["window_title"], "Visible")

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_move(argparse.Namespace(db=db_path, selectors=[record.id, second.id], tag=None, all=False, window_id="ops", window_title="Ops", tab_index=4, hidden=False))
            self.assertEqual(code, 0)
            moved = json.loads(buffer.getvalue())["sessions"]
            self.assertEqual([row["tab_index"] for row in moved], [0, 1])

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_archive(argparse.Namespace(db=db_path, selectors=[record.id], tag=None, all=False))
            self.assertEqual(code, 0)
            archived = json.loads(buffer.getvalue())["sessions"][0]
            self.assertEqual(archived["status"], "archived")
            self.assertFalse(archived["visible"])

    def test_pin_cli_front_loads_and_protects_kill(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                shell = store.create("shell", "dev", "bash", tmp, window_id="main", window_title="Main", tab_index=0)
                agent = store.create("agent", "ai", "codex", tmp, window_id="main", window_title="Main", tab_index=1)
            finally:
                store.close()

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_pin(argparse.Namespace(db=db_path, selectors=[agent.id], tag=None, all=False))
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["sessions"][0]["pinned"])

            store = SessionStore(db_path)
            try:
                visible = store.list(visible_only=True, window_id="main")
                self.assertEqual([record.id for record in visible], [agent.id, shell.id])
            finally:
                store.close()

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_kill(argparse.Namespace(db=db_path, selectors=[agent.id], tag=None, all=False, dry_run=False, yes=True, include_pinned=False))
            self.assertEqual(code, 1)
            protected = json.loads(buffer.getvalue())["sessions"][0]
            self.assertFalse(protected["ok"])
            self.assertTrue(protected["pinned"])
            self.assertIn("protected", protected["detail"])

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_unpin(argparse.Namespace(db=db_path, selectors=[agent.id], tag=None, all=False))
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(buffer.getvalue())["sessions"][0]["pinned"])

    def test_recently_closed_and_reopen_closed_cli_are_browser_like(self):
        from pathlib import Path
        import tempfile
        from termdeck.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                visible = store.create("shell", "dev", "bash", tmp, window_id="dev", window_title="Dev", tab_index=0)
                hidden = store.create("codex-one", "ai", "codex", tmp, window_id="agents", window_title="Agents", tab_index=3)
                store.update(hidden.id, visible=False, status="running", last_pid=123, last_opened_at="2026-07-09T00:00:02+00:00")
                store.update(visible.id, last_opened_at="2026-07-09T00:00:01+00:00")
            finally:
                store.close()

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_recently_closed(argparse.Namespace(db=db_path, tag=None, limit=5, json=True))
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["sessions"][0]["id"], hidden.id)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_reopen_closed(argparse.Namespace(db=db_path, tag=None, limit=1, window_id="main", window_title="Main"))
            self.assertEqual(code, 0)
            reopened = json.loads(buffer.getvalue())["sessions"][0]
            self.assertEqual(reopened["action"], "reopen-closed")
            self.assertTrue(reopened["visible"])
            self.assertEqual(reopened["window_title"], "Main")
            self.assertEqual(reopened["status"], "running")

    def test_search_command_finds_metadata_and_redacted_output(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from termdeck.session_store import SessionStore

        class FakeBackend:
            def capture(self, record, lines=120):
                return "ready\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\nagent finished status summary"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sessions.sqlite3"
            store = SessionStore(db_path)
            try:
                store.create("codex-one", "ai", "codex", tmp, window_id="agents", window_title="Agents", tab_index=0)
            finally:
                store.close()

            args = argparse.Namespace(
                db=db_path,
                query="status summary",
                tag=None,
                visible_only=False,
                running_only=False,
                all=False,
                include_output=True,
                lines=120,
                snippets=2,
                limit=100,
                raw=False,
                json=True,
            )
            buffer = io.StringIO()
            with patch("termdeck.cli.TmuxBackend", lambda: FakeBackend()):
                with contextlib.redirect_stdout(buffer):
                    code = cmd_search(args)
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["schema"], "termdeck.session-search")
            self.assertEqual(payload["count"], 1)
            match = payload["matches"][0]
            self.assertEqual(match["name"], "codex-one")
            self.assertEqual(match["output_match_count"], 1)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(payload))
            self.assertTrue(match["redacted"])



if __name__ == "__main__":
    unittest.main()
