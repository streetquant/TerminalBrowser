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

    def paste_clipboard(self) -> None:
        self.calls.append("paste_clipboard")

    def paste_primary(self) -> None:
        self.calls.append("paste_primary")


class ClipboardStub:
    def __init__(self, text: str | bytes | None) -> None:
        self.text = text
        self.requests = []

    def request_text(self, callback, *_args) -> None:
        self.requests.append(callback)

    def deliver(self) -> None:
        callback = self.requests.pop(0)
        callback(self, self.text, None)


def make_attach(clips: dict[object, str], *, last_text: str = "", has_selection: bool = False):
    terminal = TerminalStub(has_selection=has_selection)
    attach = SimpleNamespace(terminal=terminal, _last_copied_text=last_text)
    clipboards = {selection: ClipboardStub(text) for selection, text in clips.items()}
    attach._system_clipboard = lambda selection: clipboards.get(selection)
    attach._feed_text = lambda text: AttachTerminal._feed_text(attach, text)
    return attach, terminal, clipboards


class TerminalPastePipelineTest(TestCase):
    def test_clipboard_text_feeds_child_binary_and_clears_selection(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: "hello"},
            has_selection=True,
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=True)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()
        self.assertEqual(terminal.calls, ["unselect_all", ("feed_child_binary", b"hello")])

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
        clipboards[gtk_app.Gdk.SELECTION_PRIMARY].deliver()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()
        self.assertEqual(terminal.calls, [("feed_child_binary", b"from-clipboard")])

    def test_last_select_to_copy_payload_is_used_when_clipboard_is_empty(self) -> None:
        attach, terminal, clipboards = make_attach(
            {gtk_app.Gdk.SELECTION_CLIPBOARD: ""},
            last_text="last-copy",
        )

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=True)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()
        self.assertEqual(terminal.calls, [("feed_child_binary", b"last-copy")])

    def test_clipboard_paste_falls_back_to_vte_when_no_text_is_available(self) -> None:
        attach, terminal, clipboards = make_attach({gtk_app.Gdk.SELECTION_CLIPBOARD: ""})

        pasted = AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD, prefer_last=False)

        self.assertTrue(pasted)
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()
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
        clipboards[gtk_app.Gdk.SELECTION_PRIMARY].deliver()
        self.assertEqual(terminal.calls, [])
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()
        self.assertEqual(terminal.calls, ["paste_primary"])

    def test_detached_terminal_ignores_late_clipboard_callback(self) -> None:
        attach, terminal, clipboards = make_attach({gtk_app.Gdk.SELECTION_CLIPBOARD: "late"})

        AttachTerminal._paste_from_selection(attach, gtk_app.Gdk.SELECTION_CLIPBOARD)
        attach._detached = True
        clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].deliver()

        self.assertEqual(terminal.calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
