"""GTK3 chrome stylesheet for TermDeck's modern visual system."""

from __future__ import annotations


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
    return f"""
/* TermDeck chrome — theme={tokens.name} density={density} ui_font={ui_font or "system"} scale={scale:.2f} */
* {{
  {font_family_css}
  font-size: {size["base"]};
}}
window, dialog, messagedialog {{
  background-color: {tokens.bg};
  color: {tokens.fg};
}}
window.background {{ background-color: {tokens.bg}; }}
.td-main-root {{
  background-color: {tokens.bg};
}}
button {{
  background-image: none;
  background-color: alpha({tokens.fg}, 0.045);
  color: {tokens.fg};
  border: 1px solid alpha({tokens.border}, 0.88);
  border-radius: 11px;
  padding: 6px 12px;
  box-shadow: 0 2px 7px alpha(#000000, 0.10);
  transition: 120ms ease-out;
}}
button:hover {{
  background-color: alpha({tokens.accent}, 0.10);
  border-color: alpha({tokens.accent}, 0.38);
  box-shadow: 0 4px 12px alpha(#000000, 0.16);
}}
button:active {{
  background-color: alpha({tokens.accent}, 0.17);
  box-shadow: inset 0 1px 2px alpha(#000000, 0.22);
}}
button:focus {{
  border-color: alpha({tokens.accent}, 0.76);
  box-shadow: 0 0 0 2px alpha({tokens.accent}, 0.15);
}}
headerbar {{
  background-image: none;
  background-color: {tokens.bg_raised};
  border: none;
  border-bottom: 1px solid alpha({tokens.border}, 0.72);
  box-shadow: 0 8px 24px alpha(#000000, 0.24);
  padding: 8px 12px;
  min-height: 52px;
}}
headerbar label, headerbar button {{ color: {tokens.fg}; }}
headerbar button {{
  background-image: none;
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 12px;
  padding: 6px 10px;
  margin: 0 2px;
  min-height: 34px;
  box-shadow: none;
  transition: 120ms ease-out;
}}
headerbar button:hover {{
  background-color: alpha({tokens.accent}, 0.11);
  border-color: alpha({tokens.accent}, 0.30);
  box-shadow: none;
}}
headerbar button:active {{ background-color: alpha({tokens.accent}, 0.18); }}
button.td-primary-action, headerbar button.suggested-action, button.td-accent {{
  background-image: none;
  background-color: {tokens.accent};
  color: {tokens.accent_fg};
  border: 1px solid alpha({tokens.accent}, 0.85);
  border-radius: 10px;
  font-weight: 700;
  box-shadow: 0 4px 12px alpha({tokens.accent}, 0.20);
}}
button.td-primary-action:hover, headerbar button.suggested-action:hover, button.td-accent:hover {{
  background-color: shade({tokens.accent}, 1.08);
  border-color: shade({tokens.accent}, 1.12);
}}
button.td-ghost-action {{
  background-image: none;
  background-color: alpha({tokens.fg}, 0.035);
  color: {tokens.fg};
  border: 1px solid alpha({tokens.border}, 0.90);
  border-radius: 10px;
}}
button.td-ghost-action:hover {{
  background-color: alpha({tokens.accent}, 0.10);
  border-color: alpha({tokens.accent}, 0.34);
}}
button.td-danger-action, button.destructive-action {{
  background-image: none;
  background-color: alpha({tokens.danger}, 0.12);
  color: {tokens.danger};
  border: 1px solid alpha({tokens.danger}, 0.35);
  border-radius: 10px;
  font-weight: 700;
}}
button.td-danger-action:hover, button.destructive-action:hover {{
  background-color: alpha({tokens.danger}, 0.20);
  border-color: alpha({tokens.danger}, 0.60);
}}
button.td-header-overflow {{
  background-color: alpha({tokens.fg}, 0.050);
  border-color: alpha({tokens.border}, 0.78);
}}
button.td-header-icon {{
  min-width: 36px;
  padding-left: 8px;
  padding-right: 8px;
}}
headerbar button.suggested-action.td-header-icon {{
  min-width: 38px;
  min-height: 38px;
  padding: 0;
  border-radius: 999px;
  box-shadow: 0 5px 16px alpha({tokens.accent}, 0.24);
}}
headerbar button.td-header-group-end {{ margin-left: 8px; }}
.td-title {{
  font-weight: 700;
  font-size: {size["title"]};
  color: {tokens.fg};
  letter-spacing: 0.01em;
}}
.td-section-description {{
  color: {tokens.fg_muted};
  font-size: {size["muted"]};
  padding: 2px 2px 8px 2px;
}}
.td-subtitle, .dim-label, .td-muted {{
  color: {tokens.fg_muted};
  font-size: {size["muted"]};
}}
.td-eyebrow {{
  color: {tokens.accent};
  font-size: {size["chip"]};
  font-weight: 700;
  letter-spacing: 0.10em;
}}
.td-side-pane {{
  background-color: {tokens.bg_raised};
  border: 1px solid alpha({tokens.border}, 0.82);
  border-radius: 18px;
  box-shadow: 0 16px 40px alpha(#000000, 0.24);
  padding: {pad + 2}px;
  margin: {margin + 2}px;
}}
.td-count-pill {{
  background-color: alpha({tokens.accent}, 0.13);
  color: {tokens.accent};
  border: 1px solid alpha({tokens.accent}, 0.24);
  border-radius: 999px;
  padding: 3px 9px;
  font-size: {size["chip"]};
  font-weight: 700;
}}
.td-session-card {{
  background-color: alpha({tokens.fg}, 0.030);
  border: 1px solid alpha({tokens.border}, 0.78);
  border-radius: {radius}px;
  padding: {pad}px;
  margin: {margin // 2}px 0;
  box-shadow: 0 4px 14px alpha(#000000, 0.12);
  transition: 120ms ease-out;
}}
.td-session-card:hover {{
  background-color: alpha({tokens.accent}, 0.070);
  border-color: alpha({tokens.accent}, 0.46);
  box-shadow: 0 8px 22px alpha(#000000, 0.18);
}}
.td-session-card.td-session-selected {{
  background-color: alpha({tokens.accent}, 0.11);
  border-color: alpha({tokens.accent}, 0.78);
  box-shadow: inset 3px 0 0 {tokens.accent}, 0 6px 20px alpha(#000000, 0.16);
}}
.td-chip, .td-count-pill {{ border-radius: 999px; }}
.td-chip {{
  background-color: alpha({tokens.accent}, 0.14);
  color: {tokens.accent};
  border: 1px solid alpha({tokens.accent}, 0.20);
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
.td-badge-ok {{ background-color: alpha({tokens.success}, 0.14); color: {tokens.success}; border: 1px solid alpha({tokens.success}, 0.22); }}
.td-badge-warn {{ background-color: alpha({tokens.warning}, 0.14); color: {tokens.warning}; border: 1px solid alpha({tokens.warning}, 0.22); }}
.td-badge-danger {{ background-color: alpha({tokens.danger}, 0.14); color: {tokens.danger}; border: 1px solid alpha({tokens.danger}, 0.22); }}
.td-badge-muted {{ background-color: alpha({tokens.fg_muted}, 0.12); color: {tokens.fg_muted}; border: 1px solid alpha({tokens.fg_muted}, 0.18); }}
.td-badge-info {{ background-color: alpha({tokens.accent}, 0.14); color: {tokens.accent}; border: 1px solid alpha({tokens.accent}, 0.22); }}
.td-resource {{ font-family: {tokens.mono}; font-size: {size["chip"]}; color: {tokens.fg_muted}; }}
.td-mono, .td-accel, .td-id {{ font-family: {tokens.mono}; font-size: {size["mono"]}; }}
.td-empty-state {{
  background-color: alpha({tokens.fg}, 0.025);
  border: 1px dashed alpha({tokens.border}, 0.95);
  border-radius: {radius}px;
  padding: 24px 18px;
  margin: 8px 0;
}}
.td-empty-icon {{ color: {tokens.accent}; font-size: 28px; font-weight: 700; }}
.td-table-window, .td-overlay-dialog {{ background-color: {tokens.bg}; }}
.td-overlay-dialog border, .td-overlay-dialog {{ border-radius: 16px; }}
entry, .td-search-entry entry, entry.td-search-entry {{
  background-color: {tokens.bg_sunken};
  border: 1px solid alpha({tokens.border}, 0.95);
  border-radius: 11px;
  padding: 9px 13px;
  color: {tokens.fg};
  min-height: 36px;
  font-size: {size["base"]};
  caret-color: {tokens.accent};
}}
entry:focus, .td-search-entry entry:focus, entry.td-search-entry:focus {{
  border-color: alpha({tokens.accent}, 0.82);
  box-shadow: 0 0 0 2px alpha({tokens.accent}, 0.14);
}}
checkbutton check {{
  background-color: {tokens.bg_sunken};
  border: 1px solid alpha({tokens.border}, 0.95);
  border-radius: 5px;
  min-width: 16px;
  min-height: 16px;
}}
checkbutton check:checked {{ background-color: {tokens.accent}; border-color: {tokens.accent}; color: {tokens.accent_fg}; }}
.td-list-row {{ padding: {pad}px; border-bottom: 1px solid alpha({tokens.border}, 0.55); }}
.td-list-row:hover {{ background-color: alpha({tokens.accent}, 0.07); }}
.td-category {{ font-size: {size["chip"]}; font-weight: 700; letter-spacing: 0.08em; color: {tokens.accent}; }}
notebook {{ background-color: {tokens.bg_sunken}; border: none; }}
notebook.td-terminal-notebook {{
  background-color: {tokens.bg_sunken};
  border: 1px solid alpha({tokens.border}, 0.78);
  border-radius: 16px;
  margin: 10px 12px 12px 12px;
  box-shadow: 0 18px 48px alpha(#000000, 0.28);
}}
notebook.td-terminal-notebook header {{
  background-color: {tokens.bg_raised};
  border: none;
  border-bottom: 1px solid alpha({tokens.border}, 0.68);
  border-radius: 15px 15px 0 0;
  padding: 7px 8px;
}}
notebook.td-terminal-notebook header tabs {{ background-color: transparent; border-color: transparent; }}
notebook.td-terminal-notebook tab {{
  background-color: transparent;
  border: 1px solid transparent;
  border-radius: 10px;
  padding: 6px 11px;
  margin: 1px 3px;
  color: {tokens.fg_muted};
  font-size: {size["base"]};
  transition: 120ms ease-out;
}}
notebook.td-terminal-notebook tab:hover {{
  background-color: alpha({tokens.fg}, 0.055);
  border-color: alpha({tokens.border}, 0.55);
  color: {tokens.fg};
}}
notebook.td-terminal-notebook tab:checked {{
  background-color: alpha({tokens.accent}, 0.14);
  color: {tokens.fg};
  border-color: alpha({tokens.accent}, 0.36);
  box-shadow: inset 0 -2px 0 {tokens.accent}, 0 5px 14px alpha(#000000, 0.18);
}}
notebook.td-terminal-notebook > stack {{
  background-color: {tokens.bg_sunken};
  border-radius: 0 0 15px 15px;
}}
button.td-tab-close {{
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  border: none;
  border-radius: 999px;
  background-color: transparent;
  color: {tokens.fg_muted};
  font-size: {size["base"]};
  box-shadow: none;
  opacity: 0.52;
}}
notebook.td-terminal-notebook tab:checked button.td-tab-close {{ opacity: 0.82; }}
button.td-tab-close:hover {{
  background-color: alpha({tokens.danger}, 0.16);
  color: {tokens.danger};
  opacity: 1;
  box-shadow: none;
}}
.td-bottom-bar {{
  background-color: alpha({tokens.fg}, 0.025);
  border: 1px solid alpha({tokens.border}, 0.72);
  border-radius: 12px;
  padding: 6px;
}}
.td-bottom-bar button, headerbar button, button {{ font-size: {size["button"]}; min-height: 32px; }}
.td-more-actions {{ background-color: transparent; border: 1px solid alpha({tokens.border}, 0.82); border-radius: 10px; }}
.td-toast {{
  background-color: {tokens.bg_raised};
  border: 1px solid alpha({tokens.border}, 0.95);
  border-radius: 12px;
  padding: 11px 17px;
  color: {tokens.fg};
  font-size: {size["base"]};
  box-shadow: 0 10px 30px alpha(#000000, 0.32);
}}
.td-toast.td-toast-success {{ border-color: alpha({tokens.success}, 0.55); }}
.td-toast.td-toast-warn {{ border-color: alpha({tokens.warning}, 0.55); }}
.td-toast.td-toast-danger {{ border-color: alpha({tokens.danger}, 0.55); }}
scrolledwindow {{
  background-color: {tokens.bg_sunken};
  border: 1px solid alpha({tokens.border}, 0.72);
  border-radius: 12px;
}}
textview, textview text {{
  background-color: {tokens.bg_sunken};
  color: {tokens.fg};
}}
treeview {{ background-color: {tokens.bg_sunken}; color: {tokens.fg}; border: none; font-size: {size["base"]}; }}
treeview:selected {{ background-color: alpha({tokens.accent}, 0.24); color: {tokens.fg}; }}
treeview header button {{
  background-image: none;
  background-color: {tokens.bg_raised};
  color: {tokens.fg_muted};
  border: none;
  border-bottom: 1px solid {tokens.border};
  font-weight: 700;
  font-size: {size["button"]};
  padding: 9px 11px;
}}
menu, popover {{
  background-color: {tokens.bg_raised};
  color: {tokens.fg};
  border: 1px solid alpha({tokens.border}, 0.95);
  border-radius: 12px;
  padding: 6px;
  box-shadow: 0 12px 34px alpha(#000000, 0.30);
}}
menuitem {{ border-radius: 9px; padding: 8px 11px; margin: 1px 0; }}
menuitem:hover {{ background-color: alpha({tokens.accent}, 0.13); color: {tokens.fg}; }}
separator {{ background-color: alpha({tokens.border}, 0.72); min-height: 1px; margin: 5px 4px; }}
tooltip {{
  background-color: {tokens.bg_raised};
  color: {tokens.fg};
  border: 1px solid alpha({tokens.border}, 0.95);
  border-radius: 8px;
  padding: 7px 10px;
}}
paned > separator {{ background-color: transparent; min-width: 1px; min-height: 1px; }}
scrollbar {{ background-color: transparent; }}
scrollbar slider {{
  background-color: alpha({tokens.fg_muted}, 0.30);
  border-radius: 99px;
  min-width: 8px;
  min-height: 8px;
}}
scrollbar slider:hover {{ background-color: alpha({tokens.accent}, 0.52); }}
.td-density-compact * {{ font-size: {size["compact"]}; }}
.td-density-compact .td-session-card {{ padding: 6px 8px; border-radius: 10px; }}
.td-density-compact notebook.td-terminal-notebook {{ margin: 6px 8px 8px 8px; border-radius: 13px; }}
.td-density-compact notebook.td-terminal-notebook header {{ border-radius: 12px 12px 0 0; padding: 5px 6px; }}
.td-density-compact notebook.td-terminal-notebook tab {{ padding: 5px 8px; margin: 1px 2px; }}
.td-density-compact treeview {{ font-size: {size["compact"]}; }}
"""
