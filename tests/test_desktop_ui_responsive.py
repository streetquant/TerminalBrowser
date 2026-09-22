"""Focused guards for the compact GTK header and keyboard navigation chrome."""

import os
from pathlib import Path

import pytest

from termdeck.ui_theme import build_chrome_css


ROOT = Path(__file__).resolve().parents[1]
GTK_SOURCE = (ROOT / "termdeck" / "gtk_app.py").read_text(encoding="utf-8")


def test_account_selector_has_a_bounded_header_layout() -> None:
    assert "_configure_header_account_combo(auth_combo)" in GTK_SOURCE


def test_account_selector_runtime_bounds_long_state_labels() -> None:
    if not os.environ.get("DISPLAY"):
        pytest.skip("GTK allocation needs a display; visual smoke covers this path")

    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from termdeck.gtk_app import _configure_header_account_combo

    window = Gtk.Window()
    combo = Gtk.ComboBoxText()
    _configure_header_account_combo(combo)
    combo.append(
        "long-account",
        "Codex: choose account · upgrades terminal with a very long provider label",
    )
    combo.set_active_id("long-account")
    window.add(combo)
    window.show_all()
    try:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        minimum, natural = combo.get_preferred_width()
        assert minimum <= 170
        assert natural <= 170
        assert combo.get_cells()[0].get_property("width") == 112
        assert combo.get_accessible().get_name() == "Codex account"
    finally:
        window.destroy()


def test_header_and_tab_strip_expose_keyboard_focus() -> None:
    css = build_chrome_css("dark", "comfortable")
    for selector in (
        "combobox.td-auth-account:focus",
        "headerbar button.td-header-icon:focus",
        "notebook.td-terminal-notebook tab:focus",
    ):
        assert selector in css
    assert "outline-offset: 1px" in css
    assert "outline-offset: -2px" in css
