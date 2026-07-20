import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from termdeck.command_render import directory_tab_name, normalise_directory
from termdeck.gtk_app import TermDeckWindow
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
        self.assertEqual(side.refresh_count, 1)

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

    def test_tab_metadata_refresh_persists_and_clears_safe_recovery_commands(self) -> None:
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

                self.assertEqual(changed, 2)
                self.assertEqual(store.get(first.id).recovery_command, "codex --yolo")  # type: ignore[union-attr]
                self.assertEqual(store.get(first.id).recovery_cwd, first_recovery_cwd)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_command)  # type: ignore[union-attr]
                self.assertIsNone(store.get(second.id).recovery_cwd)  # type: ignore[union-attr]
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
                self.assertEqual(window.native_title, "TermDeck · Backend")
                self.assertEqual(tab.record.window_title, "Backend")
                self.assertEqual(window.side.refresh_count, 1)
            finally:
                store.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
