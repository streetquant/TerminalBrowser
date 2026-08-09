from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from termdeck.gtk_app import Gtk, TermDeckApplication, TermDeckWindow


class FakeWindow:
    created: list["FakeWindow"] = []

    def __init__(self, app, **kwargs) -> None:
        self.app = app
        self.kwargs = kwargs
        self.window_id = kwargs.get("window_id")
        self.tabs = {
            record.id: record
            for record in (kwargs.get("restore_records") or [])
        }
        self.calls: list[str] = []
        FakeWindow.created.append(self)

    def get_realized(self) -> bool:
        return True

    def show_all(self) -> None:
        self.calls.append("show")

    def present(self) -> None:
        self.calls.append("present")

    def restore_previous_tabs(self, records=None) -> int:
        rows = list(records or [])
        self.calls.append(f"restore:{len(rows)}")
        self.tabs.update({record.id: record for record in rows})
        return len(rows)

    def new_shell_tab(self, cwd) -> None:
        self.calls.append(f"shell:{Path(cwd)}")


class FakeNameDialog:
    response = Gtk.ResponseType.OK
    supplied_value = "Backend"
    created: list["FakeNameDialog"] = []

    def __init__(self, parent, **kwargs) -> None:
        self.parent = parent
        self.kwargs = kwargs
        self.application = None
        self.destroyed = False
        FakeNameDialog.created.append(self)

    def set_application(self, application) -> None:
        self.application = application

    def run(self):
        return self.response

    def value(self) -> str:
        return self.supplied_value

    def destroy(self) -> None:
        self.destroyed = True


