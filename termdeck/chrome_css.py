"""GTK3 chrome stylesheet for TermDeck.

All colors are resolved to plain rgba() at Python-time so GTK3's
GtkCssProvider accepts every rule.  GTK Sass helpers (shade, alpha) and
box-shadow are NOT used here because they are only valid inside GTK engine
theme files, not in application-level CSS providers loaded at
STYLE_PROVIDER_PRIORITY_APPLICATION.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Color helpers — compute at Python time so the CSS is plain rgba().
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    a = max(0.0, min(1.0, alpha))
    return f"rgba({r},{g},{b},{a:.3f})"


def _shade(hex_color: str, factor: float) -> str:
    """Lighten (factor>1) or darken (factor<1) a hex color."""
    r, g, b = _hex_to_rgb(hex_color)
    r2 = max(0, min(255, int(r * factor)))
    g2 = max(0, min(255, int(g * factor)))
    b2 = max(0, min(255, int(b * factor)))
    return f"#{r2:02x}{g2:02x}{b2:02x}"


# ---------------------------------------------------------------------------
# Main CSS builder
# ---------------------------------------------------------------------------

def build_modern_chrome_css(
    *,
    tokens,
    density: str,
    ui_font: str | None,
    scale: float,
    font_family_css: str,
    size: dict[str, str],
    pad: int,
    margin: int,
    radius: int,
) -> str:
    t = tokens  # shorthand

    # Pre-compute every rgba/shade value used in the stylesheet.
    # Background
    bg_light     = _shade(t.bg, 1.14)
    bg_dark      = _shade(t.bg, 0.92)

    # Buttons
    btn_bg       = _rgba(t.fg, 0.045)
    btn_bg_hi    = _rgba(t.fg, 0.075)
    btn_bg_lo    = _rgba(t.fg, 0.028)
    btn_border   = _rgba(t.border, 0.88)
    btn_hov_hi   = _rgba(t.accent, 0.16)
    btn_hov_lo   = _rgba(t.accent, 0.07)
    btn_hov_b    = _rgba(t.accent, 0.44)
    btn_act_hi   = _rgba(t.accent, 0.20)
    btn_act_lo   = _rgba(t.accent, 0.13)
    btn_foc_b    = _rgba(t.accent, 0.80)

    # HeaderBar
    hb_bg_light  = _shade(t.bg_raised, 1.10)
    hb_border    = _rgba(t.accent, 0.20)
    hb_hov_bg    = _rgba(t.accent, 0.12)
    hb_hov_b     = _rgba(t.accent, 0.32)
    hb_act_bg    = _rgba(t.accent, 0.20)

    # Primary / accent button
    acc_light    = _shade(t.accent, 1.20)
    acc_mid      = _shade(t.accent, 0.84)
    acc_border   = _rgba(t.accent, 0.90)
    acc_hov_1    = _shade(t.accent, 1.30)
    acc_hov_2    = _shade(t.accent, 1.06)
    acc_hov_3    = _shade(t.accent, 0.92)
    acc_hov_b    = _shade(t.accent, 1.14)

    # Ghost / danger buttons
    ghost_bg     = _rgba(t.fg, 0.035)
    ghost_border = _rgba(t.border, 0.90)
    ghost_hov_bg = _rgba(t.accent, 0.10)
    ghost_hov_b  = _rgba(t.accent, 0.36)
    dng_bg       = _rgba(t.danger, 0.12)
    dng_bg_hi    = _rgba(t.danger, 0.17)
    dng_bg_lo    = _rgba(t.danger, 0.09)
    dng_border   = _rgba(t.danger, 0.38)
    dng_hov_hi   = _rgba(t.danger, 0.26)
    dng_hov_lo   = _rgba(t.danger, 0.15)
    dng_hov_b    = _rgba(t.danger, 0.62)

    # Overflow / new-tab
    ovf_bg       = _rgba(t.fg, 0.050)
    ovf_border   = _rgba(t.border, 0.78)
    ntab_hi      = _rgba(t.accent, 0.16)
    ntab_lo      = _rgba(t.accent, 0.08)
    ntab_border  = _rgba(t.accent, 0.32)
    ntab_hov_hi  = _rgba(t.accent, 0.24)
    ntab_hov_lo  = _rgba(t.accent, 0.13)
    ntab_hov_b   = _rgba(t.accent, 0.55)

    # Side pane
    side_bg_lt   = _shade(t.bg_raised, 1.06)
    side_bg_dk   = _shade(t.bg_raised, 0.96)
    side_border  = _rgba(t.border, 0.82)

    # Count pill / chip
    pill_hi      = _rgba(t.accent, 0.20)
    pill_lo      = _rgba(t.accent, 0.10)
    pill_border  = _rgba(t.accent, 0.28)
    chip_hi      = _rgba(t.accent, 0.20)
    chip_lo      = _rgba(t.accent, 0.11)
    chip_border  = _rgba(t.accent, 0.24)

    # Session card
    card_bg      = _rgba(t.fg, 0.030)
    card_bg_hi   = _rgba(t.fg, 0.045)
    card_bg_lo   = _rgba(t.fg, 0.015)
    card_border  = _rgba(t.border, 0.78)
    card_hov_hi  = _rgba(t.accent, 0.10)
    card_hov_lo  = _rgba(t.accent, 0.045)
    card_hov_b   = _rgba(t.accent, 0.50)
    card_sel_hi  = _rgba(t.accent, 0.17)
    card_sel_lo  = _rgba(t.accent, 0.07)
    card_sel_b   = _rgba(t.accent, 0.80)
    card_sel_bar = t.accent

    # Empty state
    emp_hi       = _rgba(t.accent, 0.045)
    emp_lo       = _rgba(t.fg, 0.015)
    emp_border   = _rgba(t.accent, 0.38)

    # Badges
    ok_bg    = _rgba(t.success, 0.14); ok_b  = _rgba(t.success, 0.26)
    wn_bg    = _rgba(t.warning, 0.14); wn_b  = _rgba(t.warning, 0.26)
    dn_bg    = _rgba(t.danger,  0.14); dn_b  = _rgba(t.danger,  0.26)
    mu_bg    = _rgba(t.fg_muted, 0.12); mu_b = _rgba(t.fg_muted, 0.18)
    inf_bg   = _rgba(t.accent,  0.14); inf_b = _rgba(t.accent,  0.26)

    # Entry / focus
    ent_border   = _rgba(t.border, 0.95)
    ent_foc_b    = _rgba(t.accent, 0.85)
    ent_foc_ring = _rgba(t.accent, 0.20)

    # Check
    chk_border   = _rgba(t.border, 0.95)

    # List row
    row_border   = _rgba(t.border, 0.55)
    row_hov_bg   = _rgba(t.accent, 0.07)

    # Notebook
    nb_border    = _rgba(t.border, 0.78)
    nb_hdr_light = _shade(t.bg_raised, 1.08)
    nb_hdr_b     = _rgba(t.border, 0.68)
    nb_tab_hov_bg= _rgba(t.fg, 0.055)
    nb_tab_hov_b = _rgba(t.border, 0.55)
    nb_tab_sel_hi= _rgba(t.accent, 0.20)
    nb_tab_sel_lo= _rgba(t.accent, 0.09)
    nb_tab_sel_b = _rgba(t.accent, 0.42)
    nb_tab_sel_u = t.accent    # underline color (solid)

    # Tab close
    tc_hov_bg    = _rgba(t.danger, 0.16)

    # Bottom bar
    bb_bg        = _rgba(t.fg, 0.025)
    bb_bg_hi     = _rgba(t.fg, 0.04)
    bb_bg_lo     = _rgba(t.fg, 0.012)
    bb_border    = _rgba(t.border, 0.72)
    more_border  = _rgba(t.border, 0.82)

    # Toast
    toast_border = _rgba(t.accent, 0.30)
    ts_ok_b      = _rgba(t.success, 0.60)
    ts_wn_b      = _rgba(t.warning, 0.60)
    ts_dn_b      = _rgba(t.danger,  0.60)

    # Scrolled window
    sw_border    = _rgba(t.border, 0.72)

    # Treeview header
    th_bg_light  = _shade(t.bg_raised, 1.06)

    # Menu / popover
    mn_bg_light  = _shade(t.bg_raised, 1.07)
    mn_border    = _rgba(t.accent, 0.22)
    mi_hov_bg    = _rgba(t.accent, 0.14)
    sep_bg       = _rgba(t.border, 0.72)

    # Tooltip
    tt_border    = _rgba(t.accent, 0.26)

    # Treeview selected
    tv_sel_bg    = _rgba(t.accent, 0.26)

    # Scrollbar
    sb_slider    = _rgba(t.fg_muted, 0.30)
    sb_sl_hov    = _rgba(t.accent, 0.60)
    sb_term_sl   = _rgba(t.fg_muted, 0.82)
    sb_term_border = _rgba(t.border, 0.90)
    sb_term_trough_b = _rgba(t.border, 0.72)
    sb_term_fg_b = _rgba(t.fg, 0.22)
    sb_term_hov  = _rgba(t.accent, 0.72)

    return f"""\
