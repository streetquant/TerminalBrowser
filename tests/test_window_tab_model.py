import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck import gtk_app
from termdeck.command_render import directory_tab_name, normalise_directory
from termdeck.gtk_app import TermDeckWindow
from termdeck.session_store import SessionStore


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


class DirectoryChooserStub:
    response = gtk_app.Gtk.ResponseType.OK
    filename: str | None = None
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.current_folder = None
        self.destroyed = False
        DirectoryChooserStub.last_instance = self

    def add_buttons(self, *_args) -> None:
        return None

    def set_modal(self, _modal: bool) -> None:
        return None

    def set_current_folder(self, folder: str) -> None:
        self.current_folder = folder

    def run(self):
        return self.response

    def get_filename(self):
        return self.filename

    def destroy(self) -> None:
        self.destroyed = True


class WindowTabModelTests(TestCase):
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

    def test_directory_chooser_returns_normalised_folder_and_starts_from_active_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp) / "client-api"
            selected.mkdir()
            DirectoryChooserStub.response = gtk_app.Gtk.ResponseType.OK
            DirectoryChooserStub.filename = str(selected)
            window = SimpleNamespace(_active_directory=lambda: tmp)

            with patch("termdeck.gtk_app.Gtk.FileChooserDialog", DirectoryChooserStub):
                result = TermDeckWindow.choose_tab_directory(window)

            chooser = DirectoryChooserStub.last_instance
            self.assertEqual(result, str(selected.resolve()))
            self.assertEqual(chooser.current_folder, tmp)
            self.assertEqual(chooser.kwargs["action"], gtk_app.Gtk.FileChooserAction.SELECT_FOLDER)
            self.assertTrue(chooser.destroyed)

    def test_directory_chooser_cancel_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            DirectoryChooserStub.response = gtk_app.Gtk.ResponseType.CANCEL
            DirectoryChooserStub.filename = tmp
            window = SimpleNamespace(_active_directory=lambda: tmp)

            with patch("termdeck.gtk_app.Gtk.FileChooserDialog", DirectoryChooserStub):
                result = TermDeckWindow.choose_tab_directory(window)

            self.assertIsNone(result)
            self.assertTrue(DirectoryChooserStub.last_instance.destroyed)

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
