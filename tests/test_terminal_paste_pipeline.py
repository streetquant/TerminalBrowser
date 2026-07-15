from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import AttachTerminal


class TerminalStub:
    def __init__(self, *, has_selection: bool = False) -> None:
        self.has_selection = has_selection
        self.calls = []

    def get_has_selection(self) -> bool:
        return self.has_selection

    def unselect_all(self) -> None:
        self.calls.append("unselect_all")
        self.has_selection = False

    def feed_child_binary(self, data: bytes) -> None:
        self.calls.append(("feed_child_binary", data))

    def feed_child(self, data: bytes) -> None:
        self.calls.append(("feed_child", data))

    def paste_text(self, text: str) -> None:
        self.calls.append(("paste_text", text))

    def paste_clipboard(self) -> None:
        self.calls.append("paste_clipboard")

    def paste_primary(self) -> None:
        self.calls.append("paste_primary")


class ClipboardStub:
    def __init__(self, text: str | bytes | None, image=None) -> None:
        self.text = text
        self.image = image
        self.text_requests = []
        self.image_requests = []

    def request_text(self, callback, *_args) -> None:
        self.text_requests.append(callback)

    def request_image(self, callback, *_args) -> None:
        self.image_requests.append(callback)

    def deliver_text(self) -> None:
        callback = self.text_requests.pop(0)
        callback(self, self.text, None)

    def deliver_image(self) -> None:
        callback = self.image_requests.pop(0)
        callback(self, self.image, None)


class ImageStub:
    def __init__(self, payload: bytes = b"png-image") -> None:
        self.payload = payload

    def savev(self, path: str, file_type: str, option_keys, option_values) -> bool:
        assert file_type == "png"
        Path(path).write_bytes(self.payload)
        return True


def make_attach(clips: dict[object, object], *, last_text: str = "", has_selection: bool = False):
    terminal = TerminalStub(has_selection=has_selection)
    attach = SimpleNamespace(
        terminal=terminal,
        _last_copied_text=last_text,
        _clipboard_image_paths=set(),
    )
    clipboards = {
        selection: ClipboardStub(
            value[0], value[1]
        ) if isinstance(value, tuple) else ClipboardStub(value)
        for selection, value in clips.items()
    }
    attach._system_clipboard = lambda selection: clipboards.get(selection)
    attach._feed_text = lambda text: AttachTerminal._feed_text(attach, text)
    attach._paste_clipboard_image = lambda image: AttachTerminal._paste_clipboard_image(attach, image)
    return attach, terminal, clipboards


class TerminalPastePipelineTest(TestCase):
    def test_clipboard_text_uses_vte_paste_handling_and_clears_selection(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: "hello"},
            has_selection=True,
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=True)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, ["unselect_all", ("paste_text", "hello")])

    def test_clipboard_newline_is_passed_to_vte_as_paste_not_raw_terminal_input(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: "printf hello\n"}
        )

        AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=True)
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()

        self.assertEqual(terminal.calls, [("paste_text", "printf hello\n")])

    def test_primary_paste_falls_back_to_clipboard_text(self) -> None:
        attach, terminal, clipboards = make_attach(
            {
                gtk_app.Gdk.SELECTION_PRIMARY: "",
                gtk_app.Gdk.SELECTION_CLIPBOARD: "from-clipboard",
            }
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_PRIMARY, prefer_last=False)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_PRIMARY].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, [("paste_text", "from-clipboard")])

    def test_last_select_to_copy_payload_is_used_when_clipboard_is_empty(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: ""},
            last_text="last-copy",
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=True)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_image()
        self.assertEqual(terminal.calls, [("paste_text", "last-copy")])

    def test_clipboard_image_is_saved_as_png_and_its_path_is_pasted(self) -> None:
        image = ImageStub()
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: ("", image)},
            last_text="stale-copy",
        )

        AttachTerminal._paste_from_selection(
            attach,
            gtk_app.Gdk.SELECTION_CLIPBOARD,
            prefer_last=True,
        )
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_image()

        self.assertEqual(len(attach._clipboard_image_paths), 1)
        image_path = Path(next(iter(attach._clipboard_image_paths)))
        try:
            self.assertEqual(image_path.suffix, ".png")
            self.assertEqual(image_path.read_bytes(), image.payload)
            self.assertEqual(terminal.calls, [("paste_text", str(image_path))])
            AttachTerminal._cleanup_clipboard_images(attach)
            self.assertFalse(image_path.exists())
            self.assertEqual(attach._clipboard_image_paths, set())
        finally:
            image_path.unlink(missing_ok=True)

    def test_text_clipboard_does_not_request_or_materialise_an_image(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: ("hello", ImageStub())}
        )

        AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD)
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()

        self.assertEqual(terminal.calls, [("paste_text", "hello")])
        self.assertEqual(clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].image_requests, [])
        self.assertEqual(attach._clipboard_image_paths, set())

    def test_clipboard_paste_falls_back_to_vte_when_no_text_is_available(self) -> None:
        attach, terminal, clipboards = make_attach({gtk_app.Gdk.SELECTION_CLIPBOARD: ""})

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=False)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_image()
        self.assertEqual(terminal.calls, ["paste_clipboard"])

    def test_primary_paste_falls_back_to_vte_primary_when_no_text_is_available(self) -> None:
        attach, terminal, clipboards = make_attach(
            {
                gtk_app.Gdk.SELECTION_PRIMARY: "",
                gtk_app.Gdk.SELECTION_CLIPBOARD: "",
            }
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_PRIMARY, prefer_last=False)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_PRIMARY].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_image()
        self.assertEqual(terminal.calls, ["paste_primary"])

    def test_detached_terminal_ignores_late_clipboard_callback(self) -> None:
        attach, terminal, clipboards = make_attach({gtk_app.Gdk.SELECTION_CLIPBOARD: "late"})

        AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD)
        attach._detached = True
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver_text()

        self.assertEqual(terminal.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
