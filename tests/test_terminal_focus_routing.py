from types import SimpleNamespace
from unittest import TestCase
from unittest import mock

from termdeck import gtk_app
from termdeck.gtk_app import AttachTerminal, TermDeckWindow


class TerminalStub:
    def __init__(self) -> None:
        self.focus_count = 0
        self.bottom_count = 0

    def grab_focus(self) -> None:
        self.focus_count += 1


class KeyMapStub:
    def matches(self, _event, _action: str) -> bool:
        return False


class TerminalFocusRoutingTest(TestCase):
    def test_first_terminal_key_restores_live_view_before_vte_receives_it(self) -> None:
        calls = []
        record = SimpleNamespace(id="scrolling")
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_has_selection=lambda: False),
            copy_clipboard=lambda: calls.append("copy"),
            paste_clipboard=lambda: calls.append("paste"),
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
        )
        backend = SimpleNamespace(scroll_view_active=lambda candidate: candidate is record)
        window = SimpleNamespace(
            keymap=KeyMapStub(),
            app_context=SimpleNamespace(backend=backend),
            handle_zoom_event=lambda _event: False,
            _current_attach_terminal=lambda: attach,
            _is_copy_event=lambda _event: False,
            _is_plain_ctrl_c_event=lambda _event: False,
            _terminal_has_selection=lambda _attach: False,
            _is_paste_event=lambda _event: False,
            current_record=lambda: record,
            _restore_live_terminal=lambda candidate: calls.append(("restore", candidate.id)),
        )
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_x, state=0)

        handled = TermDeckWindow.on_terminal_key_press(window, attach.terminal, event)

        self.assertFalse(handled)
        self.assertEqual(
            calls,
            [("restore", "scrolling"), ("cursor-hidden", False)],
        )

    def test_paste_restores_live_view_before_clipboard_delivery(self) -> None:
        calls = []
        record = SimpleNamespace(id="scrolling")
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_has_selection=lambda: False),
            copy_clipboard=lambda: calls.append("copy"),
            paste_clipboard=lambda: calls.append("paste"),
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
        )
        backend = SimpleNamespace(scroll_view_active=lambda candidate: candidate is record)
        window = SimpleNamespace(
            keymap=KeyMapStub(),
            app_context=SimpleNamespace(backend=backend),
            handle_zoom_event=lambda _event: False,
            _current_attach_terminal=lambda: attach,
            _is_copy_event=lambda _event: False,
            _is_plain_ctrl_c_event=lambda _event: False,
            _terminal_has_selection=lambda _attach: False,
            _is_paste_event=lambda _event: True,
            current_record=lambda: record,
            _restore_live_terminal=lambda candidate: calls.append(("restore", candidate.id)),
        )
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_v, state=0)

        handled = TermDeckWindow.on_terminal_key_press(window, attach.terminal, event)

        self.assertTrue(handled)
        self.assertEqual(
            calls,
            [("restore", "scrolling"), ("cursor-hidden", False), "paste"],
        )

    def test_unhandled_window_key_focuses_terminal_and_delivers_first_event(self) -> None:
        terminal = TerminalStub()
        attach = SimpleNamespace(
            terminal=terminal,
            prepare_for_live_input=lambda: None,
            scroll_to_bottom=lambda: setattr(
                terminal,
                "bottom_count",
                terminal.bottom_count + 1,
            ),
        )
        chrome = object()
        delivered = []
        window = SimpleNamespace(
            _current_attach_terminal=lambda: attach,
            get_focus=lambda: chrome,
            _is_text_input_focus=lambda _focus: False,
            on_terminal_key_press=lambda *_args: False,
        )
        attach.feed_key_event = lambda event: delivered.append(event) or True
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_x, state=0)

        handled = TermDeckWindow._route_unhandled_key_to_terminal(window, event)

        self.assertTrue(handled)
        self.assertEqual(terminal.bottom_count, 1)
        self.assertEqual(terminal.focus_count, 1)
        self.assertEqual(delivered, [event])

    def test_terminal_shortcut_is_handled_after_focus_moves_from_chrome(self) -> None:
        terminal = TerminalStub()
        delivered = []
        attach = SimpleNamespace(
            terminal=terminal,
            prepare_for_live_input=lambda: None,
            scroll_to_bottom=lambda: None,
            feed_key_event=delivered.append,
        )
        window = SimpleNamespace(
            _current_attach_terminal=lambda: attach,
            get_focus=lambda: object(),
            _is_text_input_focus=lambda _focus: False,
            on_terminal_key_press=lambda *_args: True,
        )
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_v, state=gtk_app.Gdk.ModifierType.CONTROL_MASK)

        handled = TermDeckWindow._route_unhandled_key_to_terminal(window, event)

        self.assertTrue(handled)
        self.assertEqual(terminal.focus_count, 1)
        self.assertEqual(delivered, [])

    def test_text_input_focus_is_not_stolen(self) -> None:
        terminal = TerminalStub()
        attach = SimpleNamespace(terminal=terminal, scroll_to_bottom=lambda: None)
        window = SimpleNamespace(
            _current_attach_terminal=lambda: attach,
            get_focus=lambda: object(),
            _is_text_input_focus=lambda _focus: True,
            on_terminal_key_press=lambda *_args: self.fail("text input key was routed to terminal"),
        )

        handled = TermDeckWindow._route_unhandled_key_to_terminal(
            window,
            SimpleNamespace(keyval=gtk_app.Gdk.KEY_x, state=0),
        )

        self.assertFalse(handled)
        self.assertEqual(terminal.focus_count, 0)

    def test_window_key_handler_delegates_otherwise_unhandled_key(self) -> None:
        routed = []
        window = SimpleNamespace(
            keymap=KeyMapStub(),
            handle_zoom_event=lambda _event: False,
            _route_unhandled_key_to_terminal=lambda event: routed.append(event) or True,
        )
        event = SimpleNamespace(keyval=gtk_app.Gdk.KEY_x, state=0)

        handled = TermDeckWindow.on_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(routed, [event])

    def test_plain_ctrl_l_is_content_preserving_noop(self) -> None:
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_l,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK,
        )

        self.assertTrue(TermDeckWindow._is_plain_ctrl_l_event(event))
        self.assertFalse(
            TermDeckWindow._is_plain_ctrl_l_event(
                SimpleNamespace(
                    keyval=gtk_app.Gdk.KEY_l,
                    state=gtk_app.Gdk.ModifierType.CONTROL_MASK
                    | gtk_app.Gdk.ModifierType.SHIFT_MASK,
                )
            )
        )
        self.assertFalse(
            TermDeckWindow._is_plain_ctrl_l_event(
                SimpleNamespace(
                    keyval=gtk_app.Gdk.KEY_l,
                    state=gtk_app.Gdk.ModifierType.CONTROL_MASK
                    | gtk_app.Gdk.ModifierType.MOD1_MASK,
                )
            )
        )

    def test_terminal_handler_consumes_plain_ctrl_l_before_vte(self) -> None:
        window = SimpleNamespace(
            keymap=KeyMapStub(),
            handle_zoom_event=lambda _event: False,
            _current_attach_terminal=lambda: self.fail("Ctrl+L must be consumed before VTE routing"),
        )
        event = SimpleNamespace(
            keyval=gtk_app.Gdk.KEY_l,
            state=gtk_app.Gdk.ModifierType.CONTROL_MASK,
        )

        handled = TermDeckWindow.on_terminal_key_press(window, None, event)

        self.assertTrue(handled)

    def test_first_key_encoder_handles_text_control_and_navigation_keys(self) -> None:
        cases = [
            (gtk_app.Gdk.KEY_x, 0, b"x"),
            (gtk_app.Gdk.KEY_X, gtk_app.Gdk.ModifierType.SHIFT_MASK, b"X"),
            (gtk_app.Gdk.KEY_d, gtk_app.Gdk.ModifierType.CONTROL_MASK, b"\x04"),
            (gtk_app.Gdk.KEY_x, gtk_app.Gdk.ModifierType.MOD1_MASK, b"\x1bx"),
            (gtk_app.Gdk.KEY_Return, 0, b"\r"),
            (gtk_app.Gdk.KEY_Left, 0, b"\x1b[D"),
            (
                gtk_app.Gdk.KEY_Left,
                gtk_app.Gdk.ModifierType.CONTROL_MASK,
                b"\x1b[1;5D",
            ),
            (gtk_app.Gdk.KEY_Page_Down, 0, b"\x1b[6~"),
        ]

        for keyval, state, expected in cases:
            with self.subTest(keyval=keyval, state=state):
                event = SimpleNamespace(keyval=keyval, state=state)
                self.assertEqual(AttachTerminal._key_event_bytes(event), expected)

    def test_first_key_is_written_directly_to_attached_pty(self) -> None:
        attach = SimpleNamespace(
            pty_obj=SimpleNamespace(get_fd=lambda: 17),
            terminal=SimpleNamespace(),
            _key_event_bytes=lambda _event: b"x",
        )

        with mock.patch("termdeck.gtk_app.os.write", return_value=1) as write:
            delivered = AttachTerminal.feed_key_event(attach, SimpleNamespace())

        self.assertTrue(delivered)
        write.assert_called_once_with(17, b"x")


if __name__ == "__main__":
    import unittest

    unittest.main()
