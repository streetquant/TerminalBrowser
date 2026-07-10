from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from termdeck.gtk_app import Gtk, TermDeckApplication, TermDeckWindow


class FakeWindow:
    created: list["FakeWindow"] = []

    def __init__(self, app, **kwargs) -> None:
        self.app = app
        self.kwargs = kwargs
        self.calls: list[str] = []
        FakeWindow.created.append(self)

    def show_all(self) -> None:
        self.calls.append("show")

    def present(self) -> None:
        self.calls.append("present")

    def new_directory_tab(self) -> None:
        self.calls.append("choose-directory")


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

    def test_create_named_window_presents_then_chooses_first_directory(self) -> None:
        app = SimpleNamespace(windows=[])

        with patch("termdeck.gtk_app.TermDeckWindow", FakeWindow):
            created = TermDeckApplication.create_named_window(app, "  Client work  ")

        self.assertIs(created, FakeWindow.created[0])
        self.assertEqual(created.kwargs["window_title"], "Client work")
        self.assertEqual(created.kwargs["restore_records"], [])
        self.assertEqual(created.calls, ["present", "choose-directory"])
        self.assertEqual(app.windows, [created])

    def test_create_named_window_can_skip_directory_prompt_for_noninteractive_restore(self) -> None:
        app = SimpleNamespace(windows=[])

        with patch("termdeck.gtk_app.TermDeckWindow", FakeWindow):
            created = TermDeckApplication.create_named_window(
                app,
                "Restored",
                choose_first_directory=False,
            )

        self.assertEqual(created.calls, ["present"])

    def test_first_run_prompt_names_window_and_requests_first_directory(self) -> None:
        created_window = object()
        create_named_window = Mock(return_value=created_window)
        app = SimpleNamespace(windows=[], create_named_window=create_named_window)

        with patch("termdeck.gtk_app.WindowNameDialog", FakeNameDialog):
            result = TermDeckApplication.prompt_for_new_window(app, first_run=True)

        dialog = FakeNameDialog.created[0]
        self.assertIs(result, created_window)
        self.assertEqual(dialog.kwargs["title"], "Create your first TermDeck window")
        self.assertEqual(dialog.kwargs["initial"], "Window 1")
        self.assertEqual(dialog.kwargs["confirm_label"], "Create window")
        self.assertIn("directory tabs", dialog.kwargs["description"])
        self.assertIs(dialog.application, app)
        self.assertTrue(dialog.destroyed)
        create_named_window.assert_called_once_with("Backend", choose_first_directory=True)

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
        prompt_for_new_window.assert_called_once_with(first_run=True)
        window_class.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
