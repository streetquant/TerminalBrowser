import time
from types import SimpleNamespace
from unittest import TestCase
from unittest import mock

from termdeck.gtk_app import AttachTerminal, Gdk, Gtk, TermDeckWindow, Vte
from termdeck.models import TerminalTheme


class TerminalMouseBehaviourTest(TestCase):
    def test_real_vte_attach_does_not_echo_terminal_probe_replies_into_shell(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        attach = AttachTerminal(["true"], "/tmp", TerminalTheme())
        attach._attach_restart_deferred = True
        window = Gtk.Window()
        try:
            window.add(attach)
            window.resize(800, 500)
            window.show_all()
            attach._ensure_loopback_pty()
            sent = []
            attach.controller = SimpleNamespace(
                send_bytes=lambda data: sent.append(data),
                resize=lambda **kwargs: None,
                stop=lambda: None,
            )
            # Ask VTE for device attributes exactly as an attached TUI would.
            # The replies must flow back to the control-mode client, never be
            # painted into the visible buffer as escape-code garbage.
            attach.terminal.feed(b"probe\r\n\x1b[c\x1b[>c")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not sent:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                time.sleep(0.01)
            reply = b"".join(sent)
            self.assertTrue(reply.startswith(b"\x1b["), reply)
            captured = attach.terminal.get_text_format(Vte.Format.TEXT)
            self.assertIn("probe", captured)
            self.assertNotIn("^[[", captured)
        finally:
            attach.detach()
            window.destroy()

    def test_reparenting_rebinds_attach_exit_to_destination_window(self) -> None:
        connected = []
        terminal = SimpleNamespace(
            connect=lambda signal, handler: connected.append((signal, handler)) or 17,
            disconnect=mock.Mock(),
        )
        destination_exit = lambda: False
        attach = SimpleNamespace(
            terminal=terminal,
            _key_press_handler_id=0,
            _live_focus_handler=None,
            _scroll_handler=None,
            _selection_history_handler=None,
            _attach_exit_handler=lambda: True,
        )
        key_handler = lambda *_args: False
        focus_handler = lambda: None
        scroll_handler = lambda _lines: True
        selection_history_handler = lambda: "history"

        AttachTerminal.rebind_window_handlers(
            attach,
            key_press_handler=key_handler,
            live_focus_handler=focus_handler,
            scroll_handler=scroll_handler,
            selection_history_handler=selection_history_handler,
            attach_exit_handler=destination_exit,
        )

        self.assertEqual(connected, [("key-press-event", key_handler)])
        self.assertEqual(attach._key_press_handler_id, 17)
        self.assertIs(attach._live_focus_handler, focus_handler)
        self.assertIs(attach._scroll_handler, scroll_handler)
        self.assertIs(attach._selection_history_handler, selection_history_handler)
        self.assertIs(attach._attach_exit_handler, destination_exit)

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

    def test_terminal_scrollbar_is_bound_to_the_native_vte_adjustment(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=1), mock.patch(
            "termdeck.gtk_app.GLib.source_remove"
        ):
            history_reader = lambda: "history"
            attach = AttachTerminal(
                ["true"],
                "/tmp",
                TerminalTheme(),
                selection_history_handler=history_reader,
            )
            try:
                attach.show_all()
                self.assertEqual(
                    attach.terminal_row.get_children(),
                    [attach.terminal, attach.scrollbar],
                )
                self.assertIs(attach.native_adjustment, attach.terminal.get_vadjustment())
                self.assertIs(attach.scrollbar.get_adjustment(), attach.native_adjustment)
                self.assertTrue(attach.terminal.get_enable_fallback_scrolling())
                self.assertFalse(attach.terminal.get_scroll_on_output())
                self.assertTrue(attach.terminal.get_scroll_on_keystroke())
                self.assertIs(attach._selection_history_handler, history_reader)
                self.assertIn(
                    attach._child_environment["TERM"],
                    {"termdeck-vte", "teraterm-256color"},
                )
            finally:
                attach.destroy()

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

    def test_plain_wheel_without_selection_defers_to_native_vte_stock_scrolling(self) -> None:
        calls = []
        terminal = SimpleNamespace(get_has_selection=lambda: False)
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: calls.append("native-check") or True,
            _move_native_history=lambda lines: calls.append(("native", lines)),
        )

        handled = AttachTerminal._on_terminal_scroll(
            attach,
            terminal,
            SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0),
        )

        self.assertFalse(handled)
        self.assertEqual(calls, [])

    def test_at_native_live_edge_reads_real_adjustment_geometry(self) -> None:
        at_bottom = SimpleNamespace(
            native_adjustment=SimpleNamespace(
                get_value=lambda: 76.0,
                get_upper=lambda: 100.0,
                get_page_size=lambda: 24.0,
            )
        )
        scrolled_back = SimpleNamespace(
            native_adjustment=SimpleNamespace(
                get_value=lambda: 40.0,
                get_upper=lambda: 100.0,
                get_page_size=lambda: 24.0,
            )
        )

        self.assertTrue(AttachTerminal._at_native_live_edge(at_bottom))
        self.assertFalse(AttachTerminal._at_native_live_edge(scrolled_back))

    def test_selection_start_queues_debounced_autocopy(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_has_selection=lambda: True),
            _selection_pending=False,
            _queue_selection_copy=lambda **kwargs: calls.append(("copy", kwargs)),
        )

        AttachTerminal._on_selection_changed(attach, None)

        self.assertTrue(attach._selection_pending)
        self.assertEqual(calls, [("copy", {"delay_ms": 120})])

    def test_selection_clear_does_not_queue_an_autocopy(self) -> None:
        calls = []
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_has_selection=lambda: False),
            _selection_pending=True,
            _queue_selection_copy=lambda **kwargs: calls.append(("copy", kwargs)),
        )

        AttachTerminal._on_selection_changed(attach, None)

        self.assertFalse(attach._selection_pending)
        self.assertEqual(calls, [])

    def test_wheel_snapshots_pending_selection_before_tmux_redraw(self) -> None:
        terminal = SimpleNamespace(get_has_selection=lambda: True)
        set_clipboard = mock.Mock()
        attach = SimpleNamespace(
            terminal=terminal,
            _selection_autocopy_source_id=41,
            _selection_pending=True,
            _selected_text=lambda: "selected before scroll",
            _set_clipboard_text=set_clipboard,
        )

        with mock.patch("termdeck.gtk_app.GLib.source_remove") as source_remove:
            copied = AttachTerminal._snapshot_selection_before_history_scroll(attach)

        self.assertTrue(copied)
        self.assertEqual(attach._last_copied_text, "selected before scroll")
        set_clipboard.assert_called_once_with(
            "selected before scroll", Gdk.SELECTION_CLIPBOARD
        )
        source_remove.assert_called_once_with(41)
        self.assertEqual(attach._selection_autocopy_source_id, 0)
        self.assertFalse(attach._selection_pending)

    def test_wheel_with_selection_uses_native_buffer_and_never_redraws_tmux(self) -> None:
        calls = []
        terminal = SimpleNamespace(get_has_selection=lambda: True)
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: True,
            _continue_selection_through_native_scroll=lambda event, lines: False,
            _move_native_history=lambda lines: calls.append(("native", lines)),
            _scroll_handler=lambda lines: calls.append(("tmux", lines)) or True,
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["snapshot", ("native", -3 if event.direction == Gdk.ScrollDirection.UP else 3)])

    def test_selected_wheel_prefers_native_vte_history_over_tmux_redraw(self) -> None:
        calls = []
        adjustment = mock.Mock()
        adjustment.get_lower.return_value = 0.0
        adjustment.get_upper.return_value = 400.0
        adjustment.get_page_size.return_value = 20.0
        adjustment.get_value.return_value = 380.0
        adjustment.get_step_increment.return_value = 1.0
        terminal = SimpleNamespace(
            get_has_selection=lambda: True,
            get_vadjustment=lambda: adjustment,
        )
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: True,
            _continue_selection_through_native_scroll=lambda event, lines: False,
            _move_native_history=lambda lines: calls.append(("native", lines)),
            _scroll_handler=lambda lines: calls.append(("tmux", lines)) or True,
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["snapshot", ("native", -3 if event.direction == Gdk.ScrollDirection.UP else 3)])

    def test_held_selection_scroll_completes_moves_and_shift_extends_native_range(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _dispatch_selection_button_event=lambda event, kind, state: calls.append(
                (kind, int(state))
            )
            or True,
            _move_native_history=lambda lines: calls.append(("move", lines)),
        )
        event = SimpleNamespace(
            state=Gdk.ModifierType.BUTTON1_MASK | Gdk.ModifierType.CONTROL_MASK,
        )

        handled = AttachTerminal._continue_selection_through_native_scroll(
            attach, event, -3
        )

        self.assertTrue(handled)
        self.assertEqual(calls[0][0], Gdk.EventType.BUTTON_RELEASE)
        self.assertTrue(calls[0][1] & int(Gdk.ModifierType.BUTTON1_MASK))
        self.assertEqual(calls[1], ("move", -3))
        self.assertEqual(calls[2][0], Gdk.EventType.BUTTON_PRESS)
        self.assertFalse(calls[2][1] & int(Gdk.ModifierType.BUTTON1_MASK))
        self.assertTrue(calls[2][1] & int(Gdk.ModifierType.SHIFT_MASK))
        self.assertTrue(calls[2][1] & int(Gdk.ModifierType.CONTROL_MASK))

    def test_held_selection_bridge_ignores_wheel_without_button_one(self) -> None:
        attach = SimpleNamespace(
            _dispatch_selection_button_event=mock.Mock(),
            _move_native_history=mock.Mock(),
        )

        handled = AttachTerminal._continue_selection_through_native_scroll(
            attach,
            SimpleNamespace(state=Gdk.ModifierType.SHIFT_MASK),
            3,
        )

        self.assertFalse(handled)
        attach._dispatch_selection_button_event.assert_not_called()
        attach._move_native_history.assert_not_called()

    def test_native_scrollback_is_detected_from_the_real_vte_adjustment(self) -> None:
        adjustment = mock.Mock()
        adjustment.get_lower.return_value = 0.0
        adjustment.get_upper.return_value = 400.0
        adjustment.get_page_size.return_value = 20.0
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_vadjustment=lambda: adjustment),
        )

        available = AttachTerminal._native_scrollback_available(attach)

        self.assertTrue(available)
        adjustment.set_value.assert_not_called()

    def test_zero_range_adjustment_is_not_reported_as_native_scrollback(self) -> None:
        adjustment = mock.Mock()
        adjustment.get_lower.return_value = 0.0
        adjustment.get_upper.return_value = 20.0
        adjustment.get_page_size.return_value = 20.0
        attach = SimpleNamespace(
            terminal=SimpleNamespace(get_vadjustment=lambda: adjustment),
        )

        available = AttachTerminal._native_scrollback_available(attach)

        self.assertFalse(available)
        adjustment.set_value.assert_not_called()

    def test_selected_wheel_at_native_live_edge_does_not_enter_tmux_review(self) -> None:
        calls = []
        terminal = SimpleNamespace(get_has_selection=lambda: True)
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: True,
            _continue_selection_through_native_scroll=lambda event, lines: False,
            _move_native_history=lambda lines: calls.append(("native", lines)),
            _scroll_handler=lambda lines: calls.append(("tmux", lines)) or True,
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.DOWN, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["snapshot", ("native", -3 if event.direction == Gdk.ScrollDirection.UP else 3)])

    def test_real_vte_selection_is_preserved_during_explicit_native_wheel_move(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        with mock.patch("termdeck.gtk_app.GLib.timeout_add", return_value=1), mock.patch(
            "termdeck.gtk_app.GLib.source_remove"
        ):
            tmux_calls = []
            attach = AttachTerminal(
                ["true"],
                "/tmp",
                TerminalTheme(),
                scroll_handler=lambda lines: tmux_calls.append(lines) or True,
            )
        window = Gtk.Window()
        try:
            window.add(attach)
            window.resize(800, 500)
            window.show_all()
            attach.terminal.feed(
                "".join(f"selection-history-{index}\r\n" for index in range(400)).encode()
            )
            for _ in range(20):
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
            adjustment = attach.terminal.get_vadjustment()
            if float(adjustment.get_upper()) <= float(adjustment.get_page_size()):
                self.skipTest("VTE did not expose native history in this backend")
            attach.terminal.select_all()
            self.assertTrue(attach.terminal.get_has_selection())
            live_edge = float(adjustment.get_value())
            selected_before = attach.terminal.get_text_selected(Vte.Format.TEXT)
            self.assertTrue(
                AttachTerminal._on_terminal_scroll(
                    attach,
                    attach.terminal,
                    SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0),
                )
            )
            self.assertTrue(attach.terminal.get_has_selection())
            self.assertLess(float(adjustment.get_value()), live_edge)
            self.assertEqual(
                attach.terminal.get_text_selected(Vte.Format.TEXT),
                selected_before,
            )
            self.assertEqual(tmux_calls, [])
        finally:
            window.destroy()

    def test_selected_wheel_without_native_history_never_redraws_or_switches_surface(self) -> None:
        calls = []
        terminal = SimpleNamespace(get_has_selection=lambda: True)
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: False,
            _scroll_handler=lambda lines: calls.append(("tmux", lines)) or True,
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.UP, delta_y=0.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["snapshot"])

    def test_smooth_wheel_with_a_selection_scales_native_history_movement(self) -> None:
        calls = []
        terminal = SimpleNamespace(get_has_selection=lambda: True)
        attach = SimpleNamespace(
            terminal=terminal,
            _snapshot_selection_before_history_scroll=lambda: calls.append("snapshot"),
            _native_scrollback_available=lambda: True,
            _continue_selection_through_native_scroll=lambda event, lines: False,
            _move_native_history=lambda lines: calls.append(("native", lines)),
        )
        event = SimpleNamespace(direction=Gdk.ScrollDirection.SMOOTH, delta_y=-5.0)

        handled = AttachTerminal._on_terminal_scroll(attach, terminal, event)

        self.assertTrue(handled)
        self.assertEqual(calls, ["snapshot", ("native", -15)])

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
            prepare_for_live_input=lambda: calls.append("live") or True,
        )
        event = SimpleNamespace(button=1, state=0)

        handled = AttachTerminal._on_button_release(attach, None, event)

        self.assertFalse(handled)
        self.assertEqual(calls, ["live", "focus"])

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

    def test_live_input_restores_owner_before_resetting_view(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _scroll_idle_source_id=0,
            _remove_glib_source=lambda source_id: None,
            _live_focus_handler=lambda: calls.append("restore") or True,
            scroll_to_bottom=lambda: calls.append("bottom"),
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor-hidden", hidden)),
        )

        restored = AttachTerminal.prepare_for_live_input(attach)

        self.assertTrue(restored)
        self.assertEqual(
            calls,
            ["restore", "bottom", ("cursor-hidden", False)],
        )

    def test_live_input_latches_priority_before_controller_exists(self) -> None:
        attach = SimpleNamespace(
            _scroll_idle_source_id=0,
            _remove_glib_source=lambda _source_id: None,
            _setup_priority_requested=False,
            controller=None,
            _live_focus_handler=lambda: True,
            scroll_to_bottom=lambda: None,
            _set_history_cursor_hidden=lambda _hidden: None,
        )

        restored = AttachTerminal.prepare_for_live_input(attach)

        self.assertTrue(restored)
        self.assertTrue(attach._setup_priority_requested)

    def test_latched_priority_is_applied_before_controller_start(self) -> None:
        calls = []
        controller = SimpleNamespace(
            prioritize_setup=lambda: calls.append("priority"),
            start=lambda **kwargs: calls.append(("start", kwargs)),
        )
        attach = SimpleNamespace(
            controller=None,
            _detached=False,
            _terminally_stopped=False,
            _attach_restart_deferred=False,
            _connect_source_id=11,
            _connect_fallback_source_id=12,
            _remove_glib_source=lambda source_id: calls.append(("remove", source_id)),
            _ensure_loopback_pty=lambda: calls.append("pty"),
            _sync_stream_geometry=lambda **kwargs: calls.append(("sync", kwargs)),
            _stream_generation=0,
            argv=["tmux", "attach-session", "-t", "demo"],
            _child_environment={},
            cwd="/tmp",
            _on_stream_output=lambda *_args: None,
            _on_stream_pane_dead=lambda *_args: None,
            _on_stream_state=lambda *_args: None,
            _scrollback_extent=2000,
            _contrast_filter=SimpleNamespace(reset=lambda: calls.append("reset")),
            _setup_priority_requested=True,
        )

        with mock.patch("termdeck.gtk_app.PaneStreamController", return_value=controller):
            AttachTerminal._connect_stream(attach, columns=132, rows=43)

        priority_index = calls.index("priority")
        start_index = next(index for index, call in enumerate(calls) if call[0] == "start")
        self.assertLess(priority_index, start_index)
        self.assertEqual(calls[start_index], ("start", {"columns": 132, "rows": 43}))

    def test_priority_failure_does_not_prevent_controller_start(self) -> None:
        started = []

        def fail_priority() -> None:
            raise RuntimeError("priority gate unavailable")

        controller = SimpleNamespace(
            prioritize_setup=fail_priority,
            start=lambda **kwargs: started.append(kwargs),
        )
        attach = SimpleNamespace(
            controller=None,
            _detached=False,
            _terminally_stopped=False,
            _attach_restart_deferred=False,
            _connect_source_id=0,
            _connect_fallback_source_id=0,
            _remove_glib_source=lambda _source_id: None,
            _ensure_loopback_pty=lambda: None,
            _sync_stream_geometry=lambda **_kwargs: None,
            _stream_generation=0,
            argv=["tmux", "attach-session", "-t", "demo"],
            _child_environment={},
            cwd="/tmp",
            _on_stream_output=lambda *_args: None,
            _on_stream_pane_dead=lambda *_args: None,
            _on_stream_state=lambda *_args: None,
            _scrollback_extent=2000,
            _contrast_filter=SimpleNamespace(reset=lambda: None),
            _setup_priority_requested=True,
        )

        with mock.patch("termdeck.gtk_app.PaneStreamController", return_value=controller):
            AttachTerminal._connect_stream(attach, columns=100, rows=30)

        self.assertEqual(started, [{"columns": 100, "rows": 30}])

    def test_failed_live_restore_does_not_mutate_view_or_release_input_surface(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _scroll_idle_source_id=0,
            _remove_glib_source=lambda source_id: None,
            _live_focus_handler=lambda: calls.append("restore") or False,
            scroll_to_bottom=lambda: calls.append("bottom"),
            _set_history_cursor_hidden=lambda hidden: calls.append(("cursor", hidden)),
        )

        restored = AttachTerminal.prepare_for_live_input(attach)

        self.assertFalse(restored)
        self.assertEqual(calls, ["restore"])

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

    def test_forced_geometry_sync_resizes_pty_and_signals_stream_controller(self) -> None:
        pty_obj = mock.Mock()
        controller = mock.Mock()
        attach = SimpleNamespace(
            pty_obj=pty_obj,
            controller=controller,
            _last_synced_grid=(120, 40),
        )

        AttachTerminal._sync_stream_geometry(
            attach,
            columns=120,
            rows=40,
            force=True,
        )

        pty_obj.set_size.assert_called_once_with(40, 120)
        controller.resize.assert_called_once_with(columns=120, rows=40)
        self.assertEqual(attach._last_synced_grid, (120, 40))

    def test_canonical_grid_resizes_vte_before_forcing_stream_sync(self) -> None:
        calls = []
        attach = SimpleNamespace(
            _detached=False,
            terminal=SimpleNamespace(
                set_size=lambda columns, rows: calls.append(("vte", columns, rows))
            ),
            _last_allocated_grid=(80, 24),
            _last_synced_grid=(80, 24),
            _sync_stream_geometry=lambda **kwargs: calls.append(("stream", kwargs)),
        )

        AttachTerminal.apply_canonical_grid(attach, columns=144, rows=43)

        self.assertEqual(attach._last_allocated_grid, (144, 43))
        self.assertEqual(
            calls,
            [
                ("vte", 144, 43),
                ("stream", {"columns": 144, "rows": 43, "force": True}),
            ],
        )

    def test_canonical_grid_is_idempotent_after_vte_and_pty_match(self) -> None:
        terminal = SimpleNamespace(set_size=mock.Mock())
        sync_stream = mock.Mock()
        attach = SimpleNamespace(
            _detached=False,
            terminal=terminal,
            _last_allocated_grid=(144, 43),
            _last_synced_grid=(144, 43),
            _sync_stream_geometry=sync_stream,
        )

        AttachTerminal.apply_canonical_grid(attach, columns=144, rows=43)

        terminal.set_size.assert_not_called()
        sync_stream.assert_not_called()

    def test_failed_vte_canonical_resize_remains_retryable_after_stream_sync(self) -> None:
        terminal = SimpleNamespace(set_size=mock.Mock(side_effect=RuntimeError("resize")))
        sync_stream = mock.Mock()
        attach = SimpleNamespace(
            _detached=False,
            terminal=terminal,
            _last_allocated_grid=(80, 24),
            _last_synced_grid=(80, 24),
            _sync_stream_geometry=sync_stream,
        )

        AttachTerminal.apply_canonical_grid(attach, columns=144, rows=43)

        terminal.set_size.assert_called_once_with(144, 43)
        sync_stream.assert_called_once_with(columns=144, rows=43, force=True)
        self.assertEqual(attach._last_allocated_grid, (80, 24))

    def test_settled_window_geometry_fans_visible_grid_out_to_hidden_tabs(self) -> None:
        visible = SimpleNamespace(
            _detached=False,
            terminal=SimpleNamespace(
                get_column_count=lambda: 144,
                get_row_count=lambda: 43,
            ),
            apply_canonical_grid=mock.Mock(),
        )
        hidden = SimpleNamespace(
            _detached=False,
            terminal=SimpleNamespace(),
            apply_canonical_grid=mock.Mock(),
        )
        detached = SimpleNamespace(
            _detached=True,
            terminal=SimpleNamespace(),
            apply_canonical_grid=mock.Mock(),
        )
        notebook = SimpleNamespace(
            get_current_page=lambda: 1,
            get_nth_page=lambda index: visible if index == 1 else None,
        )
        window = SimpleNamespace(
            _terminal_layout_sync_source_id=9,
            notebook=notebook,
            tabs={
                "hidden": SimpleNamespace(widget=hidden),
                "visible": SimpleNamespace(widget=visible),
                "detached": SimpleNamespace(widget=detached),
            },
        )

        keep_source = TermDeckWindow._sync_current_terminal_geometry(window)

        self.assertFalse(keep_source)
        self.assertEqual(window._terminal_layout_sync_source_id, 0)
        visible.apply_canonical_grid.assert_called_once_with(columns=144, rows=43)
        hidden.apply_canonical_grid.assert_called_once_with(columns=144, rows=43)
        detached.apply_canonical_grid.assert_not_called()

    def test_window_or_notebook_allocation_queues_settled_geometry_fanout(self) -> None:
        queue_sync = mock.Mock()
        window = SimpleNamespace(_queue_current_terminal_geometry_sync=queue_sync)

        TermDeckWindow._on_terminal_layout_size_allocate(window, object(), object())

        queue_sync.assert_called_once_with()

    def test_unchanged_geometry_does_not_repeat_non_forced_sync(self) -> None:
        pty_obj = mock.Mock()
        controller = mock.Mock()
        attach = SimpleNamespace(
            pty_obj=pty_obj,
            controller=controller,
            _last_synced_grid=(120, 40),
        )

        AttachTerminal._sync_stream_geometry(attach, columns=120, rows=40)

        pty_obj.set_size.assert_not_called()
        controller.resize.assert_not_called()

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
