from types import SimpleNamespace
from unittest import TestCase

from termdeck import gtk_app
from termdeck.gtk_app import AttachTerminal


class ClipboardStub:
    def __init__(self) -> None:
        self.text = ''
        self.stored = False

    def set_text(self, text: str, length: int) -> None:
        self.text = text
        self.length = length

    def store(self) -> None:
        self.stored = True


class TerminalStub:
    def __init__(self, *, selected_text='', has_selection=True) -> None:
        self.selected_text = selected_text
        self.has_selection = has_selection
        self.calls = []

    def get_has_selection(self) -> bool:
        return self.has_selection

    def get_text_selected(self, _fmt):
        return self.selected_text

    def copy_clipboard_format(self, fmt) -> None:
        self.calls.append(('copy_clipboard_format', fmt))

    def copy_clipboard(self) -> None:
        self.calls.append('copy_clipboard')

    def copy_primary(self) -> None:
        self.calls.append('copy_primary')


def make_attach(terminal: TerminalStub, clipboards: dict[object, ClipboardStub]):
    attach = SimpleNamespace(terminal=terminal, _last_copied_text='')
    attach._selected_text = lambda: AttachTerminal._selected_text(attach)
    attach._system_clipboard = lambda selection: clipboards.get(selection)
    attach._set_clipboard_text = lambda text, selection: AttachTerminal._set_clipboard_text(attach, text, selection)
    return attach


class TerminalCopyPipelineTest(TestCase):
    def test_selected_text_uses_durable_clipboard_and_vte_owned_primary(self) -> None:
        terminal = TerminalStub(selected_text='copied text')
        clipboards = {
            gtk_app.Gdk.SELECTION_CLIPBOARD: ClipboardStub(),
            gtk_app.Gdk.SELECTION_PRIMARY: ClipboardStub(),
        }
        attach = make_attach(terminal, clipboards)

        copied = AttachTerminal._copy_selection_to_clipboards(attach)

        self.assertTrue(copied)
        self.assertEqual(attach._last_copied_text, 'copied text')
        self.assertEqual(clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].text, 'copied text')
        self.assertEqual(clipboards[gtk_app.Gdk.SELECTION_PRIMARY].text, '')
        self.assertTrue(clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].stored)
        self.assertFalse(clipboards[gtk_app.Gdk.SELECTION_PRIMARY].stored)
        self.assertIn(('copy_clipboard_format', gtk_app.Vte.Format.TEXT), terminal.calls)
        self.assertIn('copy_primary', terminal.calls)

    def test_durable_clipboard_is_installed_without_reclaiming_primary(self) -> None:
        events = []
        terminal = TerminalStub(selected_text="copied text")
        terminal.copy_clipboard_format = lambda fmt: events.append(("vte-clipboard", fmt))
        terminal.copy_primary = lambda: events.append(("vte-primary", None))
        attach = make_attach(terminal, {})
        attach._set_clipboard_text = lambda text, selection: events.append(("gtk", selection, text))

        self.assertTrue(AttachTerminal._copy_selection_to_clipboards(attach))

        self.assertEqual(events[-1], ("gtk", gtk_app.Gdk.SELECTION_CLIPBOARD, "copied text"))
        self.assertNotIn(
            ("gtk", gtk_app.Gdk.SELECTION_PRIMARY, "copied text"),
            events,
        )

    def test_no_text_but_reported_selection_uses_vte_copy_fallbacks(self) -> None:
        terminal = TerminalStub(selected_text='', has_selection=True)
        attach = make_attach(terminal, {})

        copied = AttachTerminal._copy_selection_to_clipboards(attach)

        self.assertTrue(copied)
        self.assertIn(('copy_clipboard_format', gtk_app.Vte.Format.TEXT), terminal.calls)
        self.assertIn('copy_primary', terminal.calls)

    def test_no_selection_returns_false_without_mutating_last_copy(self) -> None:
        terminal = TerminalStub(selected_text='', has_selection=False)
        attach = make_attach(terminal, {})
        attach._last_copied_text = 'previous'

        copied = AttachTerminal._copy_selection_to_clipboards(attach)

        self.assertFalse(copied)
        self.assertEqual(attach._last_copied_text, 'previous')
        self.assertEqual(terminal.calls, [])

    def test_missing_clipboard_is_ignored_without_losing_other_selection(self) -> None:
        terminal = TerminalStub(selected_text='clip')
        clipboards = {gtk_app.Gdk.SELECTION_CLIPBOARD: ClipboardStub()}
        attach = make_attach(terminal, clipboards)

        copied = AttachTerminal._copy_selection_to_clipboards(attach)

        self.assertTrue(copied)
        self.assertEqual(clipboards[gtk_app.Gdk.SELECTION_CLIPBOARD].text, 'clip')
        self.assertEqual(attach._last_copied_text, 'clip')


if __name__ == '__main__':
    import unittest

    unittest.main()
