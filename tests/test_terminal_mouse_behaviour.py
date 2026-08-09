import signal
from types import SimpleNamespace
from unittest import TestCase
from unittest import mock

from termdeck.gtk_app import AttachTerminal, Gdk, Gtk, TermDeckWindow
from termdeck.models import TerminalTheme
from termdeck.tmux_backend import ScrollDisposition


class TerminalMouseBehaviourTest(TestCase):
    def test_font_zoom_clamps_readably_and_synchronizes_every_tab(self) -> None:
        widgets = [
            SimpleNamespace(
                terminal=object(),
                theme=None,
                request_geometry_sync=mock.Mock(),
            )
            for _ in range(2)
        ]
        window = SimpleNamespace(
            app_context=SimpleNamespace(
                theme=TerminalTheme(font="Monospace 24", use_system_font=False)
            ),
            tabs={
                "visible": SimpleNamespace(widget=widgets[0]),
                "hidden": SimpleNamespace(widget=widgets[1]),
            },
            font_zoom_delta=0,
            show_toast=mock.Mock(),
        )
        window.base_font = lambda: TermDeckWindow.base_font(window)
        window.zoomed_font = lambda: TermDeckWindow.zoomed_font(window)
        window.apply_font_zoom = lambda: TermDeckWindow.apply_font_zoom(window)

        with mock.patch("termdeck.gtk_app.apply_theme") as apply_theme:
            TermDeckWindow.zoom_font(window, -100)

            self.assertEqual(window.zoomed_font(), "Monospace 10")
            self.assertEqual(window.font_zoom_delta, -14)
            self.assertEqual(apply_theme.call_count, 2)
            for widget in widgets:
                widget.request_geometry_sync.assert_called_once_with(delay_ms=80)

            apply_theme.reset_mock()
            for widget in widgets:
                widget.request_geometry_sync.reset_mock()

            TermDeckWindow.zoom_font(window, 1)

            self.assertEqual(window.zoomed_font(), "Monospace 11")
            self.assertEqual(window.font_zoom_delta, -13)
            self.assertEqual(apply_theme.call_count, 2)
            for widget in widgets:
                widget.request_geometry_sync.assert_called_once_with(delay_ms=80)

    def test_terminal_has_terminator_style_scrollbar_without_arrow_key_fallback(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=1), mock.patch(
            "termdeck.gtk_app.GLib.source_remove"
        ):
            attach = AttachTerminal(["true"], "/tmp", TerminalTheme())
            try:
                attach.show_all()
                self.assertEqual(
                    attach.terminal_row.get_children(),
                    [attach.terminal, attach.scrollbar],
                )
                adjustment = attach.scrollbar.get_adjustment()
                self.assertIsNot(adjustment, attach.terminal.get_vadjustment())
                self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())
                self.assertEqual(
                    adjustment.get_value(),
                    adjustment.get_upper() - adjustment.get_page_size(),
                )
                self.assertTrue(attach.scrollbar.get_visible())
                self.assertTrue(
                    attach.scrollbar.get_style_context().has_class("td-terminal-scrollbar")
                )
                self.assertFalse(attach.terminal.get_enable_fallback_scrolling())
                self.assertFalse(attach.terminal.get_scroll_on_output())
                self.assertTrue(attach.terminal.get_scroll_on_keystroke())
            finally:
                attach.destroy()

    def test_dragging_terminal_scrollbar_routes_delta_to_session_history(self) -> None:
        calls = []
        adjustment = mock.Mock()
        adjustment.get_value.return_value = 70.0
        attach = SimpleNamespace(
            _scrollbar_syncing=False,
            _last_scrollbar_value=90.0,
            _scroll_handler=lambda lines: calls.append(lines) or True,
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
            _scrollbar_at_live_edge=lambda: False,
            _set_scrollbar_value=lambda v: None,
            _arm_scroll_idle_resume=lambda: None,
        )

        AttachTerminal._on_scrollbar_value_changed(attach, adjustment)

        self.assertEqual(calls, [-20, ("cursor-hidden", True)])
        self.assertEqual(attach._last_scrollbar_value, 70.0)

    def test_history_cursor_is_hidden_until_live_input_resumes(self) -> None:
        terminal = mock.Mock()
        confirm_hidden = mock.Mock()
        attach = SimpleNamespace(
            terminal=terminal,
            _history_cursor_hidden=False,
            _cursor_hide_source_id=0,
            _remove_glib_source=lambda source_id: None,
            _confirm_history_cursor_hidden=confirm_hidden,
        )

        with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=42) as timeout_add:
            AttachTerminal._set_history_cursor_hidden(attach, True)

        terminal.feed.assert_called_once_with(b"\x1b[?25l")
        timeout_add.assert_called_once_with(40, confirm_hidden)
        self.assertEqual(attach._cursor_hide_source_id, 42)
        self.assertTrue(attach._history_cursor_hidden)

        AttachTerminal._set_history_cursor_hidden(attach, False)

        terminal.feed.assert_called_with(b"\x1b[?25h")
        self.assertEqual(attach._cursor_hide_source_id, 0)
        self.assertFalse(attach._history_cursor_hidden)

    def test_history_cursor_hide_is_reasserted_after_the_terminal_redraw(self) -> None:
        terminal = mock.Mock()
        attach = SimpleNamespace(
            terminal=terminal,
            _cursor_hide_source_id=42,
            _history_cursor_hidden=True,
            _detached=False,
        )

        keep_source = AttachTerminal._confirm_history_cursor_hidden(attach)

        self.assertFalse(keep_source)
        self.assertEqual(attach._cursor_hide_source_id, 0)
        terminal.feed.assert_called_once_with(b"\x1b[?25l")

    def test_wheel_up_moves_real_scrollback_without_feeding_child_input(self) -> None:
        adjustment = mock.Mock()
        adjustment.get_step_increment.return_value = 1.0
        adjustment.get_lower.return_value = 0.0
        adjustment.get_upper.return_value = 200.0
        adjustment.get_page_size.return_value = 20.0
        adjustment.get_value.return_value = 180.0
        terminal = SimpleNamespace(get_vadjustment=lambda: adjustment)
        attach = SimpleNamespace(terminal=terminal)
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        adjustment.set_value.assert_called_once_with(177.0)

    def test_tmux_scroll_handler_owns_wheel_before_vte_fallback(self) -> None:
        calls = []
        adjustment = mock.Mock()
        terminal = SimpleNamespace(get_vadjustment=lambda: adjustment)
        attach = SimpleNamespace(
            terminal=terminal,
            _scroll_handler=lambda lines: calls.append(lines) or True,
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
            _move_scrollbar=lambda lines: None,
            _scrollbar_at_live_edge=lambda: False,
            _arm_scroll_idle_resume=lambda: None,
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [-3, ("cursor-hidden", True)])
        adjustment.get_value.assert_not_called()

    def test_alternate_screen_wheel_is_passed_through_to_the_tui(self) -> None:
        calls = []
        terminal = SimpleNamespace()
        attach = SimpleNamespace(
            terminal=terminal,
            _scroll_handler=lambda lines: calls.append(lines) or ScrollDisposition.PASSTHROUGH,
            _history_cursor_hidden=True,
            reset_scrollbar=lambda: calls.append("scrollbar-bottom"),
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [-3, "scrollbar-bottom", ("cursor-hidden", False)])

    def test_proxy_scrollbar_does_not_move_for_application_owned_history(self) -> None:
        calls = []
        adjustment = mock.Mock()
        adjustment.get_value.return_value = 70.0
        attach = SimpleNamespace(
            _scrollbar_syncing=False,
            _last_scrollbar_value=90.0,
            _scroll_handler=lambda lines: calls.append(lines) or ScrollDisposition.PASSTHROUGH,
            _history_cursor_hidden=True,
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
            _set_scrollbar_value=lambda value: calls.append(("scrollbar", value)),
        )

        AttachTerminal._on_scrollbar_value_changed(attach, adjustment)

        self.assertEqual(
            calls,
            [-20, ("scrollbar", 90.0), ("cursor-hidden", False)],
        )
        self.assertEqual(attach._last_scrollbar_value, 90.0)

    def test_smooth_wheel_delta_is_clamped_to_scrollback_bounds(self) -> None:
        adjustment = mock.Mock()
        adjustment.get_step_increment.return_value = 1.0
        adjustment.get_lower.return_value = 0.0
        adjustment.get_upper.return_value = 200.0
        adjustment.get_page_size.return_value = 20.0
        adjustment.get_value.return_value = 2.0
        terminal = SimpleNamespace(get_vadjustment=lambda: adjustment)
        attach = SimpleNamespace(terminal=terminal)
        event = SimpleNamespace(direction=Gdk.ScrollDirection.SMOOTH, delta_y=-5.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        adjustment.set_value.assert_called_once_with(0.0)

    def test_middle_click_pastes_primary_and_consumes_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=2)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["paste_primary"])

    def test_right_click_opens_terminal_context_menu_and_consumes_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=3)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertTrue(handled)
        self.assertEqual(calls, [("menu", event)])

    def test_left_click_does_not_consume_press_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(grab_focus=lambda: calls.append("focus")),
            prepare_for_live_input=lambda: calls.append("live"),
            scroll_to_bottom=lambda: calls.append("bottom"),
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=1)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_left_release_without_selection_returns_to_live_terminal(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(
                get_has_selection=lambda: False,
                grab_focus=lambda: calls.append("focus"),
            ),
            _selection_autocopy_source_id=0,
            prepare_for_live_input=lambda: calls.append("live"),
            scroll_to_bottom=lambda: calls.append("bottom"),
        )
        event = SimpleNamespace(button=1, state=0)

        handled = AttachTerminal._on_button_release(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, ["live", "bottom", "focus"])

    def test_shift_left_release_without_selection_preserves_scrollback_position(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(
                get_has_selection=lambda: False,
                grab_focus=lambda: calls.append("focus"),
            ),
            _selection_autocopy_source_id=0,
            prepare_for_live_input=lambda: calls.append("live"),
            scroll_to_bottom=lambda: calls.append("bottom"),
        )
        event = SimpleNamespace(button=1, state=Gdk.ModifierType.SHIFT_MASK)

        handled = AttachTerminal._on_button_release(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_shift_left_click_keeps_scrollback_position_for_selection(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(grab_focus=lambda: calls.append("focus")),
            prepare_for_live_input=lambda: calls.append("live"),
            scroll_to_bottom=lambda: calls.append("bottom"),
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace(button=1, state=Gdk.ModifierType.SHIFT_MASK)

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_live_input_restores_tui_and_forces_immediate_geometry_sync(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _scroll_idle_source_id=0,
            _remove_glib_source=lambda source_id: None,
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
            reset_scrollbar=lambda: calls.append("scrollbar-bottom"),
            _live_focus_handler=lambda: calls.append("restore"),
            request_geometry_sync=lambda **kwargs: calls.append(("geometry", kwargs)),
        )

        AttachTerminal.prepare_for_live_input(attach)

        self.assertEqual(
            calls,
            [
                "scrollbar-bottom",
                "restore",
                ("cursor-hidden", False),
                ("geometry", {"delay_ms": 0}),
            ],
        )

    def test_notebook_switch_restores_live_view_and_syncs_selected_page(self) -> None:
        calls = []
        page = SimpleNamespace(prepare_for_live_input=lambda: calls.append("prepare"))
        notebook = SimpleNamespace(
            get_current_page=lambda: 1,
            get_nth_page=lambda index: page if index == 1 else None,
        )
        window = SimpleNamespace(
            notebook=notebook,
            _refresh_auth_selector=lambda: calls.append("auth"),
            _prepare_switched_terminal=lambda selected: TermDeckWindow._prepare_switched_terminal(
                window, selected
            ),
        )

        with mock.patch(
            "termdeck.gtk_app.GLib.idle_add",
            side_effect=lambda callback, *args: callback(*args),
        ) as idle_add:
            TermDeckWindow._on_notebook_switch_page(window, notebook, page, 1)

        self.assertEqual(calls, ["auth", "prepare"])
        self.assertEqual(idle_add.call_count, 2)

    def test_forced_geometry_sync_resizes_pty_and_signals_tmux_client(self) -> None:
        pty_obj = mock.Mock()
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        attach = SimpleNamespace(
            pty_obj=pty_obj,
            process=process,
            _last_synced_grid=(120, 40),
        )

        with mock.patch("termdeck.gtk_app.os.getpgid", return_value=7654), mock.patch(
            "termdeck.gtk_app.os.killpg"
        ) as killpg:
            AttachTerminal._sync_pty_geometry(
                attach,
                columns=120,
                rows=40,
                force=True,
            )

        pty_obj.set_size.assert_called_once_with(40, 120)
        killpg.assert_called_once_with(7654, signal.SIGWINCH)
        self.assertEqual(attach._last_synced_grid, (120, 40))

    def test_unchanged_geometry_does_not_repeat_non_forced_sync(self) -> None:
        pty_obj = mock.Mock()
        attach = SimpleNamespace(
            pty_obj=pty_obj,
            process=None,
            _last_synced_grid=(120, 40),
        )

        AttachTerminal._sync_pty_geometry(attach, columns=120, rows=40)

        pty_obj.set_size.assert_not_called()

    def test_missing_button_does_not_consume_press_event(self) -> None:
        calls = []
        attach = SimpleNamespace(
            paste_primary=lambda: calls.append("paste_primary"),
            _show_terminal_context_menu=lambda event: calls.append(("menu", event)),
        )
        event = SimpleNamespace()

        handled = AttachTerminal._on_button_press(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
