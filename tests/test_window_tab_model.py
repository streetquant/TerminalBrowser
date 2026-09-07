import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from termdeck.command_render import directory_tab_name, normalise_directory
from termdeck.gtk_app import TermDeckWindow, _private_resize_handler
from termdeck.session_lifecycle import recently_closed_records
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxError


class LabelStub:
    def __init__(self) -> None:
        self.text = ""

    def set_text(self, text: str) -> None:
        self.text = text


class SideStub:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


class WindowTabModelTests(TestCase):
    def test_private_resize_handler_captures_identity_without_store_access(self) -> None:
        calls: list[tuple[int, str, str]] = []

        class Backend:
            @staticmethod
            def propagate_terminal_resize(
                pane_pid: int,
                *,
                codex_home: str,
                auth_scope_id: str,
            ):
                calls.append((pane_pid, codex_home, auth_scope_id))
                return "ok"

        record = SimpleNamespace(
            codex_home="/tmp/auth-test",
            auth_scope_id="auth-test",
        )
        handler = _private_resize_handler(Backend(), record)
        self.assertIsNotNone(handler)

        # The callback executes later on PaneStreamController's worker. Mutate
        # the stand-in row to prove the handler retained immutable strings and
        # does not resolve any store or GTK object at callback time.
        record.codex_home = "/tmp/auth-other"
        record.auth_scope_id = "auth-other"
        self.assertEqual(handler(4321, 120, 40), "ok")  # type: ignore[misc]
        self.assertEqual(calls, [(4321, "/tmp/auth-test", "auth-test")])

    def test_private_resize_handler_rejects_incomplete_identity(self) -> None:
        backend = SimpleNamespace(propagate_terminal_resize=lambda *_args, **_kwargs: None)
        self.assertIsNone(
            _private_resize_handler(
                backend,
                SimpleNamespace(codex_home="", auth_scope_id="auth-test"),
            )
        )

    def test_stale_backend_pid_preserves_live_status_without_sampling(self) -> None:
        class Sampler:
            def snapshot(self, _pid):
                raise AssertionError("stale pane data must not mark a live tab stopped")

        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=SimpleNamespace(pane_snapshot_stale=True),
                sampler=Sampler(),
            )
        )
        running = SimpleNamespace(status="running")
        paused = SimpleNamespace(status="paused")

        self.assertEqual(TermDeckWindow.observed_status(window, running, 101), "running")
        self.assertEqual(TermDeckWindow.observed_status(window, paused, 202), "paused")

    def test_restore_batches_backend_queries_and_refreshes_side_once(self) -> None:
        records = [
            SimpleNamespace(id="second", tab_index=1),
            SimpleNamespace(id="first", tab_index=0),
        ]
        calls = []

        class Backend:
            def session_exists_many(self, candidates):
                calls.append(("exists", [record.id for record in candidates]))
                return {record.id: True for record in candidates}

            def pane_pids(self, candidates):
                calls.append(("pids", [record.id for record in candidates]))
                return {record.id: index for index, record in enumerate(candidates, start=101)}

        side = SideStub()
        window = SimpleNamespace(
            app_context=SimpleNamespace(backend=Backend()),
            tabs={},
            side=side,
        )

        def open_session(record, **kwargs):
            calls.append(("open", record.id, kwargs))
            window.tabs[record.id] = object()

        window.open_session = open_session

        restored = TermDeckWindow.restore_previous_tabs(window, records=records)

        self.assertEqual(restored, 2)
        self.assertEqual(calls[0], ("exists", ["first", "second"]))
        self.assertEqual(calls[1], ("pids", ["first", "second"]))
        self.assertEqual([call[1] for call in calls[2:]], ["first", "second"])
        self.assertTrue(all(call[2]["known_live"] for call in calls[2:]))
        self.assertTrue(all(call[2]["known_pid_resolved"] for call in calls[2:]))
        self.assertTrue(all(call[2]["restart_missing"] for call in calls[2:]))
        self.assertTrue(all(not call[2]["refresh_side"] for call in calls[2:]))
        self.assertTrue(all(call[2]["retry_start_failure"] for call in calls[2:]))
        self.assertEqual(side.refresh_count, 1)

    def test_one_attach_exception_does_not_hide_later_tabs(self) -> None:
        """A VTE/attach failure is isolated and retried, not window-fatal."""

        records = [
            SimpleNamespace(id="first", tab_index=0, visible=True, status="running"),
            SimpleNamespace(id="second", tab_index=1, visible=True, status="running"),
        ]
        updates = []

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: True for candidate in candidates}

            def pane_pids(self, candidates):
                return {candidate.id: index for index, candidate in enumerate(candidates, start=101)}

        side = SideStub()
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(
                    update=lambda session_id, **changes: updates.append((session_id, changes)),
                ),
            ),
            tabs={},
            side=side,
        )

        def open_session(record, **_kwargs):
            if record.id == "first":
                raise RuntimeError("VTE allocation failed")
            window.tabs[record.id] = object()

        window.open_session = open_session

        restored = TermDeckWindow.restore_previous_tabs(window, records=records)

        self.assertEqual(restored, 1)
        self.assertEqual(set(window.tabs), {"second"})
        self.assertEqual(window._restore_retry_ids, {"first"})
        self.assertEqual(updates[0][0], "first")
        self.assertEqual(updates[0][1]["visible"], True)
        self.assertEqual(side.refresh_count, 1)

    def test_store_bookkeeping_failure_does_not_hide_later_tabs(self) -> None:
        records = [
            SimpleNamespace(id="first", tab_index=0, visible=True, status="running"),
            SimpleNamespace(id="second", tab_index=1, visible=True, status="running"),
        ]

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: True for candidate in candidates}

            def pane_pids(self, candidates):
                return {candidate.id: 200 + index for index, candidate in enumerate(candidates)}

        def reject_update(_session_id, **_changes):
            raise KeyError("row was concurrently removed")

        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(update=reject_update),
            ),
            tabs={},
            side=SideStub(),
        )

        def open_session(record, **_kwargs):
            if record.id == "first":
                raise RuntimeError("first VTE failed")
            window.tabs[record.id] = object()

        window.open_session = open_session

        restored = TermDeckWindow.restore_previous_tabs(window, records=records)

        self.assertEqual(restored, 1)
        self.assertEqual(set(window.tabs), {"second"})
        self.assertEqual(window._restore_retry_ids, {"first"})

    def test_accepted_recovery_never_falls_through_to_direct_restart(self) -> None:
        record = SimpleNamespace(
            id="dead-pane",
            tab_index=0,
            visible=True,
            status="running",
        )
        captured = {}

        class Coordinator:
            def submit(self, candidate, **kwargs):
                captured.update(kwargs)
                captured["record"] = candidate
                return SimpleNamespace(accepted=True, state="queued")

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: False for candidate in candidates}

            def pane_pids(self, _candidates):
                return {}

        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(get=lambda _session_id: record),
                _get_recovery_coordinator=lambda: Coordinator(),
            ),
            tabs={},
            side=SideStub(),
            _restore_callbacks_alive=True,
        )

        def open_session(candidate, **kwargs):
            captured["open"] = (candidate, kwargs)
            window.tabs[candidate.id] = object()

        window.open_session = open_session
        with mock.patch("termdeck.gtk_app.startup_recovery_allowed", return_value=True):
            restored = TermDeckWindow.restore_previous_tabs(window, records=[record])

        self.assertEqual(restored, 0)
        self.assertNotIn("open", captured)
        self.assertEqual(captured["mode"], "payload")
        self.assertEqual(window._restore_inflight_ids, {record.id})

        captured["on_ready"](SimpleNamespace(pid=6101))

        self.assertEqual(set(window.tabs), {record.id})
        self.assertEqual(window._restore_inflight_ids, set())
        self.assertFalse(captured["open"][1]["restart_missing"])

    def test_destroyed_window_ignores_late_recovery_callback(self) -> None:
        record = SimpleNamespace(
            id="late-pane",
            tab_index=0,
            visible=True,
            status="running",
        )
        captured = {}

        class Coordinator:
            def submit(self, _candidate, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(accepted=True, state="queued")

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: False for candidate in candidates}

            def pane_pids(self, _candidates):
                return {}

        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(
                    get=lambda _session_id: (_ for _ in ()).throw(
                        AssertionError("destroyed callback touched the store")
                    ),
                ),
                _get_recovery_coordinator=lambda: Coordinator(),
            ),
            tabs={},
            side=SideStub(),
            _restore_callbacks_alive=True,
            open_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("destroyed callback opened a tab")
            ),
        )
        with mock.patch("termdeck.gtk_app.startup_recovery_allowed", return_value=True):
            TermDeckWindow.restore_previous_tabs(window, records=[record])

        window._restore_callbacks_alive = False
        captured["on_ready"](SimpleNamespace(pid=6102))

        self.assertEqual(window.tabs, {})
        self.assertEqual(window._restore_inflight_ids, set())

    def test_retry_discards_explicitly_stopped_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("stopped", "terminal", "bash", tmp)
                record = store.update(
                    record.id,
                    status="stopped",
                    recovery_intent="inactive",
                    visible=True,
                )
                restore = mock.Mock()
                window = SimpleNamespace(
                    _restore_callbacks_alive=True,
                    _restore_retry_source_id=9,
                    _restore_retry_attempts=0,
                    _restore_retry_ids={record.id},
                    tabs={},
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(session_exists=lambda _record: False),
                    ),
                    restore_previous_tabs=restore,
                )

                result = TermDeckWindow._retry_restore_tabs(window)

                self.assertFalse(result)
                self.assertEqual(window._restore_retry_ids, set())
                restore.assert_not_called()
            finally:
                store.close()

    def test_restore_restarts_selected_tabs_when_the_backend_was_lost(self) -> None:
        record = SimpleNamespace(id="crashed", tab_index=0)
        calls = []

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: False for candidate in candidates}

            def pane_pids(self, _candidates):
                raise AssertionError("missing sessions have no pane pids")

        side = SideStub()
        window = SimpleNamespace(
            app_context=SimpleNamespace(backend=Backend()),
            tabs={},
            side=side,
        )

        def open_session(candidate, **kwargs):
            calls.append((candidate.id, kwargs))
            window.tabs[candidate.id] = object()

        window.open_session = open_session

        restored = TermDeckWindow.restore_previous_tabs(window, records=[record])

        self.assertEqual(restored, 1)
        self.assertEqual(calls[0][0], "crashed")
        self.assertFalse(calls[0][1]["known_live"])
        self.assertTrue(calls[0][1]["restart_missing"])
        self.assertEqual(side.refresh_count, 1)

    def test_failed_crash_restart_keeps_the_tab_visible_and_recoverable(self) -> None:
        record = SimpleNamespace(id="crashed", tab_index=0, visible=True)
        updates = []

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: False for candidate in candidates}

            def pane_pids(self, _candidates):
                return {}

        side = SideStub()
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(
                    update=lambda session_id, **changes: updates.append((session_id, changes))
                ),
            ),
            tabs={},
            side=side,
            open_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TmuxError("backend unavailable")
            ),
        )

        restored = TermDeckWindow.restore_previous_tabs(window, records=[record])

        self.assertEqual(restored, 0)
        self.assertEqual(
            updates,
            [("crashed", {"status": "start-error", "last_pid": None, "visible": True})],
        )
        self.assertEqual(side.refresh_count, 1)

    def test_failed_exact_restore_keeps_recovery_pending_status(self) -> None:
        record = SimpleNamespace(
            id="recover-me",
            tab_index=0,
            visible=True,
            status="recovery-pending",
        )
        updates = []

        class Backend:
            def session_exists_many(self, candidates):
                return {candidate.id: False for candidate in candidates}

            def pane_pids(self, _candidates):
                return {}

        side = SideStub()
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                backend=Backend(),
                store=SimpleNamespace(
                    update=lambda session_id, **changes: updates.append((session_id, changes))
                ),
            ),
            tabs={},
            side=side,
            open_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TmuxError("exact recovery unavailable")
            ),
        )

        restored = TermDeckWindow.restore_previous_tabs(window, records=[record])

        self.assertEqual(restored, 0)
        self.assertEqual(
            updates,
            [
                (
                    "recover-me",
                    {
                        "status": "recovery-pending",
                        "last_pid": None,
                        "visible": True,
                    },
                )
            ],
        )

    def test_directory_tab_name_uses_only_final_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "client-api"
            nested.mkdir()
            self.assertEqual(directory_tab_name(nested), "client-api")
            self.assertEqual(normalise_directory(nested), str(nested.resolve()))

    def test_store_rename_window_updates_only_that_group_and_preserves_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("api", "terminal", "bash", tmp, window_id="client", window_title="Old")
                second = store.create("web", "terminal", "bash", tmp, window_id="client", window_title="Old")
                other = store.create("notes", "terminal", "bash", tmp, window_id="other", window_title="Other")
                timestamps = {row.id: row.last_opened_at for row in (first, second, other)}

                changed = store.rename_window("client", "Client Work")

                self.assertEqual(changed, 2)
                self.assertEqual(store.get(first.id).window_title, "Client Work")  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).window_title, "Client Work")  # type: ignore[union-attr]
                self.assertEqual(store.get(other.id).window_title, "Other")  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).last_opened_at, timestamps[first.id])  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).last_opened_at, timestamps[second.id])  # type: ignore[union-attr]
            finally:
                store.close()

    def test_new_shell_tab_uses_directory_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "backend"
            nested.mkdir()
            calls = []
            window = SimpleNamespace(create_session=lambda *args: calls.append(args))

            TermDeckWindow.new_shell_tab(window, nested)

            self.assertEqual(calls[0][0], "backend")
            self.assertEqual(calls[0][1], "terminal")
            self.assertEqual(calls[0][3], str(nested.resolve()))

    def test_active_directory_prefers_live_tmux_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stored = Path(tmp) / "stored"
            live = Path(tmp) / "live"
            stored.mkdir()
            live.mkdir()
            record = SimpleNamespace(cwd=str(stored))
            backend = SimpleNamespace(current_directory=lambda _record: str(live))
            window = SimpleNamespace(
                current_record=lambda: record,
                app_context=SimpleNamespace(backend=backend),
            )

            result = TermDeckWindow._active_directory(window)

            self.assertEqual(result, str(live.resolve()))

    def test_active_directory_falls_back_to_stored_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stored = Path(tmp) / "stored"
            stored.mkdir()
            record = SimpleNamespace(cwd=str(stored))
            backend = SimpleNamespace(current_directory=lambda _record: None)
            window = SimpleNamespace(
                current_record=lambda: record,
                app_context=SimpleNamespace(backend=backend),
            )

            result = TermDeckWindow._active_directory(window)

            self.assertEqual(result, str(stored.resolve()))

    def test_active_directory_uses_home_when_no_tab_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            window = SimpleNamespace(current_record=lambda: None)

            from unittest.mock import patch

            with patch("termdeck.gtk_app.Path.home", return_value=home):
                result = TermDeckWindow._active_directory(window)

            self.assertEqual(result, str(home.resolve()))

    def test_default_tab_name_tracks_live_directory_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_directory = Path(tmp) / "old-project"
            new_directory = Path(tmp) / "new-project"
            old_directory.mkdir()
            new_directory.mkdir()
            record = SimpleNamespace(name="old-project", cwd=str(old_directory))

            changes = TermDeckWindow._directory_tracking_changes(record, str(new_directory))

            self.assertEqual(
                changes,
                {"cwd": str(new_directory.resolve()), "name": "new-project"},
            )

    def test_custom_tab_name_remains_an_override_when_directory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_directory = Path(tmp) / "old-project"
            new_directory = Path(tmp) / "new-project"
            old_directory.mkdir()
            new_directory.mkdir()
            record = SimpleNamespace(name="API shell", cwd=str(old_directory))

            changes = TermDeckWindow._directory_tracking_changes(record, str(new_directory))

            self.assertEqual(changes, {"cwd": str(new_directory.resolve())})

    def test_tab_directory_refresh_batches_lookup_and_updates_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_directory = root / "old-project"
            new_directory = root / "new-project"
            old_directory.mkdir()
            new_directory.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create("old-project", "terminal", "bash", old_directory)
                original_opened_at = record.last_opened_at
                calls = []

                class Backend:
                    def current_directories(self, records):
                        calls.append([candidate.id for candidate in records])
                        return {record.id: str(new_directory)}

                tab = SimpleNamespace(record=record)
                side = SideStub()
                window = SimpleNamespace(
                    tabs={record.id: tab},
                    app_context=SimpleNamespace(store=store, backend=Backend()),
                    side=side,
                    master_window=None,
                    _directory_tracking_changes=lambda candidate, live: TermDeckWindow._directory_tracking_changes(
                        candidate, live
                    ),
                )

                def refresh_label(updated):
                    tab.record = updated
                    calls.append(("label", updated.name, updated.cwd))

                window.refresh_tab_label = refresh_label

                changed = TermDeckWindow.refresh_tab_directories(window)

                updated = store.get(record.id)
                self.assertEqual(changed, 1)
                self.assertEqual(calls[0], [record.id])
                self.assertEqual(calls[1], ("label", "new-project", str(new_directory.resolve())))
                self.assertEqual(updated.name, "new-project")  # type: ignore[union-attr]
                self.assertEqual(updated.cwd, str(new_directory.resolve()))  # type: ignore[union-attr]
                self.assertEqual(updated.last_opened_at, original_opened_at)  # type: ignore[union-attr]
                self.assertEqual(side.refresh_count, 1)
            finally:
                store.close()

    def test_close_snapshots_live_directory_before_detaching_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_directory = root / "old-project"
            new_directory = root / "new-project"
            old_directory.mkdir()
            new_directory.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create("old-project", "terminal", "bash", old_directory)
                order = []

                class Backend:
                    def current_directory(self, candidate):
                        order.append(("resolve", candidate.id))
                        return str(new_directory)

                def remove(session_id):
                    updated = store.get(session_id)
                    order.append(("detach", updated.cwd, updated.name))
                    return True

                window = SimpleNamespace(
                    tabs={record.id: SimpleNamespace(record=record)},
                    app_context=SimpleNamespace(store=store, backend=Backend()),
                    _remove_tab_widget=remove,
                    _renumber_visible_tabs=lambda: None,
                    _persist_live_directory_before_detach=lambda candidate: TermDeckWindow._persist_live_directory_before_detach(
                        window, candidate
                    ),
                    _directory_tracking_changes=lambda candidate, live: TermDeckWindow._directory_tracking_changes(
                        candidate, live
                    ),
                    refresh_tab_label=lambda updated: order.append(("label", updated.cwd)),
                    side=SideStub(),
                )

                TermDeckWindow.hide_tab(window, record.id, include_pinned=True)

                updated = store.get(record.id)
                self.assertEqual(updated.cwd, str(new_directory.resolve()))  # type: ignore[union-attr]
                self.assertEqual(updated.name, "new-project")  # type: ignore[union-attr]
                self.assertFalse(updated.visible)  # type: ignore[union-attr]
                self.assertEqual(order[0], ("resolve", record.id))
                self.assertEqual(
                    order[2],
                    ("detach", str(new_directory.resolve()), "new-project"),
                )
            finally:
                store.close()

    def test_user_open_replaces_missing_session_with_one_shell_at_saved_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "saved-project"
            directory.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create(
                    "ended external",
                    "terminal",
                    "sentinel-command-must-not-run",
                    directory,
                )
                record = store.update(
                    record.id,
                    status="running",
                    visible=True,
                    external=True,
                )
                opened = []
                toasts = []
                window = SimpleNamespace(
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(session_exists=lambda candidate: False),
                    ),
                    tabs={},
                    new_shell_tab=lambda cwd: opened.append(cwd),
                    show_toast=lambda message, **kwargs: toasts.append((message, kwargs)),
                    side=SideStub(),
                    master_window=None,
                    _remove_tab_widget=lambda _session_id: False,
                )

                result = TermDeckWindow.open_user_session(window, record)

                ended = store.get(record.id)
                self.assertTrue(result)
                self.assertEqual(opened, [str(directory.resolve())])
                self.assertEqual(ended.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(ended.visible)  # type: ignore[union-attr]
                self.assertIsNone(ended.last_pid)  # type: ignore[union-attr]
                self.assertEqual(len(toasts), 1)
                self.assertIn("opened a new shell", toasts[0][0])
            finally:
                store.close()

    def test_missing_attach_can_be_reopened_once_at_saved_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "saved-project"
            directory.mkdir()
            store = SessionStore(root / "sessions.sqlite3")
            try:
                record = store.create(
                    "ended shell",
                    "terminal",
                    "sentinel-command-must-not-run",
                    directory,
                )
                record = store.update(
                    record.id,
                    status="running",
                    visible=True,
                    external=True,
                )
                opened: list[str] = []
                removed: list[str] = []
                side = SideStub()
                window = SimpleNamespace(
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(session_exists=lambda _record: False),
                    ),
                    tabs={},
                    _missing_session_replacements=set(),
                    new_shell_tab=lambda cwd: opened.append(cwd),
                    show_toast=lambda *_args, **_kwargs: None,
                    side=side,
                    master_window=None,
                    _remove_tab_widget=lambda session_id: removed.append(session_id),
                    _renumber_visible_tabs=lambda: None,
                )
                window.open_user_session = lambda candidate: TermDeckWindow.open_user_session(
                    window, candidate
                )

                self.assertFalse(
                    TermDeckWindow._finalize_missing_attach_target(window, record.id)
                )
                missing = store.get(record.id)
                self.assertEqual(missing.status, "missing")  # type: ignore[union-attr]
                self.assertFalse(missing.visible)  # type: ignore[union-attr]
                self.assertEqual(
                    [candidate.id for candidate in recently_closed_records(store)],
                    [record.id],
                )

                self.assertEqual(TermDeckWindow.reopen_last_closed_tab(window), 1)

                ended = store.get(record.id)
                self.assertEqual(opened, [str(directory.resolve())])
                self.assertEqual(ended.status, "stopped")  # type: ignore[union-attr]
                self.assertFalse(ended.visible)  # type: ignore[union-attr]
                self.assertEqual(recently_closed_records(store), [])

                killed = store.create("killed", "terminal", "bash", directory, visible=False)
                store.update(killed.id, status="stopped", visible=False)
                self.assertEqual(recently_closed_records(store), [])
            finally:
                store.close()

    def test_user_open_replacement_failure_preserves_original_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("ended", "terminal", "do-not-replay", tmp)
                record = store.update(
                    record.id,
                    status="running",
                    visible=True,
                    external=True,
                    last_pid=919,
                )
                removed = []
                toasts = []
                window = SimpleNamespace(
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(session_exists=lambda _record: False),
                    ),
                    _missing_session_replacements=set(),
                    new_shell_tab=lambda _cwd: (_ for _ in ()).throw(TmuxError("tmux offline")),
                    show_toast=lambda message, **kwargs: toasts.append((message, kwargs)),
                    side=SideStub(),
                    _remove_tab_widget=removed.append,
                )

                result = TermDeckWindow.open_user_session(window, record)

                unchanged = store.get(record.id)
                self.assertFalse(result)
                self.assertEqual(unchanged.status, "running")  # type: ignore[union-attr]
                self.assertTrue(unchanged.visible)  # type: ignore[union-attr]
                self.assertEqual(unchanged.last_pid, 919)  # type: ignore[union-attr]
                self.assertEqual(removed, [])
                self.assertEqual(window._missing_session_replacements, set())
                self.assertIn("tmux offline", toasts[0][0])
            finally:
                store.close()

    def test_repeated_stale_open_creates_only_one_replacement_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                stale = store.create("ended", "terminal", "do-not-replay", tmp)
                stale = store.update(stale.id, status="running", external=True)
                opened = []
                removed = []
                window = SimpleNamespace(
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(session_exists=lambda _record: False),
                    ),
                    _missing_session_replacements=set(),
                    new_shell_tab=lambda cwd: opened.append(cwd),
                    show_toast=lambda *_args, **_kwargs: None,
                    side=SideStub(),
                    _remove_tab_widget=removed.append,
                )

                self.assertTrue(TermDeckWindow.open_user_session(window, stale))
                self.assertTrue(TermDeckWindow.open_user_session(window, stale))

                self.assertEqual(opened, [str(Path(tmp).resolve())])
                self.assertEqual(removed, [stale.id])
                self.assertEqual(store.get(stale.id).status, "stopped")  # type: ignore[union-attr]
            finally:
                store.close()

    def test_missing_managed_recovery_uses_exact_resume_path_not_blank_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("recover", "ai", "codex", tmp)
                record = store.update(
                    record.id,
                    status="recovery-pending",
                    recovery_command="codex --yolo resume 019f9cd7-5cbb-7600-80c4-8cacdf88deb7",
                    recovery_cwd=tmp,
                )
                created = []
                opened = []
                removed = []

                class Backend:
                    def session_exists(self, _record):
                        return False

                    def create_or_resume(self, candidate):
                        created.append(candidate)

                window = SimpleNamespace(
                    app_context=SimpleNamespace(store=store, backend=Backend()),
                    open_session=lambda candidate, **kwargs: opened.append((candidate, kwargs)),
                    new_shell_tab=lambda _cwd: self.fail("exact recovery must not become a blank shell"),
                    _remove_tab_widget=removed.append,
                    show_toast=lambda *_args, **_kwargs: None,
                    side=SideStub(),
                )

                result = TermDeckWindow.open_user_session(window, record)

                self.assertTrue(result)
                self.assertEqual([candidate.id for candidate in created], [record.id])
                self.assertEqual(created[0].status, "recovery-pending")
                self.assertEqual(opened[0][1]["known_live"], True)
                self.assertEqual(removed, [record.id])
            finally:
                store.close()

    def test_failed_managed_exact_recovery_preserves_record_and_surfaces_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("recover", "ai", "codex", tmp)
                record = store.update(
                    record.id,
                    status="stopped",
                    visible=True,
                    recovery_command="codex --yolo resume 019f9cd7-5cbb-7600-80c4-8cacdf88deb7",
                    recovery_cwd=tmp,
                )
                toasts = []
                window = SimpleNamespace(
                    app_context=SimpleNamespace(
                        store=store,
                        backend=SimpleNamespace(
                            session_exists=lambda _record: False,
                            create_or_resume=lambda _record: (_ for _ in ()).throw(
                                TmuxError("archive missing")
                            ),
                        ),
                    ),
                    open_session=lambda *_args, **_kwargs: self.fail("failed recovery must not open"),
                    new_shell_tab=lambda _cwd: self.fail("failed recovery must not become a shell"),
                    _remove_tab_widget=lambda _session_id: self.fail("failed recovery stays actionable"),
                    show_toast=lambda message, **kwargs: toasts.append((message, kwargs)),
                    side=SideStub(),
                )

                result = TermDeckWindow.open_user_session(window, record)

                unchanged = store.get(record.id)
                self.assertFalse(result)
                self.assertEqual(unchanged.status, "stopped")  # type: ignore[union-attr]
                self.assertTrue(unchanged.visible)  # type: ignore[union-attr]
                self.assertEqual(unchanged.recovery_command, record.recovery_command)  # type: ignore[union-attr]
                self.assertIn("archive missing", toasts[0][0])
            finally:
                store.close()

    def test_first_new_tab_defers_payload_until_its_vte_grid_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            opened = []

            class Backend:
                def create_or_resume_at_grid(self, *_args, **_kwargs):
                    self.fail("empty-window payload must wait for VTE allocation")

                def pane_pid(self, _record):
                    self.fail("empty-window payload has no pane before allocation")

            backend = Backend()
            backend.fail = self.fail
            side = SideStub()
            notebook = SimpleNamespace(
                get_n_pages=lambda: 0,
                get_current_page=lambda: -1,
                get_nth_page=lambda _page: None,
            )
            window = SimpleNamespace(
                app_context=SimpleNamespace(store=store, backend=backend),
                window_id="win",
                window_title="Win",
                notebook=notebook,
                open_session=lambda candidate, **kwargs: opened.append((candidate, kwargs)),
                _remove_tab_widget=lambda _session_id: False,
                side=side,
            )
            try:
                record = TermDeckWindow.create_session(
                    window,
                    "first",
                    "terminal",
                    "codex --yolo",
                    tmp,
                )

                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0][0].id, record.id)
                self.assertFalse(opened[0][1]["known_live"])
                self.assertTrue(opened[0][1]["known_pid_resolved"])
                self.assertTrue(opened[0][1]["rollback_new_record_on_failure"])
                self.assertEqual(store.get(record.id).status, "new")  # type: ignore[union-attr]
                self.assertEqual(side.refresh_count, 1)
            finally:
                store.close()

    def test_deferred_start_callbacks_resolve_the_live_store_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = store.create("first", "terminal", "codex --yolo", tmp)
            captured = {}
            restored = []

            class Backend:
                history_limit = 1000

                def attach_command(self, _record):
                    return ["tmux", "attach"]

                def create_or_resume_at_grid(self, _record, *, columns, rows):
                    self.grid = (columns, rows)

                def configure_interactive_session(self, _record):
                    return None

                def pane_pid(self, _record):
                    return 4242

                def restore_live_session(self, candidate):
                    restored.append(candidate)
                    return True

            backend = Backend()

            class FakeNotebook:
                def get_n_pages(self):
                    return 0

                def append_page(self, _terminal, _label):
                    return 0

                def set_tab_reorderable(self, *_args):
                    return None

                def set_current_page(self, _page):
                    return None

            class FakeAttach:
                def __init__(self, *_args, **kwargs):
                    captured.update(kwargs)
                    self.terminal = SimpleNamespace(grab_focus=lambda: None)

                def show_all(self):
                    return None

            side = SideStub()
            window = SimpleNamespace(
                app_context=SimpleNamespace(
                    store=store,
                    backend=backend,
                    theme=SimpleNamespace(scrollback_lines=1000),
                    sampler=SimpleNamespace(snapshot=lambda _pid: None),
                ),
                window_id="win",
                window_title="Win",
                tabs={},
                notebook=FakeNotebook(),
                side=side,
                observed_status=lambda candidate, pid: "running" if pid else candidate.status,
                on_terminal_key_press=lambda *_args: None,
                _restore_live_terminal=lambda candidate: backend.restore_live_session(candidate),
                _scroll_terminal=lambda *_args: None,
                _selection_history_for_terminal=lambda *_args: "",
                _attach_client_should_retry=lambda *_args: False,
                _on_auth_attach_stable=lambda *_args: None,
                zoomed_font=lambda: None,
                _build_tab_label=lambda _record: object(),
            )
            try:
                with mock.patch("termdeck.gtk_app.AttachTerminal", FakeAttach):
                    TermDeckWindow.open_session(
                        window,
                        record,
                        known_live=False,
                        known_pid_resolved=True,
                    )

                self.assertTrue(captured["prepare_target_handler"](132, 43))
                self.assertEqual(backend.grid, (132, 43))
                updated = store.get(record.id)
                self.assertIsNotNone(updated)
                self.assertEqual(updated.status, "running")  # type: ignore[union-attr]
                self.assertEqual(updated.last_pid, 4242)  # type: ignore[union-attr]
                self.assertTrue(captured["live_focus_handler"]())
                self.assertEqual(restored[-1].status, "running")
                self.assertEqual(restored[-1].last_pid, 4242)
            finally:
                store.close()

    def test_async_first_tab_failure_rolls_back_row_and_partial_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            record = store.create("first", "terminal", "codex --yolo", tmp)
            terminated = []
            toasts = []
            captured = {}

            class Backend:
                history_limit = 1000

                def attach_command(self, _record):
                    return ["tmux", "attach"]

                def create_or_resume_at_grid(self, *_args, **_kwargs):
                    return None

                def terminate(self, candidate):
                    terminated.append(candidate.id)

            class FakeNotebook:
                def get_n_pages(self):
                    return 0

                def append_page(self, _terminal, _label):
                    return 0

                def set_tab_reorderable(self, *_args):
                    return None

                def set_current_page(self, _page):
                    return None

            class FakeAttach:
                def __init__(self, *_args, **kwargs):
                    captured.update(kwargs)
                    self.terminal = SimpleNamespace(grab_focus=lambda: None)

                def show_all(self):
                    return None

            side = SideStub()
            window = SimpleNamespace(
                app_context=SimpleNamespace(
                    store=store,
                    backend=Backend(),
                    theme=SimpleNamespace(scrollback_lines=1000),
                    sampler=SimpleNamespace(snapshot=lambda _pid: None),
                ),
                window_id="win",
                window_title="Win",
                tabs={},
                notebook=FakeNotebook(),
                side=side,
                observed_status=lambda _record, _pid: "running",
                on_terminal_key_press=lambda *_args: None,
                _restore_live_terminal=lambda *_args: True,
                _scroll_terminal=lambda *_args: None,
                _selection_history_for_terminal=lambda *_args: "",
                _attach_client_should_retry=lambda *_args: False,
                _on_auth_attach_stable=lambda *_args: None,
                zoomed_font=lambda: None,
                _build_tab_label=lambda _record: object(),
                _remove_tab_widget=lambda session_id: window.tabs.pop(session_id, None),
                show_toast=lambda message, **kwargs: toasts.append((message, kwargs)),
            )
            try:
                with mock.patch("termdeck.gtk_app.AttachTerminal", FakeAttach), mock.patch(
                    "termdeck.gtk_app.GLib.idle_add",
                    side_effect=lambda callback, *args: callback(*args),
                ):
                    TermDeckWindow.open_session(
                        window,
                        record,
                        known_live=False,
                        known_pid_resolved=True,
                        rollback_new_record_on_failure=True,
                    )
                    captured["prepare_target_error_handler"](RuntimeError("launch failed"))

                self.assertIsNone(store.get(record.id))
                self.assertEqual(terminated, [record.id])
                self.assertNotIn(record.id, window.tabs)
                self.assertIn("launch failed", toasts[-1][0])
            finally:
                store.close()

    def test_failed_new_session_rolls_back_its_row_and_partial_tmux_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            terminated = []

            class Backend:
                def create_or_resume(self, _record):
                    return None

                def pane_pid(self, _record):
                    raise TmuxError("pane unavailable")

                def terminate(self, candidate):
                    terminated.append(candidate.id)

            window = SimpleNamespace(
                app_context=SimpleNamespace(store=store, backend=Backend()),
                window_id="win",
                window_title="Win",
                notebook=SimpleNamespace(get_n_pages=lambda: 0),
                _remove_tab_widget=lambda _session_id: False,
            )

            try:
                with self.assertRaisesRegex(TmuxError, "pane unavailable"):
                    TermDeckWindow.create_session(window, "replacement", "terminal", "bash", tmp)
                self.assertEqual(store.list(), [])
                self.assertEqual(len(terminated), 1)
            finally:
                store.close()

    def test_user_open_reattaches_live_session_without_replacement(self) -> None:
        record = SimpleNamespace(id="live", cwd="/tmp")
        opened = []
        replacements = []
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                store=SimpleNamespace(get=lambda _session_id: record),
                backend=SimpleNamespace(session_exists=lambda candidate: True),
            ),
            tabs={},
            open_session=lambda candidate, **kwargs: opened.append((candidate, kwargs)),
            new_shell_tab=lambda cwd: replacements.append(cwd),
        )

        result = TermDeckWindow.open_user_session(window, record)

        self.assertTrue(result)
        self.assertEqual(replacements, [])
        self.assertEqual(opened[0][0], record)
        self.assertEqual(opened[0][1]["known_live"], True)

    def test_tab_metadata_refresh_persists_and_preserves_safe_recovery_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                first = store.create("first", "terminal", "/bin/bash", tmp)
                second = store.create("second", "terminal", "/bin/bash", tmp)
                second = store.update(
                    second.id,
                    recovery_command="codex --yolo",
                    recovery_cwd=str(Path(tmp) / "old-repo"),
                )
                first_recovery_cwd = str(Path(tmp) / "first-repo")

                class Backend:
                    def current_directories(self, records):
                        return {record.id: record.cwd for record in records}

                    def recovery_metadata(self, records):
                        return {
                            first.id: ("codex --yolo", first_recovery_cwd),
                            second.id: (None, None),
                        }

                side = SideStub()
                window = SimpleNamespace(
                    tabs={
                        first.id: SimpleNamespace(record=first),
                        second.id: SimpleNamespace(record=second),
                    },
                    app_context=SimpleNamespace(store=store, backend=Backend()),
                    side=side,
                    master_window=None,
                    _directory_tracking_changes=lambda candidate, live: {},
                    refresh_tab_label=lambda _record: None,
                )

                changed = TermDeckWindow.refresh_tab_directories(window)

                self.assertEqual(changed, 1)
                self.assertEqual(store.get(first.id).recovery_command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).recovery_cwd, first_recovery_cwd)  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).recovery_command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(store.get(second.id).recovery_cwd, str(Path(tmp) / "old-repo"))  # type: ignore[union-attr]
                self.assertEqual(side.refresh_count, 1)
            finally:
                store.close()

    def test_new_tab_opens_shell_in_home_directory(self) -> None:
        calls = []
        window = SimpleNamespace(
            new_shell_tab=lambda cwd: calls.append(cwd),
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            from unittest.mock import patch

            with patch("termdeck.gtk_app.Path.home", return_value=home):
                TermDeckWindow.new_tab(window)

        self.assertEqual(calls, [home])

    def test_legacy_new_directory_tab_alias_uses_direct_new_tab(self) -> None:
        calls = []
        window = SimpleNamespace(new_tab=lambda: calls.append("new-tab"))

        TermDeckWindow.new_directory_tab(window)

        self.assertEqual(calls, ["new-tab"])

    def test_set_window_name_updates_title_and_loaded_tab_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create("api", "terminal", "bash", tmp, window_id="win", window_title="Old")
                tab = SimpleNamespace(record=record)
                window = SimpleNamespace(
                    window_id="win",
                    window_title="Old",
                    window_title_label=LabelStub(),
                    app_context=SimpleNamespace(store=store),
                    tabs={record.id: tab},
                    side=SideStub(),
                    set_title=lambda title: setattr(window, "native_title", title),
                    show_toast=lambda message, kind=None: setattr(window, "toast", (message, kind)),
                )

                TermDeckWindow.set_window_name(window, "Backend")

                self.assertEqual(window.window_title, "Backend")
                self.assertEqual(window.window_title_label.text, "Backend")
                self.assertEqual(window.native_title, "T1000 · Backend")
                self.assertEqual(tab.record.window_title, "Backend")
                self.assertEqual(window.side.refresh_count, 1)
            finally:
                store.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
