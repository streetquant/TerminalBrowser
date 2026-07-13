"""GTK3 chrome stylesheet for TermDeck.

Design language: flat, quiet, professional. Neutral layered surfaces
separated by hairline borders, edge-to-edge docked panels, soft shadows
on floating surfaces (menus, popovers, toasts), subtle motion on
interactive elements, and a single restrained accent reserved for
selection, focus, and the primary action — the visual grammar of mature
terminal apps (Terminator, cmux, Warp, VS Code) rather than glossy web
dashboards.

All colors are resolved to plain rgba() at Python-time so GTK3's
GtkCssProvider accepts every rule. Only properties verified against the
local GTK3 CSS parser are used (box-shadow, transition, letter-spacing,
outline-offset and per-corner radii all parse cleanly).
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


def _is_dark(hex_color: str) -> bool:
    r, g, b = _hex_to_rgb(hex_color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128.0


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
    # Neutral interaction states. Hover/active use foreground alpha — never
    # accent washes — so the accent stays meaningful where it does appear.
    # -----------------------------------------------------------------------
    hairline      = _rgba(t.border, 1.00)
    hairline_soft = _rgba(t.border, 0.55)

    hov_bg = _rgba(t.fg, 0.06)
    act_bg = _rgba(t.fg, 0.10)

    # Buttons — flat and quiet, clear but neutral states
    btn_bg       = _rgba(t.fg, 0.05)
    btn_border   = _rgba(t.border, 0.90)
    btn_hov_bg   = _rgba(t.fg, 0.09)
    btn_hov_b    = _rgba(t.fg, 0.24)
    btn_act_bg   = _rgba(t.fg, 0.13)
    foc_ring     = _rgba(t.accent, 0.45)

    # Primary action — flat accent, no gradients
    acc_edge = _shade(t.accent, 0.88)
    acc_hov  = _shade(t.accent, 1.10)
    acc_act  = _shade(t.accent, 0.92)

    # Danger — outline style; fills only on hover
    dng_border = _rgba(t.danger, 0.45)
    dng_hov_bg = _rgba(t.danger, 0.12)
    dng_hov_b  = _rgba(t.danger, 0.70)

    # Chips / pills — neutral gray, small and quiet
    pill_bg     = _rgba(t.fg, 0.07)
    pill_border = _rgba(t.border, 0.80)

    # Session rows — quiet list; selection = accent bar + light tint
    card_hov_bg = _rgba(t.fg, 0.05)
    card_sel_bg = _rgba(t.accent, 0.13)

    # Empty state
    emp_border = _rgba(t.border, 1.00)

    # Badges — subdued semantic tints
    ok_bg  = _rgba(t.success, 0.12); ok_b  = _rgba(t.success, 0.30)
    wn_bg  = _rgba(t.warning, 0.12); wn_b  = _rgba(t.warning, 0.30)
    dn_bg  = _rgba(t.danger,  0.12); dn_b  = _rgba(t.danger,  0.30)
    mu_bg  = _rgba(t.fg_muted, 0.10); mu_b = _rgba(t.fg_muted, 0.24)
    inf_bg = _rgba(t.accent,  0.12); inf_b = _rgba(t.accent,  0.30)

    # Entry focus ring
    ent_foc_ring = _rgba(t.accent, 0.30)

    # List rows
    row_border = _rgba(t.border, 0.45)

    # Notebook tabs — flat strip, accent underline marks the active tab
    tab_hov_bg = _rgba(t.fg, 0.05)
    tab_sel_bg = _rgba(t.fg, 0.07)

    # Tab close — neutral hover, not alarming red
    tc_hov_bg = _rgba(t.fg, 0.12)

    # Menu / popover / toast — slightly lifted surface with a soft shadow
    mn_bg     = _shade(t.bg_raised, 1.06)
    mi_hov_bg = _rgba(t.fg, 0.07)
    shadow    = _rgba("#000000", 0.40 if _is_dark(t.bg) else 0.16)

    # Toast semantic edges
    ts_ok_b = _rgba(t.success, 0.55)
    ts_wn_b = _rgba(t.warning, 0.55)
    ts_dn_b = _rgba(t.danger,  0.55)

    # Treeview selected
    tv_sel_bg = _rgba(t.accent, 0.22)

    # Scrollbars — neutral thumbs, no accent
    sb_slider   = _rgba(t.fg_muted, 0.35)
    sb_sl_hov   = _rgba(t.fg_muted, 0.60)
    sb_term_sl  = _rgba(t.fg_muted, 0.50)
    sb_term_hov = _rgba(t.fg_muted, 0.75)

    compact_card_radius = max(6, radius - 2)

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
  border-radius: 8px;
  padding: 5px 13px;
  transition: background-color 130ms ease-out, border-color 130ms ease-out;
}}
button:hover {{
  background-color: {btn_hov_bg};
  border-color: {btn_hov_b};
}}
button:active {{
  background-color: {btn_act_bg};
}}
button:focus {{
  border-color: {t.accent};
  outline: 2px solid {foc_ring};
  outline-offset: 1px;
}}
headerbar {{
  background-color: {t.bg_raised};
  background-image: none;
  border: none;
  border-bottom: 1px solid {hairline};
  padding: 4px 8px;
  min-height: 46px;
}}
headerbar label, headerbar button {{ color: {t.fg}; }}
headerbar button {{
  background-image: none;
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 5px 10px;
  margin: 0 1px;
  min-height: 30px;
}}
headerbar button:hover {{
  background-color: {hov_bg};
  border-color: transparent;
}}
headerbar button:active {{ background-color: {act_bg}; }}
button.td-primary-action, headerbar button.suggested-action, button.td-accent {{
  background-color: {t.accent};
  background-image: none;
  color: {t.accent_fg};
  border: 1px solid {acc_edge};
  border-radius: 8px;
  font-weight: 600;
}}
button.td-primary-action:hover, headerbar button.suggested-action:hover, button.td-accent:hover {{
  background-color: {acc_hov};
  border-color: {acc_hov};
}}
button.td-primary-action:active, headerbar button.suggested-action:active, button.td-accent:active {{
  background-color: {acc_act};
}}
button.td-ghost-action {{
  background-image: none;
  background-color: transparent;
  color: {t.fg};
  border: 1px solid {hairline};
  border-radius: 8px;
}}
button.td-ghost-action:hover {{
  background-color: {hov_bg};
  border-color: {btn_hov_b};
}}
button.td-danger-action, button.destructive-action {{
  background-image: none;
  background-color: transparent;
  color: {t.danger};
  border: 1px solid {dng_border};
  border-radius: 8px;
  font-weight: 600;
}}
button.td-danger-action:hover, button.destructive-action:hover {{
  background-color: {dng_hov_bg};
  border-color: {dng_hov_b};
}}
button.td-header-overflow {{
  background-color: transparent;
  border-color: transparent;
  color: {t.fg_muted};
}}
button.td-header-overflow:hover {{ color: {t.fg}; }}
button.td-new-tab {{
  background-color: {t.accent};
  background-image: none;
  color: {t.accent_fg};
  border: 1px solid {acc_edge};
  font-weight: 600;
}}
button.td-new-tab:hover {{
  background-color: {acc_hov};
  border-color: {acc_hov};
}}
button.td-new-tab:active {{
  background-color: {acc_act};
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
  border-radius: 8px;
}}
headerbar button.td-header-group-end {{ margin-left: 8px; }}
.td-title {{
  font-weight: 600;
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
  color: {t.fg_muted};
  font-size: {size["chip"]};
  font-weight: 600;
  letter-spacing: 1px;
}}
.td-side-pane {{
  background-color: {t.bg_raised};
  background-image: none;
  border: none;
  border-radius: 0;
  padding: {pad}px;
  margin: 0;
}}
.td-count-pill {{
  background-color: {pill_bg};
  background-image: none;
  color: {t.fg_muted};
  border: 1px solid {pill_border};
  border-radius: 999px;
  padding: 2px 8px;
  font-size: {size["chip"]};
  font-weight: 600;
}}
.td-session-card {{
  background-color: transparent;
  background-image: none;
  border: 1px solid transparent;
  border-left: 2px solid transparent;
  border-radius: {radius}px;
  padding: {pad}px;
  margin: {margin // 2}px 0;
  transition: background-color 130ms ease-out, border-color 130ms ease-out;
}}
.td-session-card:hover {{
  background-color: {card_hov_bg};
}}
.td-session-card.td-session-selected {{
  background-color: {card_sel_bg};
  background-image: none;
  border-left: 2px solid {t.accent};
}}
.td-chip, .td-count-pill {{ border-radius: 999px; }}
.td-chip {{
  background-color: {pill_bg};
  background-image: none;
  color: {t.fg_muted};
  border: 1px solid {pill_border};
  padding: 2px 8px;
  font-size: {size["chip"]};
  font-weight: 600;
}}
.td-badge {{
  border-radius: 999px;
  padding: 2px 8px;
  font-size: {size["chip"]};
  font-weight: 600;
}}
.td-badge-ok    {{ background-color: {ok_bg};  color: {t.success};  border: 1px solid {ok_b};  }}
.td-badge-warn  {{ background-color: {wn_bg};  color: {t.warning};  border: 1px solid {wn_b};  }}
.td-badge-danger{{ background-color: {dn_bg};  color: {t.danger};   border: 1px solid {dn_b};  }}
.td-badge-muted {{ background-color: {mu_bg};  color: {t.fg_muted}; border: 1px solid {mu_b};  }}
.td-badge-info  {{ background-color: {inf_bg}; color: {t.accent};   border: 1px solid {inf_b}; }}
.td-resource {{ font-family: {t.mono}; font-size: {size["chip"]}; color: {t.fg_muted}; }}
.td-mono, .td-accel, .td-id {{ font-family: {t.mono}; font-size: {size["mono"]}; }}
.td-empty-state {{
  background-color: transparent;
  background-image: none;
  border: 1px dashed {emp_border};
  border-radius: {radius}px;
  padding: 24px 18px;
  margin: 8px 0;
}}
.td-empty-icon {{ color: {t.fg_muted}; font-size: 26px; font-weight: 600; }}
.td-table-window, .td-overlay-dialog {{ background-color: {t.bg}; }}
.td-overlay-dialog border, .td-overlay-dialog {{ border-radius: 12px; }}
entry, .td-search-entry entry, entry.td-search-entry {{
  background-color: {t.bg_sunken};
  border: 1px solid {hairline};
  border-radius: 8px;
  padding: 8px 12px;
  color: {t.fg};
  min-height: 34px;
  font-size: {size["base"]};
  caret-color: {t.accent};
  transition: border-color 130ms ease-out;
}}
entry:focus, .td-search-entry entry:focus, entry.td-search-entry:focus {{
  border-color: {t.accent};
  outline: 2px solid {ent_foc_ring};
  outline-offset: 1px;
}}
checkbutton check {{
  background-color: {t.bg_sunken};
  border: 1px solid {hairline};
  border-radius: 4px;
  min-width: 16px;
  min-height: 16px;
}}
checkbutton check:checked {{ background-color: {t.accent}; border-color: {t.accent}; color: {t.accent_fg}; }}
.td-list-row {{
  padding: {pad}px;
  border-bottom: 1px solid {row_border};
  transition: background-color 130ms ease-out;
}}
.td-list-row:hover {{ background-color: {hov_bg}; }}
.td-category {{ font-size: {size["chip"]}; font-weight: 600; color: {t.fg_muted}; letter-spacing: 1px; }}
notebook {{ background-color: {t.bg_sunken}; border: none; }}
notebook.td-terminal-notebook {{
  background-color: {t.bg_sunken};
  border: none;
  border-radius: 0;
  margin: 0;
}}
notebook.td-terminal-notebook header {{
  background-color: {t.bg_raised};
  background-image: none;
  border: none;
  border-bottom: 1px solid {hairline};
  border-radius: 0;
  padding: 0 4px;
}}
notebook.td-terminal-notebook header tabs {{ background-color: transparent; border-color: transparent; }}
notebook.td-terminal-notebook tab {{
  background-color: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  padding: 8px 14px;
  margin: 0 1px;
  color: {t.fg_muted};
  font-size: {size["base"]};
  transition: background-color 130ms ease-out, color 130ms ease-out, border-color 130ms ease-out;
}}
notebook.td-terminal-notebook tab:hover {{
  background-color: {tab_hov_bg};
  color: {t.fg};
}}
notebook.td-terminal-notebook tab:checked {{
  background-color: {tab_sel_bg};
  background-image: none;
  color: {t.fg};
  border-bottom: 2px solid {t.accent};
  font-weight: 600;
}}
notebook.td-terminal-notebook > stack {{
  background-color: {t.bg_sunken};
  border-radius: 0;
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
  color: {t.fg};
  opacity: 1;
}}
.td-bottom-bar {{
  background-color: transparent;
  background-image: none;
  border: none;
  border-top: 1px solid {hairline_soft};
  border-radius: 0;
  padding: 6px 0 0 0;
}}
.td-bottom-bar button, headerbar button, button {{ font-size: {size["button"]}; min-height: 32px; }}
.td-more-actions {{ background-color: transparent; border: 1px solid {hairline_soft}; border-radius: 8px; }}
.td-toast {{
  background-color: {mn_bg};
  background-image: none;
  border: 1px solid {hairline};
  border-radius: 10px;
  padding: 10px 16px;
  color: {t.fg};
  font-size: {size["base"]};
  box-shadow: 0 6px 24px {shadow};
}}
.td-toast.td-toast-success {{ border-color: {ts_ok_b}; }}
.td-toast.td-toast-warn    {{ border-color: {ts_wn_b}; }}
.td-toast.td-toast-danger  {{ border-color: {ts_dn_b}; }}
scrolledwindow {{
  background-color: {t.bg_sunken};
  border: 1px solid {hairline_soft};
  border-radius: 8px;
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
  border-bottom: 1px solid {hairline};
  font-weight: 600;
  font-size: {size["button"]};
  padding: 8px 10px;
}}
menu, popover {{
  background-color: {mn_bg};
  background-image: none;
  color: {t.fg};
  border: 1px solid {hairline};
  border-radius: 10px;
  padding: 5px;
  box-shadow: 0 10px 32px {shadow};
}}
menuitem {{ border-radius: 6px; padding: 7px 10px; margin: 1px 0; }}
menuitem:hover {{ background-color: {mi_hov_bg}; color: {t.fg}; }}
separator {{ background-color: {hairline_soft}; min-height: 1px; margin: 5px 4px; }}
tooltip {{
  background-color: {mn_bg};
  color: {t.fg};
  border: 1px solid {hairline};
  border-radius: 8px;
  padding: 6px 10px;
  box-shadow: 0 4px 16px {shadow};
}}
paned > separator {{ background-color: {hairline}; min-width: 1px; min-height: 1px; }}
scrollbar {{ background-color: transparent; }}
scrollbar slider {{
  background-color: {sb_slider};
  border-radius: 99px;
  min-width: 6px;
  min-height: 20px;
  transition: background-color 130ms ease-out;
}}
scrollbar slider:hover {{ background-color: {sb_sl_hov}; }}
scrollbar.td-terminal-scrollbar {{
  opacity: 1;
  background-image: none;
  background-color: {t.bg_sunken};
  border-left: 1px solid {hairline_soft};
  min-width: 14px;
}}
scrollbar.td-terminal-scrollbar trough {{
  background-image: none;
  background-color: transparent;
  border: none;
  min-width: 12px;
}}
scrollbar.td-terminal-scrollbar slider {{
  background-image: none;
  background-color: {sb_term_sl};
  border: none;
  border-radius: 99px;
  min-width: 8px;
  min-height: 24px;
  margin: 2px 3px;
}}
scrollbar.td-terminal-scrollbar slider:hover,
scrollbar.td-terminal-scrollbar slider:active {{
  background-color: {sb_term_hov};
}}
.td-density-compact * {{ font-size: {size["compact"]}; }}
.td-density-compact .td-session-card {{ padding: 5px 8px; border-radius: {compact_card_radius}px; }}
.td-density-compact notebook.td-terminal-notebook {{ margin: 0; }}
.td-density-compact notebook.td-terminal-notebook header {{ padding: 0 3px; }}
.td-density-compact notebook.td-terminal-notebook tab {{ padding: 5px 10px; margin: 0 1px; }}
.td-density-compact treeview {{ font-size: {size["compact"]}; }}
"""
