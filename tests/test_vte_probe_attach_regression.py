from __future__ import annotations

import inspect
import time
import unittest

from termdeck.gtk_app import AttachTerminal, Gtk, Vte
from termdeck.models import TerminalTheme


class VteLoopbackInputRegressionTest(unittest.TestCase):
    """VTE's input side must live on a raw loopback PTY with no child on it.

    Pane output is fed straight into VTE (``terminal.feed``); everything VTE
    writes to its PTY — keystrokes and DA/XTVERSION probe replies — must come
    back off the loopback slave and be forwarded to the tmux pane through the
    control-mode client, never echoed into the visible screen.
    """

    def test_loopback_pty_is_raw_foreign_and_never_runs_a_child(self) -> None:
        source = inspect.getsource(AttachTerminal._ensure_loopback_pty)

        raw = source.index("tty.setraw(slave_fd, when=termios.TCSANOW)")
        pty_new = source.index("self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)")
        attach = source.index("self.terminal.set_pty(self.pty_obj)")

        self.assertLess(raw, pty_new)
        self.assertLess(pty_new, attach)
        # No child process may ever run on the loopback slave.
        self.assertNotIn("subprocess", source)
        self.assertNotIn("Popen", source)
        # The slave feeds the control-mode client, not a local process.
        reader = inspect.getsource(AttachTerminal._on_loopback_readable)
        self.assertIn("self.controller.send_bytes(data)", reader)

    def test_real_vte_probe_replies_reach_controller_not_visible_screen(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")

        class RecordingController:
            def __init__(self) -> None:
                self.sent = bytearray()

            def send_bytes(self, data: bytes) -> None:
                self.sent.extend(data)

            def resize(self, *, columns: int, rows: int) -> None:
                pass

            def stop(self) -> None:
                pass

        terminal = AttachTerminal(["true"], "/tmp", TerminalTheme())
        # Keep the widget offline: this test exercises only the loopback path.
        terminal._attach_restart_deferred = True
        window = Gtk.Window()
        try:
            window.add(terminal)
            window.resize(800, 500)
            window.show_all()
            terminal._ensure_loopback_pty()
            controller = RecordingController()
            terminal.controller = controller
            # Ask the emulator to identify itself, exactly like codex does.
            terminal.terminal.feed(b"READY\r\n\x1b[c\x1b[>q")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                if b"\x1b[" in bytes(controller.sent):
                    break
                time.sleep(0.01)

            self.assertIn(b"\x1b[", bytes(controller.sent))
            captured = terminal.terminal.get_text_format(Vte.Format.TEXT)
            self.assertIn("READY", captured)
            self.assertNotIn("^[[", captured)
            self.assertNotIn("VTE(", captured)
        finally:
            terminal.detach()
            window.destroy()

    def test_replayed_bracketed_paste_mode_wraps_multiline_composer_text(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")

        class RecordingController:
            def __init__(self) -> None:
                self.sent = bytearray()

            def send_bytes(self, data: bytes) -> bool:
                self.sent.extend(data)
                return True

            def resize(self, *, columns: int, rows: int) -> None:
                pass

            def stop(self) -> None:
                pass

        terminal = AttachTerminal(["true"], "/tmp", TerminalTheme())
        terminal._attach_restart_deferred = True
        window = Gtk.Window()
        try:
            window.add(terminal)
            window.resize(800, 500)
            window.show_all()
            terminal._ensure_loopback_pty()
            controller = RecordingController()
            terminal.controller = controller

            # PaneStream replay must restore DECSET 2004 before clipboard text
            # is delivered. VTE then wraps embedded newlines as one composer
            # paste instead of emitting an unprotected submit key.
            terminal.terminal.feed(b"\x1b[?2004h")
            for _index in range(20):
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                time.sleep(0.005)
            terminal.terminal.paste_text("draft line\nsecond line")
            deadline = time.monotonic() + 3.0
            # VTE canonicalizes pasted newlines to CR, but DECSET 2004 keeps
            # that CR inside one protected paste transaction.
            expected = b"\x1b[200~draft line\rsecond line\x1b[201~"
            while time.monotonic() < deadline:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                if expected in bytes(controller.sent):
                    break
                time.sleep(0.01)

            payload = bytes(controller.sent)
            self.assertIn(expected, payload)
            self.assertNotIn(b"\x1b[201~\r", payload)
            self.assertEqual(payload.count(b"\x1b[200~"), 1)
            self.assertEqual(payload.count(b"\x1b[201~"), 1)
        finally:
            terminal.detach()
            window.destroy()


if __name__ == "__main__":
    unittest.main()
