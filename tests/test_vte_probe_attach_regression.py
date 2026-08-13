from __future__ import annotations

import inspect
import sys
import time
import unittest

from termdeck.gtk_app import AttachTerminal, Gtk, Vte
from termdeck.models import TerminalTheme


class VteProbeAttachRegressionTest(unittest.TestCase):
    def test_attach_makes_slave_raw_and_starts_child_before_vte_pty(self) -> None:
        source = inspect.getsource(AttachTerminal._spawn)

        raw = source.index("tty.setraw(slave_fd, when=termios.TCSANOW)")
        child = source.index("self.process = subprocess.Popen(")
        pty = source.index("self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)")
        attach = source.index("self.terminal.set_pty(self.pty_obj)")

        self.assertLess(raw, child)
        self.assertLess(child, pty)
        self.assertLess(pty, attach)

    def test_real_vte_probe_replies_are_input_not_visible_shell_echo(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")

        child = """import os, select, time
os.write(1, b'BEGIN\\r\\n\\x1b[c\\x1b[>c\\x1b[>q')
chunks = []
deadline = time.monotonic() + 1.0
while time.monotonic() < deadline:
    readable, _, _ = select.select([0], [], [], 0.05)
    if readable:
        chunks.append(os.read(0, 4096))
reply = b''.join(chunks)
os.write(1, b'REPLY_HEX:' + reply.hex().encode() + b'\\r\\nEND\\r\\n')
time.sleep(0.2)
"""
        terminal = AttachTerminal(
            [sys.executable, "-c", child],
            "/tmp",
            TerminalTheme(),
        )
        window = Gtk.Window()
        try:
            window.add(terminal)
            window.resize(800, 500)
            window.show_all()
            deadline = time.monotonic() + 3.0
            captured = ""
            while time.monotonic() < deadline:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                captured = terminal.terminal.get_text_format(Vte.Format.TEXT)
                if "END" in captured:
                    break
                time.sleep(0.01)

            self.assertIn("REPLY_HEX:1b5b", captured)
            self.assertNotIn("^[[", captured)
            self.assertNotIn("VTE(", captured)
        finally:
            terminal.detach()
            window.destroy()


if __name__ == "__main__":
    unittest.main()
