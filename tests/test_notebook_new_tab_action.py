from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from termdeck.gtk_app import Gtk, _install_notebook_end_action


GTK_APP = (Path(__file__).resolve().parents[1] / "termdeck" / "gtk_app.py").read_text(
    encoding="utf-8"
)


class NotebookNewTabActionTests(TestCase):
    def test_termdeck_window_uses_notebook_action_not_floating_overlay(self) -> None:
        self.assertIn(
            "_install_notebook_end_action(self.notebook, strip_new_tab)",
            GTK_APP,
        )
        self.assertNotIn("root.add_overlay(strip_new_tab)", GTK_APP)
        self.assertNotIn("def _position_strip_new_tab", GTK_APP)
        self.assertNotIn("_queue_strip_new_tab_position", GTK_APP)

    def test_end_action_survives_thirty_tab_overflow(self) -> None:
        ready, _argv = Gtk.init_check([])
        if not ready:
            self.skipTest("GTK display is unavailable")
        window = Gtk.Window()
        notebook = Gtk.Notebook()
        notebook.set_scrollable(True)
        button = Gtk.Button(label="+")
        _install_notebook_end_action(notebook, button)
        for index in range(30):
            notebook.append_page(Gtk.Label(label=f"page-{index}"), Gtk.Label(label=f"tab-{index}"))
        window.add(notebook)
        window.resize(360, 240)
        window.show_all()
        try:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            assert notebook.get_action_widget(Gtk.PackType.END) is button
            assert button.get_visible()
            assert button.get_mapped()
        finally:
            window.destroy()
