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

    # -----------------------------------------------------------------------
    # Pre-compute every rgba/shade value used in the stylesheet.
    # All alpha values are raised so elements are VISIBLE on dark monitors.
    # -----------------------------------------------------------------------

    # Buttons — raised fill so they actually look like buttons
    btn_bg       = _rgba(t.fg, 0.10)
    btn_border   = _rgba(t.border, 1.00)
    btn_hov_bg   = _rgba(t.accent, 0.22)
    btn_hov_b    = _rgba(t.accent, 0.65)
    btn_act_bg   = _rgba(t.accent, 0.32)
    btn_foc_b    = _rgba(t.accent, 0.90)

    # HeaderBar — flat, just clearly raised bg
    hb_border    = _rgba(t.border, 0.80)
    hb_hov_bg    = _rgba(t.accent, 0.18)
    hb_hov_b     = _rgba(t.accent, 0.50)
    hb_act_bg    = _rgba(t.accent, 0.30)

    # Primary / accent button
    acc_light    = _shade(t.accent, 1.18)
    acc_mid      = _shade(t.accent, 0.86)
    acc_border   = _rgba(t.accent, 0.95)
    acc_hov_1    = _shade(t.accent, 1.28)
    acc_hov_2    = _shade(t.accent, 1.08)
    acc_hov_3    = _shade(t.accent, 0.94)
    acc_hov_b    = _shade(t.accent, 1.16)

    # Ghost / danger buttons — visible fills
    ghost_bg     = _rgba(t.fg, 0.08)
    ghost_border = _rgba(t.border, 1.00)
    ghost_hov_bg = _rgba(t.accent, 0.20)
    ghost_hov_b  = _rgba(t.accent, 0.55)
    dng_bg       = _rgba(t.danger, 0.18)
    dng_border   = _rgba(t.danger, 0.55)
    dng_hov_bg   = _rgba(t.danger, 0.28)
    dng_hov_b    = _rgba(t.danger, 0.80)

    # Overflow / new-tab
    ovf_bg       = _rgba(t.fg, 0.08)
    ovf_border   = _rgba(t.border, 0.90)
    ntab_bg      = _rgba(t.accent, 0.18)
    ntab_border  = _rgba(t.accent, 0.55)
    ntab_hov_bg  = _rgba(t.accent, 0.28)
    ntab_hov_b   = _rgba(t.accent, 0.75)

    # Side pane — flat raised surface, clear border
    side_border  = _rgba(t.border, 0.90)

    # Count pill / chip
    pill_bg      = _rgba(t.accent, 0.22)
    pill_border  = _rgba(t.accent, 0.45)
    chip_bg      = _rgba(t.accent, 0.22)
    chip_border  = _rgba(t.accent, 0.40)

    # Session card — visible card surface
    card_bg      = _rgba(t.fg, 0.055)
    card_border  = _rgba(t.border, 0.90)
    card_hov_bg  = _rgba(t.accent, 0.15)
    card_hov_b   = _rgba(t.accent, 0.65)
    card_sel_bg  = _rgba(t.accent, 0.22)
    card_sel_b   = _rgba(t.accent, 0.90)
    card_sel_bar = t.accent

    # Empty state
    emp_bg       = _rgba(t.accent, 0.07)
    emp_border   = _rgba(t.accent, 0.45)

    # Badges — more readable fills
    ok_bg    = _rgba(t.success, 0.20); ok_b  = _rgba(t.success, 0.40)
    wn_bg    = _rgba(t.warning, 0.20); wn_b  = _rgba(t.warning, 0.40)
    dn_bg    = _rgba(t.danger,  0.20); dn_b  = _rgba(t.danger,  0.40)
    mu_bg    = _rgba(t.fg_muted, 0.16); mu_b = _rgba(t.fg_muted, 0.28)
    inf_bg   = _rgba(t.accent,  0.18); inf_b = _rgba(t.accent,  0.36)

    # Entry / focus
    ent_border   = _rgba(t.border, 1.00)
    ent_foc_b    = _rgba(t.accent, 0.90)
    ent_foc_ring = _rgba(t.accent, 0.28)

    # Check
    chk_border   = _rgba(t.border, 1.00)

    # List row
    row_border   = _rgba(t.border, 0.70)
    row_hov_bg   = _rgba(t.accent, 0.12)

    # Notebook — clearly visible borders and STRONG selected-tab indicator
    nb_border    = _rgba(t.border, 0.90)
    nb_hdr_b     = _rgba(t.border, 0.80)
    nb_tab_hov_bg= _rgba(t.fg, 0.09)
    nb_tab_hov_b = _rgba(t.border, 0.80)
    nb_tab_sel_bg= _rgba(t.accent, 0.28)
    nb_tab_sel_b = _rgba(t.accent, 0.70)
    nb_tab_sel_u = t.accent    # underline color (solid)

    # Tab close
    tc_hov_bg    = _rgba(t.danger, 0.22)

    # Bottom bar — flat with visible border
    bb_bg        = _rgba(t.fg, 0.05)
    bb_border    = _rgba(t.border, 0.82)
    more_border  = _rgba(t.border, 0.90)

    # Toast — clearly raised above bg
    toast_border = _rgba(t.accent, 0.50)
    ts_ok_b      = _rgba(t.success, 0.75)
    ts_wn_b      = _rgba(t.warning, 0.75)
    ts_dn_b      = _rgba(t.danger,  0.75)

    # Scrolled window
    sw_border    = _rgba(t.border, 0.80)

    # Menu / popover
    mn_bg_light  = _shade(t.bg_raised, 1.10)
    mn_border    = _rgba(t.accent, 0.40)
    mi_hov_bg    = _rgba(t.accent, 0.20)
    sep_bg       = _rgba(t.border, 0.80)

    # Tooltip
    tt_border    = _rgba(t.accent, 0.45)

    # Treeview selected
    tv_sel_bg    = _rgba(t.accent, 0.32)

    # Scrollbar — stronger contrast so the thumb is clearly visible
    sb_slider    = _rgba(t.fg_muted, 0.42)
    sb_sl_hov    = _rgba(t.accent, 0.75)
    sb_term_sl   = _rgba(t.fg_muted, 0.88)
    sb_term_border = _rgba(t.border, 1.00)
    sb_term_trough_b = _rgba(t.border, 0.80)
    sb_term_fg_b = _rgba(t.fg, 0.30)
    sb_term_hov  = _rgba(t.accent, 0.85)

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
}}
button {{
  background-color: {btn_bg};
  background-image: none;
  color: {t.fg};
  border: 1px solid {btn_border};
  border-radius: 9px;
  padding: 6px 14px;
}}
button:hover {{
  background-color: {btn_hov_bg};
  border-color: {btn_hov_b};
}}
button:active {{
  background-color: {btn_act_bg};
}}
button:focus {{
  border-color: {btn_foc_b};
  outline: 2px solid {_rgba(t.accent, 0.28)};
  outline-offset: 1px;
}}
headerbar {{
  background-color: {t.bg_raised};
  background-image: none;
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
  border-radius: 9px;
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
  border-radius: 9px;
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
  border-radius: 9px;
}}
button.td-ghost-action:hover {{
  background-color: {ghost_hov_bg};
  border-color: {ghost_hov_b};
}}
button.td-danger-action, button.destructive-action {{
  background-image: none;
  background-color: {dng_bg};
  color: {t.danger};
  border: 1px solid {dng_border};
  border-radius: 9px;
  font-weight: 700;
}}
button.td-danger-action:hover, button.destructive-action:hover {{
  background-color: {dng_hov_bg};
  border-color: {dng_hov_b};
}}
button.td-header-overflow {{
  background-color: {ovf_bg};
  border-color: {ovf_border};
}}
button.td-new-tab {{
  background-color: {ntab_bg};
  background-image: none;
  color: {t.accent};
  border-color: {ntab_border};
  font-weight: 600;
}}
button.td-new-tab:hover {{
  background-color: {ntab_hov_bg};
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
  border-radius: 9px;
}}
headerbar button.td-header-group-end {{ margin-left: 8px; }}
.td-title {{
  font-weight: 700;
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
  font-weight: 700;
}}
.td-side-pane {{
  background-color: {t.bg_raised};
  background-image: none;
  border: 1px solid {side_border};
  border-radius: 12px;
  padding: {pad}px;
  margin: {margin}px;
}}
.td-count-pill {{
  background-color: {pill_bg};
  background-image: none;
  color: {t.accent};
  border: 1px solid {pill_border};
  border-radius: 999px;
  padding: 3px 9px;
  font-size: {size["chip"]};
  font-weight: 700;
}}
.td-session-card {{
  background-color: {card_bg};
  background-image: none;
  border: 1px solid {card_border};
  border-radius: {radius}px;
  padding: {pad}px;
  margin: {margin // 2}px 0;
}}
.td-session-card:hover {{
  background-color: {card_hov_bg};
  border-color: {card_hov_b};
}}
.td-session-card.td-session-selected {{
  background-color: {card_sel_bg};
  background-image: none;
  border-color: {card_sel_b};
  border-left: 3px solid {card_sel_bar};
}}
.td-chip, .td-count-pill {{ border-radius: 999px; }}
.td-chip {{
  background-color: {chip_bg};
  background-image: none;
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
  background-color: {emp_bg};
  background-image: none;
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
  border-radius: 9px;
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
.td-category {{ font-size: {size["chip"]}; font-weight: 700; color: {t.accent}; }}
notebook {{ background-color: {t.bg_sunken}; border: none; }}
notebook.td-terminal-notebook {{
  background-color: {t.bg_sunken};
  border: 1px solid {nb_border};
  border-radius: 12px;
  margin: 4px 4px 4px 4px;
}}
notebook.td-terminal-notebook header {{
  background-color: {t.bg_raised};
  background-image: none;
  border: none;
  border-bottom: 1px solid {nb_hdr_b};
  border-radius: 11px 11px 0 0;
  padding: 4px 6px;
}}
notebook.td-terminal-notebook header tabs {{ background-color: transparent; border-color: transparent; }}
notebook.td-terminal-notebook tab {{
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 7px 12px;
  margin: 2px 3px;
  color: {t.fg_muted};
  font-size: {size["base"]};
}}
notebook.td-terminal-notebook tab:hover {{
  background-color: {nb_tab_hov_bg};
  border-color: {nb_tab_hov_b};
  color: {t.fg};
}}
notebook.td-terminal-notebook tab:checked {{
  background-color: {nb_tab_sel_bg};
  background-image: none;
  color: {t.fg};
  border-color: {nb_tab_sel_b};
  border-bottom: 3px solid {nb_tab_sel_u};
  font-weight: 600;
}}
notebook.td-terminal-notebook > stack {{
  background-color: {t.bg_sunken};
  border-radius: 0 0 11px 11px;
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
  opacity: 0.55;
}}
notebook.td-terminal-notebook tab:checked button.td-tab-close {{ opacity: 0.85; }}
button.td-tab-close:hover {{
  background-color: {tc_hov_bg};
  color: {t.danger};
  opacity: 1;
}}
.td-bottom-bar {{
  background-color: {bb_bg};
  background-image: none;
  border: 1px solid {bb_border};
  border-radius: 10px;
  padding: 6px;
}}
.td-bottom-bar button, headerbar button, button {{ font-size: {size["button"]}; min-height: 32px; }}
.td-more-actions {{ background-color: transparent; border: 1px solid {more_border}; border-radius: 9px; }}
.td-toast {{
  background-color: {t.bg_raised};
  background-image: none;
  border: 1px solid {toast_border};
  border-radius: 10px;
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
  border-radius: 10px;
}}
textview, textview text {{
  background-color: {t.bg_sunken};
  color: {t.fg};
}}
treeview {{ background-color: {t.bg_sunken}; color: {t.fg}; border: none; font-size: {size["base"]}; }}
treeview:selected {{ background-color: {tv_sel_bg}; color: {t.fg}; }}
treeview header button {{
  background-image: none;
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
  background-image: none;
  color: {t.fg};
  border: 1px solid {mn_border};
  border-radius: 10px;
  padding: 6px;
}}
menuitem {{ border-radius: 8px; padding: 8px 11px; margin: 1px 0; }}
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
.td-density-compact .td-session-card {{ padding: 6px 8px; border-radius: 9px; }}
.td-density-compact notebook.td-terminal-notebook {{ margin: 2px 2px 2px 2px; border-radius: 11px; }}
.td-density-compact notebook.td-terminal-notebook header {{ border-radius: 10px 10px 0 0; padding: 4px 6px; }}
.td-density-compact notebook.td-terminal-notebook tab {{ padding: 5px 9px; margin: 1px 2px; }}
.td-density-compact treeview {{ font-size: {size["compact"]}; }}
"""