/* TermDeck chrome — theme={t.name} density={density} ui_font={ui_font or "system"} scale={scale:.2f} */
* {{
  {font_family_css}
  font-size: {size["base"]};
}}
window, dialog, messagedialog {{
  background-color: {t.bg};
  color: {t.fg};
}}
window.background {{ background-color: {t.bg}; }}
.td-main-root {{
  background-color: {t.bg};
  background-image: linear-gradient(to bottom, {bg_light}, {t.bg} 38%, {bg_dark});
}}
button {{
  background-color: {btn_bg};
  background-image: linear-gradient(to bottom, {btn_bg_hi}, {btn_bg_lo});
  color: {t.fg};
  border: 1px solid {btn_border};
  border-radius: 11px;
  padding: 6px 12px;
}}
button:hover {{
  background-image: linear-gradient(to bottom, {btn_hov_hi}, {btn_hov_lo});
  border-color: {btn_hov_b};
}}
button:active {{
  background-image: linear-gradient(to bottom, {btn_act_hi}, {btn_act_lo});
}}
button:focus {{
  border-color: {btn_foc_b};
  outline: 2px solid {_rgba(t.accent, 0.22)};
  outline-offset: 1px;
}}
headerbar {{
  background-color: {t.bg_raised};
  background-image: linear-gradient(to bottom, {hb_bg_light}, {t.bg_raised});
  border: none;
  border-bottom: 1px solid {hb_border};
  padding: 5px 10px;
  min-height: 46px;
}}
headerbar label, headerbar button {{ color: {t.fg}; }}
headerbar button {{
  background-image: none;
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 12px;
  padding: 6px 10px;
  margin: 0 2px;
  min-height: 32px;
}}
headerbar button:hover {{
  background-color: {hb_hov_bg};
  border-color: {hb_hov_b};
}}
headerbar button:active {{ background-color: {hb_act_bg}; }}
button.td-primary-action, headerbar button.suggested-action, button.td-accent {{
  background-color: {t.accent};
  background-image: linear-gradient(to bottom right, {acc_light}, {t.accent} 52%, {acc_mid});
  color: {t.accent_fg};
  border: 1px solid {acc_border};
  border-radius: 10px;
  font-weight: 700;
}}
button.td-primary-action:hover, headerbar button.suggested-action:hover, button.td-accent:hover {{
  background-image: linear-gradient(to bottom right, {acc_hov_1}, {acc_hov_2} 52%, {acc_hov_3});
  border-color: {acc_hov_b};
}}
button.td-ghost-action {{
  background-image: none;
  background-color: {ghost_bg};
  color: {t.fg};
  border: 1px solid {ghost_border};
  border-radius: 10px;
}}
button.td-ghost-action:hover {{
  background-color: {ghost_hov_bg};
  border-color: {ghost_hov_b};
}}
button.td-danger-action, button.destructive-action {{
  background-image: linear-gradient(to bottom, {dng_bg_hi}, {dng_bg_lo});
  background-color: {dng_bg};
  color: {t.danger};
  border: 1px solid {dng_border};
  border-radius: 10px;
  font-weight: 700;
}}
button.td-danger-action:hover, button.destructive-action:hover {{
  background-image: linear-gradient(to bottom, {dng_hov_hi}, {dng_hov_lo});
  border-color: {dng_hov_b};
}}
button.td-header-overflow {{
  background-color: {ovf_bg};
  border-color: {ovf_border};
}}
button.td-new-tab {{
  background-image: linear-gradient(to bottom, {ntab_hi}, {ntab_lo});
  color: {t.accent};
  border-color: {ntab_border};
}}
button.td-new-tab:hover {{
  background-image: linear-gradient(to bottom, {ntab_hov_hi}, {ntab_hov_lo});
  border-color: {ntab_hov_b};
}}
button.td-header-icon {{
  min-width: 36px;
  padding-left: 8px;
  padding-right: 8px;
}}
headerbar button.td-new-tab.td-header-icon {{
  min-width: 38px;
  min-height: 38px;
  padding: 0;
  border-radius: 10px;
}}
headerbar button.td-header-group-end {{ margin-left: 8px; }}
.td-title {{
  font-weight: 800;
  font-size: {size["title"]};
  color: {t.fg};
}}
.td-section-description {{
  color: {t.fg_muted};
  font-size: {size["muted"]};
  padding: 2px 2px 8px 2px;
}}
.td-subtitle, .dim-label, .td-muted {{
  color: {t.fg_muted};
  font-size: {size["muted"]};
}}
.td-eyebrow {{
  color: {t.accent};
  font-size: {size["chip"]};
  font-weight: 800;
}}
.td-side-pane {{
  background-color: {t.bg_raised};
  background-image: linear-gradient(to bottom, {side_bg_lt}, {t.bg_raised} 55%, {side_bg_dk});
  border: 1px solid {side_border};
  border-radius: 14px;
  padding: {pad}px;
  margin: {margin}px;
}}
.td-count-pill {{
  background-image: linear-gradient(to bottom, {pill_hi}, {pill_lo});
  color: {t.accent};
  border: 1px solid {pill_border};
  border-radius: 999px;
  padding: 3px 9px;
  font-size: {size["chip"]};
  font-weight: 700;
}}
.td-session-card {{
  background-color: {card_bg};
  background-image: linear-gradient(to bottom, {card_bg_hi}, {card_bg_lo});
  border: 1px solid {card_border};
  border-radius: {radius}px;
  padding: {pad}px;
  margin: {margin // 2}px 0;
}}
.td-session-card:hover {{
  background-image: linear-gradient(to bottom, {card_hov_hi}, {card_hov_lo});
  border-color: {card_hov_b};
}}
.td-session-card.td-session-selected {{
  background-image: linear-gradient(to right, {card_sel_hi}, {card_sel_lo});
  border-color: {card_sel_b};
  border-left: 3px solid {card_sel_bar};
}}
.td-chip, .td-count-pill {{ border-radius: 999px; }}
.td-chip {{
  background-image: linear-gradient(to bottom, {chip_hi}, {chip_lo});
  color: {t.accent};
  border: 1px solid {chip_border};
  padding: 3px 9px;
  font-size: {size["chip"]};
  font-weight: 700;
}}
.td-badge {{
  border-radius: 999px;
  padding: 3px 9px;
  font-size: {size["chip"]};
  font-weight: 700;
}}
.td-badge-ok    {{ background-color: {ok_bg};  color: {t.success};  border: 1px solid {ok_b};  }}
.td-badge-warn  {{ background-color: {wn_bg};  color: {t.warning};  border: 1px solid {wn_b};  }}
.td-badge-danger{{ background-color: {dn_bg};  color: {t.danger};   border: 1px solid {dn_b};  }}
.td-badge-muted {{ background-color: {mu_bg};  color: {t.fg_muted}; border: 1px solid {mu_b};  }}
.td-badge-info  {{ background-color: {inf_bg}; color: {t.accent};   border: 1px solid {inf_b}; }}
.td-resource {{ font-family: {t.mono}; font-size: {size["chip"]}; color: {t.fg_muted}; }}
.td-mono, .td-accel, .td-id {{ font-family: {t.mono}; font-size: {size["mono"]}; }}
.td-empty-state {{
  background-image: linear-gradient(to bottom, {emp_hi}, {emp_lo});
  border: 1px dashed {emp_border};
  border-radius: {radius}px;
  padding: 24px 18px;
  margin: 8px 0;
}}
.td-empty-icon {{ color: {t.accent}; font-size: 28px; font-weight: 700; }}
.td-table-window, .td-overlay-dialog {{ background-color: {t.bg}; }}
.td-overlay-dialog border, .td-overlay-dialog {{ border-radius: 16px; }}
entry, .td-search-entry entry, entry.td-search-entry {{
  background-color: {t.bg_sunken};
  border: 1px solid {ent_border};
  border-radius: 11px;
  padding: 9px 13px;
  color: {t.fg};
  min-height: 36px;
  font-size: {size["base"]};
  caret-color: {t.accent};
}}
entry:focus, .td-search-entry entry:focus, entry.td-search-entry:focus {{
  border-color: {ent_foc_b};
  outline: 2px solid {ent_foc_ring};
  outline-offset: 1px;
}}
checkbutton check {{
  background-color: {t.bg_sunken};
  border: 1px solid {chk_border};
  border-radius: 5px;
  min-width: 16px;
  min-height: 16px;
}}
checkbutton check:checked {{ background-color: {t.accent}; border-color: {t.accent}; color: {t.accent_fg}; }}
.td-list-row {{ padding: {pad}px; border-bottom: 1px solid {row_border}; }}
.td-list-row:hover {{ background-color: {row_hov_bg}; }}
.td-category {{ font-size: {size["chip"]}; font-weight: 800; color: {t.accent}; }}
notebook {{ background-color: {t.bg_sunken}; border: none; }}
notebook.td-terminal-notebook {{
  background-color: {t.bg_sunken};
  border: 1px solid {nb_border};
  border-radius: 14px;
  margin: 6px 8px 8px 8px;
}}
notebook.td-terminal-notebook header {{
  background-color: {t.bg_raised};
  background-image: linear-gradient(to bottom, {nb_hdr_light}, {t.bg_raised});
  border: none;
  border-bottom: 1px solid {nb_hdr_b};
  border-radius: 13px 13px 0 0;
  padding: 5px 6px;
}}
notebook.td-terminal-notebook header tabs {{ background-color: transparent; border-color: transparent; }}
notebook.td-terminal-notebook tab {{
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 10px;
  padding: 6px 11px;
  margin: 1px 3px;
  color: {t.fg_muted};
  font-size: {size["base"]};
}}
notebook.td-terminal-notebook tab:hover {{
  background-color: {nb_tab_hov_bg};
  border-color: {nb_tab_hov_b};
  color: {t.fg};
}}
notebook.td-terminal-notebook tab:checked {{
  background-image: linear-gradient(to bottom, {nb_tab_sel_hi}, {nb_tab_sel_lo});
  color: {t.fg};
  border-color: {nb_tab_sel_b};
  border-bottom: 2px solid {nb_tab_sel_u};
}}
notebook.td-terminal-notebook > stack {{
  background-color: {t.bg_sunken};
  border-radius: 0 0 13px 13px;
}}
button.td-tab-close {{
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  border: none;
  border-radius: 999px;
  background-color: transparent;
  background-image: none;
  color: {t.fg_muted};
  font-size: {size["base"]};
  opacity: 0.52;
}}
notebook.td-terminal-notebook tab:checked button.td-tab-close {{ opacity: 0.82; }}
button.td-tab-close:hover {{
  background-color: {tc_hov_bg};
  color: {t.danger};
  opacity: 1;
}}
.td-bottom-bar {{
  background-color: {bb_bg};
  background-image: linear-gradient(to top, {bb_bg_hi}, {bb_bg_lo});
  border: 1px solid {bb_border};
  border-radius: 12px;
  padding: 6px;
}}
.td-bottom-bar button, headerbar button, button {{ font-size: {size["button"]}; min-height: 32px; }}
.td-more-actions {{ background-color: transparent; border: 1px solid {more_border}; border-radius: 10px; }}
.td-toast {{
  background-color: {t.bg_raised};
  background-image: linear-gradient(to bottom, {_shade(t.bg_raised, 1.10)}, {t.bg_raised});
  border: 1px solid {toast_border};
  border-radius: 12px;
  padding: 11px 17px;
  color: {t.fg};
  font-size: {size["base"]};
}}
.td-toast.td-toast-success {{ border-color: {ts_ok_b}; }}
.td-toast.td-toast-warn    {{ border-color: {ts_wn_b}; }}
.td-toast.td-toast-danger  {{ border-color: {ts_dn_b}; }}
scrolledwindow {{
  background-color: {t.bg_sunken};
  border: 1px solid {sw_border};
  border-radius: 12px;
}}
textview, textview text {{
  background-color: {t.bg_sunken};
  color: {t.fg};
}}
treeview {{ background-color: {t.bg_sunken}; color: {t.fg}; border: none; font-size: {size["base"]}; }}
treeview:selected {{ background-color: {tv_sel_bg}; color: {t.fg}; }}
treeview header button {{
  background-image: linear-gradient(to bottom, {th_bg_light}, {t.bg_raised});
  background-color: {t.bg_raised};
  color: {t.fg_muted};
  border: none;
  border-bottom: 1px solid {t.border};
  font-weight: 700;
  font-size: {size["button"]};
  padding: 9px 11px;
}}
menu, popover {{
  background-color: {t.bg_raised};
  background-image: linear-gradient(to bottom, {mn_bg_light}, {t.bg_raised});
  color: {t.fg};
  border: 1px solid {mn_border};
  border-radius: 12px;
  padding: 6px;
}}
menuitem {{ border-radius: 9px; padding: 8px 11px; margin: 1px 0; }}
menuitem:hover {{ background-color: {mi_hov_bg}; color: {t.fg}; }}
separator {{ background-color: {sep_bg}; min-height: 1px; margin: 5px 4px; }}
tooltip {{
  background-color: {t.bg_raised};
  color: {t.fg};
  border: 1px solid {tt_border};
  border-radius: 8px;
  padding: 7px 10px;
}}
paned > separator {{ background-color: transparent; min-width: 1px; min-height: 1px; }}
scrollbar {{ background-color: transparent; }}
scrollbar slider {{
  background-color: {sb_slider};
  border-radius: 99px;
  min-width: 6px;
  min-height: 20px;
}}
scrollbar slider:hover {{ background-color: {sb_sl_hov}; }}
scrollbar.td-terminal-scrollbar {{
  opacity: 1;
  background-image: none;
  background-color: {t.bg_raised};
  border-left: 1px solid {sb_term_border};
  min-width: 14px;
}}
scrollbar.td-terminal-scrollbar trough {{
  background-image: none;
  background-color: {t.bg_sunken};
  border-left: 1px solid {sb_term_trough_b};
  min-width: 12px;
}}
scrollbar.td-terminal-scrollbar slider {{
  background-image: none;
  background-color: {sb_term_sl};
  border: 1px solid {sb_term_fg_b};
  min-width: 8px;
  min-height: 24px;
  margin: 2px;
}}
scrollbar.td-terminal-scrollbar slider:hover,
scrollbar.td-terminal-scrollbar slider:active {{
  background-color: {sb_term_hov};
}}
.td-density-compact * {{ font-size: {size["compact"]}; }}
.td-density-compact .td-session-card {{ padding: 6px 8px; border-radius: 10px; }}
.td-density-compact notebook.td-terminal-notebook {{ margin: 6px 8px 8px 8px; border-radius: 13px; }}
.td-density-compact notebook.td-terminal-notebook header {{ border-radius: 12px 12px 0 0; padding: 5px 6px; }}
.td-density-compact notebook.td-terminal-notebook tab {{ padding: 5px 8px; margin: 1px 2px; }}
.td-density-compact treeview {{ font-size: {size["compact"]}; }}
"""