class FirstRunWindowFlowTests(TestCase):
    def setUp(self) -> None:
        FakeWindow.created = []
        FakeNameDialog.created = []
        FakeNameDialog.response = Gtk.ResponseType.OK
        FakeNameDialog.supplied_value = "Backend"

    def test_create_named_window_presents_then_opens_initial_shell(self) -> None:
        app = SimpleNamespace(windows=[])
        initial = Path("/tmp/client-work")

        with patch("termdeck.gtk_app.TermDeckWindow", FakeWindow):
            created = TermDeckApplication.create_named_window(
                app,
                "  Client work  ",
                initial_directory=initial,
            )

        self.assertIs(created, FakeWindow.created[0])
        self.assertEqual(created.kwargs["window_title"], "Client work")
        self.assertEqual(created.kwargs["restore_records"], [])
        self.assertEqual(created.calls, ["present", f"shell:{initial}"])
        self.assertEqual(app.windows, [created])

    def test_create_named_window_can_skip_initial_shell_for_noninteractive_restore(self) -> None:
        app = SimpleNamespace(windows=[])

        with patch("termdeck.gtk_app.TermDeckWindow", FakeWindow):
            created = TermDeckApplication.create_named_window(
                app,
                "Restored",
                open_initial_tab=False,
            )

        self.assertEqual(created.calls, ["present"])

    def test_first_run_prompt_names_window_and_opens_home_shell(self) -> None:
        created_window = object()
        create_named_window = Mock(return_value=created_window)
        app = SimpleNamespace(windows=[], create_named_window=create_named_window)

        home = Path("/tmp/test-home")
        with patch("termdeck.gtk_app.WindowNameDialog", FakeNameDialog), patch(
            "termdeck.gtk_app.Path.home", return_value=home
        ):
            result = TermDeckApplication.prompt_for_new_window(app, first_run=True)

        dialog = FakeNameDialog.created[0]
        self.assertIs(result, created_window)
        self.assertEqual(dialog.kwargs["title"], "Create your first TermDeck window")
        self.assertEqual(dialog.kwargs["initial"], "Window 1")
        self.assertEqual(dialog.kwargs["confirm_label"], "Create window")
        self.assertIn("opens immediately", dialog.kwargs["description"])
        self.assertIn("use cd", dialog.kwargs["description"])
        self.assertIs(dialog.application, app)
        self.assertTrue(dialog.destroyed)
        create_named_window.assert_called_once_with(
            "Backend",
            initial_directory=home,
            open_initial_tab=True,
        )

    def test_additional_window_opens_shell_in_home_not_parent_directory(self) -> None:
        created_window = object()
        create_named_window = Mock(return_value=created_window)
        parent = SimpleNamespace(_active_directory=lambda: "/work/live")
        app = SimpleNamespace(windows=[parent], create_named_window=create_named_window)
        home = Path("/tmp/test-home")

        with patch("termdeck.gtk_app.WindowNameDialog", FakeNameDialog), patch(
            "termdeck.gtk_app.Path.home", return_value=home
        ):
            result = TermDeckApplication.prompt_for_new_window(app, parent=parent)

        self.assertIs(result, created_window)
        create_named_window.assert_called_once_with(
            "Backend",
            initial_directory=home,
            open_initial_tab=True,
        )

    def test_move_destination_prompt_creates_an_empty_window_for_the_moved_tab(self) -> None:
        created_window = object()
        create_named_window = Mock(return_value=created_window)
        parent = SimpleNamespace()
        app = SimpleNamespace(windows=[parent], create_named_window=create_named_window)
        home = Path("/tmp/test-home")

        with patch("termdeck.gtk_app.WindowNameDialog", FakeNameDialog), patch(
            "termdeck.gtk_app.Path.home", return_value=home
        ):
            result = TermDeckApplication.prompt_for_new_window(
                app,
                parent=parent,
                open_initial_tab=False,
            )

        self.assertIs(result, created_window)
        self.assertNotIn("first shell", FakeNameDialog.created[0].kwargs["description"])
        create_named_window.assert_called_once_with(
            "Backend",
            initial_directory=home,
            open_initial_tab=False,
        )

    def test_cancelled_first_run_prompt_creates_nothing(self) -> None:
        FakeNameDialog.response = Gtk.ResponseType.CANCEL
        create_named_window = Mock()
        app = SimpleNamespace(windows=[], create_named_window=create_named_window)

        with patch("termdeck.gtk_app.WindowNameDialog", FakeNameDialog):
            result = TermDeckApplication.prompt_for_new_window(app, first_run=True)

        self.assertIsNone(result)
        create_named_window.assert_not_called()
        self.assertTrue(FakeNameDialog.created[0].destroyed)

    def test_new_window_action_reuses_application_creation_flow(self) -> None:
        prompt_for_new_window = Mock()
        app = SimpleNamespace(prompt_for_new_window=prompt_for_new_window)
        window = SimpleNamespace(app_context=app)

        TermDeckWindow.new_window(window)

        prompt_for_new_window.assert_called_once_with(parent=window)

    def test_fresh_activation_prompts_instead_of_constructing_main_window(self) -> None:
        prompt_for_new_window = Mock()
        store = SimpleNamespace(windows_with_visible_sessions=lambda: [])
        app = SimpleNamespace(
            backend=SimpleNamespace(available=True),
            sampler=object(),
            store=store,
            windows=[],
            workspace_path=None,
            prompt_for_new_window=prompt_for_new_window,
        )

        with patch("termdeck.gtk_app.reconcile_store") as reconcile, patch(
            "termdeck.gtk_app.TermDeckWindow"
        ) as window_class:
            TermDeckApplication.do_activate(app)

        reconcile.assert_called_once()
        self.assertTrue(reconcile.call_args.kwargs["recover_missing"])
        prompt_for_new_window.assert_called_once_with(first_run=True)
        window_class.assert_not_called()

    def test_activation_restores_and_presents_every_visible_window_group(self) -> None:
        agent_records = [
            SimpleNamespace(
                id=f"a{index}",
                pinned=False,
                last_opened_at=f"2026-07-17T23:00:{index:02d}+00:00",
                tab_index=index,
            )
            for index in range(30)
        ]
        ops_record = SimpleNamespace(
            id="b1",
            pinned=False,
            last_opened_at="2026-07-17T22:59:00+00:00",
            tab_index=0,
        )
        groups = [
            ("win-a", "Agents", agent_records),
            ("win-b", "Ops", [ops_record]),
        ]
        store = SimpleNamespace(windows_with_visible_sessions=lambda: groups)
        present_calls = []
        app = SimpleNamespace(
            backend=SimpleNamespace(available=True),
            sampler=object(),
            store=store,
            windows=[],
            workspace_path=None,
            prompt_for_new_window=Mock(),
            _present_all_windows=lambda: present_calls.append(list(app.windows)),
        )

        with patch("termdeck.gtk_app.reconcile_store"), patch(
            "termdeck.gtk_app.TermDeckWindow", FakeWindow
        ):
            TermDeckApplication.do_activate(app)

        self.assertEqual(len(app.windows), 2)
        self.assertEqual(app.windows[0].kwargs["window_id"], "win-a")
        self.assertEqual(app.windows[1].kwargs["window_id"], "win-b")
        self.assertEqual(app.windows[0].kwargs["window_title"], "Agents")
        self.assertEqual(app.windows[1].kwargs["window_title"], "Ops")
        self.assertEqual(app.windows[0].kwargs["restore_records"], agent_records)
        self.assertEqual(app.windows[1].kwargs["restore_records"], groups[1][2])
        self.assertEqual(len(present_calls), 1)
        self.assertEqual(present_calls[0], app.windows)
        app.prompt_for_new_window.assert_not_called()

    def test_reactivation_adds_only_missing_remembered_windows(self) -> None:
        groups = [
            (
                f"win-{index}",
                f"Window {index}",
                [SimpleNamespace(id=f"tab-{index}")],
            )
            for index in range(10)
        ]
        existing = [
            FakeWindow(
                None,
                window_id=window_id,
                window_title=title,
                restore_records=records,
            )
            for window_id, title, records in groups[:8]
        ]
        # Prove activation also fills a missing tab in an already-realized
        # window without duplicating that window.
        existing[0].tabs.clear()
        FakeWindow.created = []
        store = SimpleNamespace(windows_for_restore=lambda: groups)
        present_calls = []
        app = SimpleNamespace(
            backend=SimpleNamespace(available=True),
            sampler=object(),
            store=store,
            windows=existing,
            workspace_path=None,
            prompt_for_new_window=Mock(),
            _present_all_windows=lambda: present_calls.append(list(app.windows)),
        )

        with patch("termdeck.gtk_app.reconcile_store"), patch(
            "termdeck.gtk_app.TermDeckWindow", FakeWindow
        ):
            TermDeckApplication.do_activate(app)
            TermDeckApplication.do_activate(app)

        self.assertEqual({window.window_id for window in app.windows}, {row[0] for row in groups})
        self.assertEqual(len(app.windows), 10)
        self.assertEqual([window.window_id for window in FakeWindow.created], ["win-8", "win-9"])
        self.assertIn("restore:1", existing[0].calls)
        self.assertEqual(len(present_calls), 2)

    def test_one_window_restore_failure_does_not_suppress_later_windows(self) -> None:
        groups = [
            ("win-broken", "Broken", [SimpleNamespace(id="bad")]),
            ("win-good", "Good", [SimpleNamespace(id="good")]),
        ]
        store = SimpleNamespace(windows_for_restore=lambda: groups)
        orphan = SimpleNamespace(tabs={}, destroy=Mock())
        application_windows = []
        app = SimpleNamespace(
            backend=SimpleNamespace(available=True),
            sampler=object(),
            store=store,
            windows=[],
            workspace_path=None,
            prompt_for_new_window=Mock(),
            _present_all_windows=Mock(),
            get_windows=lambda: list(application_windows),
        )

        def build(_app, **kwargs):
            if kwargs["window_id"] == "win-broken":
                application_windows.append(orphan)
                raise RuntimeError("broken fixture")
            return FakeWindow(_app, **kwargs)

        with patch("termdeck.gtk_app.reconcile_store"), patch(
            "termdeck.gtk_app.TermDeckWindow", side_effect=build
        ):
            TermDeckApplication.do_activate(app)

        self.assertEqual([window.window_id for window in app.windows], ["win-good"])
        self.assertEqual(app._last_window_restore_errors[0][:2], ("win-broken", "Broken"))
        orphan.destroy.assert_called_once_with()
        app._present_all_windows.assert_called_once_with()

    def test_present_all_windows_raises_each_restored_window(self) -> None:
        first = FakeWindow(None)
        second = FakeWindow(None)
        app = SimpleNamespace(windows=[first, second])

        TermDeckApplication._present_all_windows(app)

        self.assertEqual(first.calls, ["present"])
        self.assertEqual(second.calls, ["present"])

    def test_shutdown_persistence_runs_once_and_reuses_the_result(self) -> None:
        expected = object()
        app = SimpleNamespace(
            store=object(),
            backend=object(),
            _shutdown_persistence_started=False,
            _last_persistence_result=None,
        )

        with patch(
            "termdeck.gtk_app.persist_live_sessions",
            return_value=expected,
        ) as persist:
            first = TermDeckApplication.persist_for_shutdown(app)
            second = TermDeckApplication.persist_for_shutdown(app)

        self.assertIs(first, expected)
        self.assertIs(second, expected)
        persist.assert_called_once_with(app.store, app.backend)


if __name__ == "__main__":
    import unittest

    unittest.main()
