from __future__ import annotations

import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi  # type: ignore

# Pin GTK3 stack versions before any repository import. On systems that ship both
# Gdk 3.0 and 4.0, an unversioned Gdk import defaults to 4.0 and then breaks Gtk 3.0.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # type: ignore  # noqa: E402

from .command_palette import PaletteItem, build_palette_items, search_palette
from .control_plan import ControlPlan, build_control_plan, render_control_plan_text
from .health import HealthPolicy, classify_session, health_badge, should_pause_for_attention
from .master_tasks import filter_rows, row_from_record
from .models import SessionRecord, TerminalTheme
from .project_detect import detect_project
from .redaction import redact_text
from .resources import ResourceSampler
from .session_lifecycle import archive_records, hide_records, pin_records, recently_closed_records, rename_record, reopen_closed_records, retag_records, unpin_records
from .session_reconcile import reconcile_store
from .session_search import search_sessions
from .session_store import SessionStore, short_id, utc_now
from .settings import AppSettings, SettingsError, save_terminal_options
from .shortcuts import build_shortcut_map, index_from_key_name, shortcut_entries, tab_shortcut
from .task_snapshot import build_task_snapshot
from .task_templates import TaskTemplate, render_command, search_templates, template_by_id, template_statuses
from .window_snapshot import build_window_snapshots
from .terminator_config import load_terminator_theme
from .tmux_backend import TmuxBackend
from .ui_theme import (
    apply_terminal_palette,
    build_chrome_css,
    cursor_shape_names,
    format_resource_cue,
    health_badge_class,
    kind_for_session,
    kind_glyph,
    normalise_density,
    parse_font_description,
    scale_font_description,
    spacing_for_density,
    terminal_palette_names,
    toast_message_for_action,
)
from .workspace_recipe import RecipeWindow, WorkspaceRecipeError, load_recipe


def _style(widget, *classes):
    ctx = widget.get_style_context()
    for name in classes:
        ctx.add_class(name)
    return widget


def _icon_button(label, icon_name, tooltip, handler, *, suggested=False):
    button = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    try:
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        box.pack_start(image, False, False, 0)
    except Exception:
        pass
    box.pack_start(Gtk.Label(label=label), False, False, 0)
    button.add(box)
    button.set_tooltip_text(tooltip)
    if suggested:
        button.get_style_context().add_class("suggested-action")
        button.get_style_context().add_class("td-accent")
    button.connect("clicked", lambda *_: handler())
    return button


def _wire_auxiliary_close(window: Gtk.Window) -> None:
    # Make helper windows closable even on minimal/tiled window managers.
    def _close() -> None:
        try:
            window.hide()
        finally:
            window.destroy()

    def _on_delete(*_args) -> bool:
        _close()
        return True

    def _on_key_press(_widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            _close()
            return True
        return False

    window.connect("delete-event", _on_delete)
    window.connect("key-press-event", _on_key_press)


def _connect_debounced_refresh(widget: Gtk.Widget, refresh, *, delay_ms: int = 140) -> None:
    """Run expensive refresh callbacks after typing pauses instead of per keypress."""
    source_id = 0

    def _run() -> bool:
        nonlocal source_id
        source_id = 0
        refresh()
        return False

    def _queue(*_args) -> None:
        nonlocal source_id
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            source_id = 0
        try:
            source_id = GLib.timeout_add(delay_ms, _run)
        except Exception:
            source_id = 0
            refresh()

    def _cleanup(*_args) -> None:
        nonlocal source_id
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            source_id = 0

    widget.connect("search-changed", _queue)
    widget.connect("destroy", _cleanup)


def apply_app_chrome(settings):
    """Install the chrome CSS provider for the process. Returns the CSS text."""
    css = build_chrome_css(
        settings.chrome_theme,
        settings.ui_density,
        ui_font=settings.ui_font,
        ui_font_scale=settings.ui_font_scale,
    )
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    return css


class KeyMap:
    def __init__(self, theme: TerminalTheme, *, profile: str = "browser", import_terminator: bool = True):
        self.bindings = build_shortcut_map(
            theme.keybindings,
            profile=profile,
            import_terminator=import_terminator,
        )

    @staticmethod
    def _clean_state(state: Gdk.ModifierType) -> int:
        mask = (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.MOD1_MASK
            | Gdk.ModifierType.SUPER_MASK
            | Gdk.ModifierType.META_MASK
        )
        return int(state & mask)

    def matches(self, event: Gdk.EventKey, action: str) -> bool:
        accelerator = self.bindings.get(action)
        if not accelerator:
            return False
        key, mods = Gtk.accelerator_parse(accelerator)
        if not key:
            return False
        return event.keyval == key and self._clean_state(event.state) == int(mods)


def rgba(value: str | None, fallback: str = "#000000"):
    colour = Gdk.RGBA()
    if not value or not colour.parse(value):
        colour.parse(fallback)
    return colour


def apply_theme(terminal: Vte.Terminal, theme: TerminalTheme, *, font_override: str | None = None) -> None:
    if theme.use_system_font and not font_override:
        pass
    else:
        font = font_override or theme.font
        terminal.set_font(Pango.FontDescription(font))
    terminal.set_scrollback_lines(theme.scrollback_lines)
    palette = [rgba(colour) for colour in theme.palette]
    terminal.set_colors(rgba(theme.foreground_color, "#d3d7cf"), rgba(theme.background_color, "#000000"), palette)
    if theme.cursor_color:
        terminal.set_color_cursor(rgba(theme.cursor_color))
    shape = theme.cursor_shape.lower()
    if "underline" in shape:
        terminal.set_cursor_shape(Vte.CursorShape.UNDERLINE)
    elif "ibeam" in shape or "i-beam" in shape or "bar" in shape:
        terminal.set_cursor_shape(Vte.CursorShape.IBEAM)
    else:
        terminal.set_cursor_shape(Vte.CursorShape.BLOCK)
    if theme.cursor_blink is not None:
        try:
            mode = Vte.CursorBlinkMode.ON if theme.cursor_blink else Vte.CursorBlinkMode.OFF
            terminal.set_cursor_blink_mode(mode)
        except Exception:
            pass



class AttachTerminal(Gtk.Box):
    """VTE terminal attached to a hidden tmux session.

    The child process here is only the tmux client. The long-lived task lives in
    tmux and therefore survives when this widget closes.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        theme: TerminalTheme,
        *,
        key_press_handler=None,
        font_override: str | None = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.argv = argv
        self.cwd = cwd
        self.theme = theme
        self.process: subprocess.Popen[bytes] | None = None
        self.terminal = Vte.Terminal()
        apply_theme(self.terminal, theme, font_override=font_override)
        # VTE keeps focus for typing; connect here so Ctrl++/- zoom is not swallowed.
        if key_press_handler is not None:
            self.terminal.connect("key-press-event", key_press_handler)
        # Select-to-copy: run *after* VTE handles the release so the selection exists.
        self.terminal.connect_after("button-release-event", self._on_button_release)
        self.terminal.connect("selection-changed", self._on_selection_changed)
        self.terminal.connect("button-press-event", self._on_button_press)
        self._selection_pending = False
        self._selection_autocopy_source_id = 0
        # Last select-to-copy payload — used when pasting back into this terminal
        # (VTE paste_* can miss text we put on the clipboard ourselves).
        self._last_copied_text = ""
        self.pack_start(self.terminal, True, True, 0)
        self._spawn()

    def _spawn(self) -> None:
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        try:
            pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)
            self.terminal.set_pty(pty_obj)
            self.process = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
            )
            try:
                self.terminal.watch_child(self.process.pid)
            except Exception:
                pass
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

    def detach(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGHUP)
        except ProcessLookupError:
            return
        except OSError:
            try:
                self.process.terminate()
            except Exception:
                pass

    def copy_clipboard(self) -> None:
        self._copy_selection_to_clipboards()

    def paste_clipboard(self) -> None:
        """Paste CLIPBOARD (then PRIMARY / last copy) into the shell as input."""
        self._paste_from_selection(Gdk.SELECTION_CLIPBOARD, prefer_last=True)

    def paste_primary(self) -> None:
        """Paste PRIMARY selection into the shell as input (middle-click)."""
        self._paste_from_selection(Gdk.SELECTION_PRIMARY, prefer_last=True)

    def _feed_text(self, text: str) -> bool:
        """Inject text into the terminal PTY as if the user typed it."""
        if not text:
            return False
        # Avoid pasting the active highlight into itself weirdly — clear selection.
        try:
            if self.terminal.get_has_selection():
                self.terminal.unselect_all()
        except Exception:
            pass
        data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        try:
            if hasattr(self.terminal, "feed_child_binary"):
                self.terminal.feed_child_binary(data)
                return True
        except Exception:
            pass
        try:
            self.terminal.feed_child(data)
            return True
        except Exception:
            return False

    def _system_clipboard(self, selection):
        # Use the real GTK display clipboard so selected terminal text can be
        # pasted into browsers, editors, chat apps, and other external windows.
        try:
            display = self.terminal.get_display()
            if display is not None:
                return Gtk.Clipboard.get_for_display(display, selection)
        except Exception:
            pass
        try:
            return Gtk.Clipboard.get(selection)
        except Exception:
            return None

    def _clipboard_text(self, selection) -> str:
        clipboard = self._system_clipboard(selection)
        if clipboard is None:
            return ""
        try:
            text = clipboard.wait_for_text()
        except Exception:
            text = None
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", errors="replace")
        return text if isinstance(text, str) else ""

    def _paste_from_selection(self, selection, *, prefer_last: bool = False) -> bool:
        """Paste into this terminal. Prefer real clipboard, then last select-to-copy."""
        text = self._clipboard_text(selection)
        if not text and prefer_last:
            text = self._last_copied_text or ""
        if not text and selection != Gdk.SELECTION_CLIPBOARD:
            text = self._clipboard_text(Gdk.SELECTION_CLIPBOARD)
        if not text and prefer_last:
            text = self._last_copied_text or ""
        if text:
            return self._feed_text(text)
        # Last resort: VTE's own paste (async; may no-op after our set_text path).
        try:
            if selection == Gdk.SELECTION_PRIMARY:
                self.terminal.paste_primary()
            else:
                self.terminal.paste_clipboard()
            return True
        except Exception:
            return False

    def _selected_text(self) -> str:
        """Return the current selection as plain text, or empty string."""
        term = self.terminal
        try:
            if hasattr(term, "get_has_selection") and not term.get_has_selection():
                return ""
        except Exception:
            pass
        # Prefer reading text ourselves — more reliable than copy_clipboard alone
        # under some VTE/Wayland combinations.
        for reader in (
            lambda: term.get_text_selected(Vte.Format.TEXT),
            lambda: term.get_text_selected_full(Vte.Format.TEXT)[0]
            if hasattr(term, "get_text_selected_full")
            else None,
        ):
            try:
                text = reader()
            except Exception:
                text = None
            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8", errors="replace")
            if isinstance(text, str) and text:
                return text
        return ""

    def _set_clipboard_text(self, text: str, selection) -> None:
        if not text:
            return
        clipboard = self._system_clipboard(selection)
        if clipboard is None:
            return
        try:
            clipboard.set_text(text, -1)
            # Best-effort persist for managers that need store().
            if hasattr(clipboard, "store"):
                clipboard.store()
        except Exception:
            pass

    def _copy_selection_to_clipboards(self) -> bool:
        """Copy current VTE selection to CLIPBOARD and PRIMARY when present."""
        text = self._selected_text()
        if not text:
            # Fallback to VTE built-ins if get_text_selected is empty but a
            # selection is claimed.
            try:
                if self.terminal.get_has_selection():
                    if hasattr(self.terminal, "copy_clipboard_format"):
                        self.terminal.copy_clipboard_format(Vte.Format.TEXT)
                    else:
                        self.terminal.copy_clipboard()
                    if hasattr(self.terminal, "copy_primary"):
                        self.terminal.copy_primary()
                    return True
            except Exception:
                return False
            return False

        self._last_copied_text = text
        self._set_clipboard_text(text, Gdk.SELECTION_CLIPBOARD)
        self._set_clipboard_text(text, Gdk.SELECTION_PRIMARY)
        # Also ask VTE to own the selections (helps some paste targets).
        try:
            if hasattr(self.terminal, "copy_clipboard_format"):
                self.terminal.copy_clipboard_format(Vte.Format.TEXT)
            else:
                self.terminal.copy_clipboard()
        except Exception:
            pass
        try:
            if hasattr(self.terminal, "copy_primary"):
                self.terminal.copy_primary()
        except Exception:
            pass
        return True

    def _on_selection_changed(self, _terminal) -> None:
        # Copy every completed terminal selection to the system clipboard.  We
        # debounce selection-changed so double-click, triple-click, keyboard
        # selection, and drag selection all converge after VTE has finished
        # updating the selection buffer.
        try:
            self._selection_pending = bool(self.terminal.get_has_selection())
        except Exception:
            self._selection_pending = False
        if self._selection_pending:
            self._queue_selection_copy(delay_ms=120)

    def _queue_selection_copy(self, *, delay_ms: int = 120) -> None:
        """Schedule select-to-copy after VTE settles the selected text."""
        source_id = getattr(self, "_selection_autocopy_source_id", 0)
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        try:
            self._selection_autocopy_source_id = GLib.timeout_add(
                delay_ms, self._copy_selection_to_clipboards_idle
            )
        except Exception:
            self._selection_autocopy_source_id = 0

    def _show_terminal_context_menu(self, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        entries = [
            ("Copy selection", self.copy_clipboard),
            ("Paste", self.paste_clipboard),
            ("Paste primary", self.paste_primary),
            ("Select all", lambda: self.terminal.select_all()),
            ("Clear selection", lambda: self.terminal.unselect_all()),
        ]
        for label, handler in entries:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, h=handler: h())
            menu.append(item)
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, int(getattr(event, "button", 3) or 3), int(getattr(event, "time", 0) or 0))

    def _on_button_press(self, _terminal, event: Gdk.EventButton) -> bool:
        button = int(getattr(event, "button", 0) or 0)
        # Middle-click paste from PRIMARY / last selection (classic terminal).
        if button == 2:
            self.paste_primary()
            return True
        if button == 3:
            self._show_terminal_context_menu(event)
            return True
        return False

    def _on_button_release(self, _terminal, event: Gdk.EventButton) -> bool:
        # Left-button release after a drag/select → immediate auto-copy.  This
        # keeps the browser-like feel while the selection-changed debounce covers
        # keyboard/double-click paths that may not deliver a useful release edge.
        button = int(getattr(event, "button", 0) or 0)
        if button == 1:
            source_id = getattr(self, "_selection_autocopy_source_id", 0)
            if source_id:
                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass
                self._selection_autocopy_source_id = 0
            # Idle so VTE finishes updating the selection buffer first.
            GLib.idle_add(self._copy_selection_to_clipboards_idle)
        return False

    def _copy_selection_to_clipboards_idle(self) -> bool:
        self._selection_autocopy_source_id = 0
        self._copy_selection_to_clipboards()
        self._selection_pending = False
        return False



class WindowOverview(Gtk.Window):
    """Browser-style overview of TermDeck windows and their aggregate load."""

    COLUMNS = [
        "Window",
        "Tabs",
        "Visible",
        "State",
        "Hot",
        "Critical",
        "CPU",
        "RAM",
        "Procs",
        "Runtime",
        "Top health",
    ]

    def __init__(self, app_context: "TermDeckApplication"):
        super().__init__(title="TermDeck Window Overview")
        self.app_context = app_context
        self._refresh_alive = True
        self.connect("destroy", lambda *_: setattr(self, "_refresh_alive", False))
        _style(self, "td-table-window")
        self.set_default_size(920, 420)
        _wire_auxiliary_close(self)
        self.store = Gtk.ListStore(*([str] * len(self.COLUMNS)))
        self.tree = Gtk.TreeView(model=self.store)
        for idx, column in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(column, renderer, text=idx)
            col.set_resizable(True)
            col.set_sort_column_id(idx)
            if column in {"Window", "State", "Top health"}:
                col.set_expand(True)
            self.tree.append_column(col)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label="All browser-like windows, aggregated from their terminal tabs")
        label.set_xalign(0)
        top.pack_start(label, True, True, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        top.pack_start(refresh, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        top.pack_start(close, False, False, 0)
        outer.pack_start(top, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)
        self.add(outer)
        GLib.timeout_add_seconds(int(max(1, round(getattr(app_context.settings, "ui_refresh_interval_seconds", 5.0)))), self._refresh_timer)
        self.refresh()

    def _refresh_timer(self) -> bool:
        if not getattr(self, "_refresh_alive", False):
            return False
        if self.get_visible():
            self.refresh()
        return True

    def refresh(self, *_args) -> None:
        self.store.clear()
        records = self.app_context.store.list()
        items = build_task_snapshot(
            records,
            backend=self.app_context.backend,
            sampler=self.app_context.sampler,
            policy=self.app_context.budget,
        )
        for row in build_window_snapshots(items):
            self.store.append([
                row.title,
                str(row.tab_count),
                str(row.visible_count),
                row.state_summary,
                str(row.attention_count),
                str(row.critical_count),
                f"{row.cpu_percent:.1f}%",
                row.memory,
                str(row.process_count),
                row.runtime,
                row.top_health,
            ])


class ShortcutWindow(Gtk.Window):
    """Discoverable browser and terminal shortcut help."""

    COLUMNS = ["Action", "Shortcut", "Scope", "Source", "Description"]
    # Preferred minimum widths so the table uses horizontal space when maximized.
    COLUMN_MIN_WIDTHS = {
        "Action": 160,
        "Shortcut": 140,
        "Scope": 100,
        "Source": 120,
        "Description": 280,
    }

    def __init__(self, app_context: "TermDeckApplication", parent: Gtk.Window | None = None):
        super().__init__(title="TermDeck Keyboard Shortcuts")
        self.app_context = app_context
        _style(self, "td-table-window")
        self.set_resizable(True)
        # NORMAL (not DIALOG) so maximize/fullscreen and full client area work on tiling WMs.
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_skip_taskbar_hint(False)
        self.set_accept_focus(True)
        if parent is not None:
            self.set_transient_for(parent)
            self.set_destroy_with_parent(False)
            try:
                width, height = parent.get_size()
                # Nearly fill the parent so content is not stuck in a small dialog frame.
                self.set_default_size(max(width - 40, 1100), max(height - 40, 700))
                if parent.is_maximized():
                    self.maximize()
            except Exception:
                self.set_default_size(1100, 700)
        else:
            self.set_default_size(1100, 700)

        display = self.get_display()
        try:
            monitor = display.get_monitor_at_window(parent.get_window()) if parent and parent.get_window() else display.get_primary_monitor()
            if monitor is not None:
                geometry = monitor.get_geometry()
                # Cap initial size to usable monitor area when not maximized.
                if not self.is_maximized():
                    self.set_default_size(
                        min(max(self.get_default_size()[0], 1100), max(geometry.width - 80, 800)),
                        min(max(self.get_default_size()[1], 700), max(geometry.height - 80, 500)),
                    )
        except Exception:
            pass

        self.connect("delete-event", self._on_delete)
        self.connect("key-press-event", self._on_key_press)
        self.connect("window-state-event", self._on_window_state)
        self.connect("size-allocate", self._on_size_allocate)

        # TreeModelSort so clicking column headers sorts the view without
        # rewriting the ListStore order on every refresh.
        self.store = Gtk.ListStore(*([str] * len(self.COLUMNS)))
        self.sorted = Gtk.TreeModelSort(model=self.store)
        self.tree = Gtk.TreeView(model=self.sorted)
        self.tree.set_headers_clickable(True)
        self.tree.set_enable_search(True)
        self.tree.set_search_column(0)
        self.tree.set_hexpand(True)
        self.tree.set_vexpand(True)
        self.tree.set_property("has-tooltip", True)
        self._columns: list[Gtk.TreeViewColumn] = []
        for idx, column in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            if column in {"Shortcut", "Scope", "Source"}:
                renderer.set_property("family", "Monospace")
            col = Gtk.TreeViewColumn(column, renderer, text=idx)
            col.set_resizable(True)
            col.set_reorderable(True)
            col.set_clickable(True)
            col.set_sort_column_id(idx)
            col.set_sort_indicator(True)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_min_width(self.COLUMN_MIN_WIDTHS.get(column, 80))
            # Grow flexible columns so the table consumes the full window width.
            col.set_expand(column in {"Action", "Description", "Source"})
            col.set_fixed_width(self.COLUMN_MIN_WIDTHS.get(column, 100))
            self.tree.append_column(col)
            self._columns.append(col)
        # Default sort: Action ascending (matches browser-style shortcut lists).
        self.sorted.set_sort_column_id(0, Gtk.SortType.ASCENDING)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.set_hexpand(True)
        label = Gtk.Label(
            label="Browser controls, generated tab pause keys, Terminator overrides, and terminal passthrough keys. Click a column header to sort. Maximize or F11 for full screen.",
        )
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_hexpand(True)
        top.pack_start(label, True, True, 0)
        maximize_button = Gtk.Button(label="Maximize")
        maximize_button.connect("clicked", lambda *_: self.toggle_maximize())
        top.pack_end(maximize_button, False, False, 0)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.close_window())
        top.pack_end(close_button, False, False, 0)
        outer.pack_start(top, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        # Propagate natural size so the tree is not stuck at a tiny allocation.
        scroller.set_propagate_natural_width(False)
        scroller.set_propagate_natural_height(False)
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bottom.set_hexpand(True)
        hint = Gtk.Label(label="Esc closes · F11 toggles full screen · click headers to sort")
        hint.set_xalign(0)
        hint.set_hexpand(True)
        bottom.pack_start(hint, True, True, 0)
        close_bottom = Gtk.Button(label="Close")
        close_bottom.set_can_default(True)
        close_bottom.connect("clicked", lambda *_: self.close_window())
        bottom.pack_end(close_bottom, False, False, 0)
        outer.pack_start(bottom, False, False, 0)

        self.add(outer)
        self.refresh()
        # Defer focus so map/size allocate can finish first.
        GLib.idle_add(self._after_show, close_bottom)

    def _after_show(self, focus_widget: Gtk.Widget) -> bool:
        try:
            self.queue_resize()
            self._redistribute_column_widths()
            focus_widget.grab_focus()
        except Exception:
            pass
        return False

    def toggle_maximize(self) -> None:
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def toggle_fullscreen(self) -> None:
        state = self.get_window().get_state() if self.get_window() else 0
        if state & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_window_state(self, _widget, event: Gdk.EventWindowState) -> bool:
        # When maximized/fullscreen, force a reflow so columns fill the new area.
        if event.changed_mask & (
            Gdk.WindowState.MAXIMIZED | Gdk.WindowState.FULLSCREEN | Gdk.WindowState.TILED
        ):
            GLib.idle_add(self._redistribute_column_widths)
        return False

    def _on_size_allocate(self, _widget, allocation) -> None:
        # Avoid thrashing: only rebalance when width changes meaningfully.
        width = int(getattr(allocation, "width", 0) or 0)
        last = getattr(self, "_last_alloc_width", 0)
        if abs(width - last) >= 24:
            self._last_alloc_width = width
            GLib.idle_add(self._redistribute_column_widths)

    def _redistribute_column_widths(self) -> bool:
        """Expand flexible columns so the tree uses the full window width."""
        try:
            allocation = self.tree.get_allocation()
            total = int(getattr(allocation, "width", 0) or 0)
            if total < 200:
                return False
            # Leave a little room for scrollbars / padding.
            usable = max(total - 24, 200)
            fixed_cols = [c for c in self._columns if not c.get_expand()]
            flex_cols = [c for c in self._columns if c.get_expand()]
            fixed_width = sum(max(c.get_min_width(), 80) for c in fixed_cols)
            remaining = max(usable - fixed_width, len(flex_cols) * 80)
            if not flex_cols:
                return False
            share = remaining // len(flex_cols)
            extra = remaining % len(flex_cols)
            for index, col in enumerate(flex_cols):
                width = max(col.get_min_width(), share + (1 if index < extra else 0))
                col.set_fixed_width(width)
            for col in fixed_cols:
                col.set_fixed_width(max(col.get_min_width(), col.get_fixed_width() or col.get_min_width()))
            self.tree.queue_draw()
        except Exception:
            pass
        return False

    def close_window(self) -> None:
        try:
            if self.get_window() and self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                self.unfullscreen()
        except Exception:
            pass
        self.hide()
        self.destroy()

    def _on_delete(self, *_args) -> bool:
        self.close_window()
        return True

    def _on_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            # Esc exits fullscreen first, then closes.
            try:
                if self.get_window() and self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
                    self.unfullscreen()
                    return True
            except Exception:
                pass
            self.close_window()
            return True
        if event.keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        return False

    def refresh(self) -> None:
        self.store.clear()
        for entry in shortcut_entries(
            self.app_context.theme.keybindings,
            profile=self.app_context.settings.keyboard_profile,
            import_terminator=self.app_context.settings.import_terminator_keybindings,
        ):
            self.store.append([entry.action, entry.accelerator, entry.scope, entry.source, entry.description])
        GLib.idle_add(self._redistribute_column_widths)


class SessionSearchWindow(Gtk.Window):
    """Browser-style search across remembered terminal tabs.

    Metadata search is cheap and always available. Captured-output search is
    opt-in because it asks the hidden session backend for recent terminal
    output; snippets still pass through the shared redaction path by default.
    """

    COLUMNS = ["ID", "Window", "Name", "Tag", "Status", "Fields", "Output", "Command"]

    def __init__(self, app_context: "TermDeckApplication"):
        super().__init__(title="TermDeck Search Tabs")
        self.app_context = app_context
        _style(self, "td-table-window")
        self.set_default_size(1040, 620)
        _wire_auxiliary_close(self)
        self.matches_by_id = {}
        self.store = Gtk.ListStore(*([str] * len(self.COLUMNS)))
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().connect("changed", lambda *_: self.update_preview())
        self.tree.connect("row-activated", lambda *_: self.open_selected())
        for idx, column in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            if column in {"ID", "Status", "Fields", "Output"}:
                renderer.set_property("family", "Monospace")
            col = Gtk.TreeViewColumn(column, renderer, text=idx)
            col.set_resizable(True)
            col.set_sort_column_id(idx)
            if column in {"Name", "Command"}:
                col.set_expand(True)
            self.tree.append_column(col)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        label = Gtk.Label(
            label="Search remembered terminal tabs by name, tag, command, directory, window, status, and optional recent output.",
            xalign=0,
        )
        label.set_line_wrap(True)
        outer.pack_start(label, False, False, 0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.query = Gtk.SearchEntry(placeholder_text="search tabs: codex, bugbounty error, api server, pytest")
        _connect_debounced_refresh(self.query, self.refresh)
        self.query.connect("activate", lambda *_: self.open_selected())
        top.pack_start(self.query, True, True, 0)
        self.include_output = Gtk.CheckButton(label="include captured output")
        self.include_output.connect("toggled", lambda *_: self.refresh())
        top.pack_start(self.include_output, False, False, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        top.pack_start(refresh, False, False, 0)
        open_button = Gtk.Button(label="Open")
        open_button.connect("clicked", lambda *_: self.open_selected())
        top.pack_start(open_button, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        top.pack_start(close, False, False, 0)
        outer.pack_start(top, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)

        self.preview = Gtk.TextView()
        self.preview.set_editable(False)
        self.preview.set_cursor_visible(False)
        self.preview.set_monospace(True)
        self.preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        preview_scroller = Gtk.ScrolledWindow()
        preview_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        preview_scroller.set_size_request(-1, 160)
        preview_scroller.add(self.preview)
        outer.pack_start(preview_scroller, False, True, 0)
        self.add(outer)
        self.set_preview_text("Type to search remembered tabs. Enable captured output only when you need recent terminal text.")
        self.query.grab_focus()
        self.show_all()

    def set_preview_text(self, text: str) -> None:
        self.preview.get_buffer().set_text(text)

    def capture_for_search(self, record: SessionRecord, lines: int) -> str:
        try:
            return self.app_context.backend.capture(record, lines=lines)
        except Exception as exc:
            return f"capture unavailable: {exc}"

    def selected_match(self):
        model, iter_ = self.tree.get_selection().get_selected()
        if iter_ is None:
            return None
        session_id = model.get_value(iter_, 0)
        return self.matches_by_id.get(session_id)

    def refresh(self, *_args) -> None:
        query = self.query.get_text().strip()
        include_output = bool(self.include_output.get_active())
        result = search_sessions(
            self.app_context.store.list(include_archived=True),
            query,
            include_output=include_output,
            capture_resolver=self.capture_for_search if include_output else None,
            lines=120,
            redact=True,
            max_results=200,
            max_snippets=4,
        )
        self.matches_by_id = {match.session_id: match for match in result.matches}
        self.store.clear()
        for match in result.matches:
            self.store.append([
                match.session_id,
                match.window_title,
                match.name,
                match.tag,
                match.status,
                ",".join(match.matched_fields) or "-",
                str(match.output_match_count) if match.output_match_count else "-",
                match.command,
            ])
        if not query:
            self.set_preview_text("Type to search remembered tabs. Enable captured output only when you need recent terminal text.")
        elif not result.matches:
            self.set_preview_text(f"No remembered terminal tabs matched: {query!r}")
        else:
            self.update_preview()

    def update_preview(self) -> None:
        match = self.selected_match()
        if match is None:
            if self.store.iter_n_children(None) > 0:
                self.set_preview_text("Select a search result to see details and redacted captured-output snippets.")
            return
        lines = [
            f"{match.name}  [{match.tag}]",
            f"ID: {match.session_id}",
            f"Window: {match.window_title}",
            f"Status: {match.status}",
            f"Directory: {match.cwd}",
            f"Command: {match.command}",
            f"Matched fields: {', '.join(match.matched_fields) or '-'}",
        ]
        if match.output_snippets:
            lines.append("")
            lines.append("Captured-output snippets, redacted by default:")
            for snippet in match.output_snippets:
                lines.append(f"line {snippet.line_number}: {snippet.text}")
            if match.redacted:
                lines.append("")
                lines.append("Redacted secret-like values: " + ", ".join(match.redaction_labels))
        elif self.include_output.get_active():
            lines.append("")
            lines.append("Captured output was searched, but no output snippets matched this query.")
        else:
            lines.append("")
            lines.append("Enable captured-output search to inspect recent terminal text safely.")
        self.set_preview_text("\n".join(lines))

    def open_selected(self) -> None:
        match = self.selected_match()
        if match is None:
            return
        record = self.app_context.store.get(match.session_id)
        if not record or not self.app_context.windows:
            return
        self.app_context.windows[-1].open_session(record)

class TerminalTab:
    def __init__(self, record: SessionRecord, widget: AttachTerminal):
        self.record = record
        self.widget = widget


class CommandDialog(Gtk.Dialog):
    """Browser-like New Task palette.

    The dialog starts from plain-language templates, shows missing binaries
    without hiding the template, and keeps every field editable.
    """

    def __init__(self, parent: Gtk.Window):
        super().__init__(title="New terminal task", transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_modal(True)
        self.set_default_size(760, 520)
        _style(self, "td-overlay-dialog", "td-table-window")
        self.status_by_id = {status.template.id: status for status in template_statuses()}
        self.templates = [status.template for status in self.status_by_id.values()]

        box = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        box.add(outer)

        intro = Gtk.Label(
            label="Pick a task type, edit the command if needed, then create a browser-like terminal tab.",
            xalign=0,
        )
        intro.set_line_wrap(True)
        _style(intro, "td-muted")
        outer.pack_start(intro, False, False, 0)

        self.template_search = Gtk.SearchEntry(placeholder_text="search templates: shell, codex, ssh, npm, tests, logs")
        _style(self.template_search, "td-search-entry")
        _connect_debounced_refresh(self.template_search, self._refresh_templates)
        outer.pack_start(self.template_search, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(640, 190)
        self.template_box = Gtk.ListBox()
        self.template_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.template_box.connect("row-selected", self._on_template_selected)
        scroller.add(self.template_box)
        outer.pack_start(scroller, True, True, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        outer.pack_start(grid, False, False, 0)

        self.name = Gtk.Entry(placeholder_text="tab name, e.g. api-server")
        self.tag = Gtk.Entry(placeholder_text="workspace/tag, e.g. dev")
        self.command = Gtk.Entry(placeholder_text="command, e.g. npm run dev or codex")
        self.cwd = Gtk.Entry(text=os.getcwd())

        rows = [
            ("Name", self.name),
            ("Tag", self.tag),
            ("Command", self.command),
            ("Directory", self.cwd),
        ]
        for row, (label, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        self._refresh_templates()
        if self.templates:
            self._apply_template(self.templates[0])
        self.template_search.grab_focus()
        self.show_all()

    def _refresh_templates(self) -> None:
        for child in self.template_box.get_children():
            self.template_box.remove(child)
        for template in search_templates(self.template_search.get_text(), self.templates):
            status = self.status_by_id.get(template.id)
            availability = "available" if status is None or status.available else "missing: " + ", ".join(status.missing)
            kind = kind_for_session(name=template.name, command=template.command, tag=template.tag)
            row = Gtk.ListBoxRow()
            row.template = template  # type: ignore[attr-defined]
            _style(row, "td-list-row")
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=6)
            cat = Gtk.Label(label=f"{kind_glyph(kind)}  {kind.upper()} · {template.tag}", xalign=0)
            _style(cat, "td-category")
            title = Gtk.Label(label=f"{template.label}    {availability}", xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            _style(title, "td-title")
            command = Gtk.Label(label=render_command(template.command, cwd=os.getcwd()), xalign=0)
            command.set_ellipsize(Pango.EllipsizeMode.END)
            _style(command, "td-mono", "td-muted")
            description = Gtk.Label(label=template.description, xalign=0)
            description.set_line_wrap(True)
            _style(description, "td-muted")
            row_box.pack_start(cat, False, False, 0)
            row_box.pack_start(title, False, False, 0)
            row_box.pack_start(command, False, False, 0)
            row_box.pack_start(description, False, False, 0)
            row.add(row_box)
            self.template_box.add(row)
        self.template_box.show_all()

    def _on_template_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        template = getattr(row, "template", None)
        if isinstance(template, TaskTemplate):
            self._apply_template(template)

    def _apply_template(self, template: TaskTemplate) -> None:
        self.name.set_text(template.name)
        self.tag.set_text(template.tag)
        self.command.set_text(render_command(template.command, cwd=self.cwd.get_text() or os.getcwd()))

    def values(self) -> tuple[str, str, str, str]:
        command = self.command.get_text().strip() or os.environ.get("SHELL", "/bin/sh")
        name = self.name.get_text().strip() or command.split()[0]
        tag = self.tag.get_text().strip() or "default"
        cwd = self.cwd.get_text().strip() or os.getcwd()
        return name, tag, command, cwd


LauncherDialog = CommandDialog


class TerminalOptionsDialog(Gtk.Dialog):
    """Appearance controls: UI font + terminal font/cursor/palette/scrollback."""

    def __init__(self, parent: Gtk.Window, settings: AppSettings, theme: TerminalTheme):
        super().__init__(title="Appearance options", transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_modal(True)
        self.set_default_size(520, 480)
        _style(self, "td-overlay-dialog", "td-table-window")
        self.settings = settings
        self.theme = theme

        box = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        box.add(outer)

        intro = Gtk.Label(
            label="Interface and terminal fonts. Saved under appearance in ~/.config/termdeck/config.json.",
            xalign=0,
        )
        intro.set_line_wrap(True)
        _style(intro, "td-muted")
        outer.pack_start(intro, False, False, 0)

        ui_label = Gtk.Label(label="Interface (non-terminal)", xalign=0)
        _style(ui_label, "td-title")
        outer.pack_start(ui_label, False, False, 0)

        ui_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        outer.pack_start(ui_grid, False, False, 0)
        self.ui_font_entry = Gtk.Entry(text=settings.ui_font or "")
        self.ui_font_entry.set_placeholder_text("empty = system UI font · e.g. Inter 11 or Sans 12")
        self.ui_scale = Gtk.SpinButton()
        self.ui_scale.set_range(0.75, 1.75)
        self.ui_scale.set_increments(0.05, 0.1)
        self.ui_scale.set_digits(2)
        self.ui_scale.set_value(float(settings.ui_font_scale if settings.ui_font_scale is not None else 1.0))
        ui_grid.attach(Gtk.Label(label="UI font", xalign=0), 0, 0, 1, 1)
        ui_grid.attach(self.ui_font_entry, 1, 0, 1, 1)
        ui_grid.attach(Gtk.Label(label="UI scale", xalign=0), 0, 1, 1, 1)
        ui_grid.attach(self.ui_scale, 1, 1, 1, 1)

        term_label = Gtk.Label(label="Terminal (VTE)", xalign=0)
        _style(term_label, "td-title")
        outer.pack_start(term_label, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        outer.pack_start(grid, False, False, 0)

        current_font = settings.terminal_font or theme.font
        self.font_entry = Gtk.Entry(text=current_font)
        self.font_entry.set_placeholder_text("e.g. Monospace 13 or JetBrains Mono 12")
        self.system_font = Gtk.CheckButton(label="Use system monospace font")
        self.system_font.set_active(bool(settings.terminal_use_system_font))

        self.cursor_combo = Gtk.ComboBoxText()
        current_cursor = settings.terminal_cursor_shape or theme.cursor_shape or "block"
        for shape in cursor_shape_names():
            self.cursor_combo.append_text(shape)
            if shape == current_cursor:
                self.cursor_combo.set_active(list(cursor_shape_names()).index(shape))
        if self.cursor_combo.get_active() < 0:
            self.cursor_combo.set_active(0)

        self.cursor_color = Gtk.Entry(text=settings.terminal_cursor_color or (theme.cursor_color or ""))
        self.cursor_color.set_placeholder_text("optional, e.g. #cdd6f4")
        self.cursor_blink = Gtk.CheckButton(label="Blink cursor")
        cursor_blink = settings.terminal_cursor_blink if settings.terminal_cursor_blink is not None else True
        self.cursor_blink.set_active(bool(cursor_blink))

        self.scrollback = Gtk.SpinButton()
        self.scrollback.set_range(100, 500000)
        self.scrollback.set_increments(100, 1000)
        self.scrollback.set_value(float(settings.terminal_scrollback_lines or theme.scrollback_lines or 10000))

        self.palette_combo = Gtk.ComboBoxText()
        for name in terminal_palette_names():
            self.palette_combo.append_text(name)
            if name == settings.terminal_palette:
                self.palette_combo.set_active(list(terminal_palette_names()).index(name))
        if self.palette_combo.get_active() < 0:
            self.palette_combo.set_active(0)

        self.side_position_combo = Gtk.ComboBoxText()
        for position in ("right", "left"):
            self.side_position_combo.append_text(position)
            if position == settings.side_pane_position:
                self.side_position_combo.set_active(0 if position == "right" else 1)
        if self.side_position_combo.get_active() < 0:
            self.side_position_combo.set_active(0)

        rows = [
            ("Font", self.font_entry),
            ("", self.system_font),
            ("Cursor shape", self.cursor_combo),
            ("Cursor colour", self.cursor_color),
            ("Cursor blink", self.cursor_blink),
            ("Scrollback lines", self.scrollback),
            ("Colour palette", self.palette_combo),
            ("Sessions pane", self.side_position_combo),
        ]
        for index, (label, widget) in enumerate(rows):
            if label:
                grid.attach(Gtk.Label(label=label, xalign=0), 0, index, 1, 1)
                grid.attach(widget, 1, index, 1, 1)
            else:
                grid.attach(widget, 1, index, 1, 1)

        hint = Gtk.Label(
            label="UI font affects headers, side pane, tables, dialogs. Terminal font is separate (also Ctrl++/- zoom).",
            xalign=0,
        )
        hint.set_line_wrap(True)
        _style(hint, "td-muted")
        outer.pack_start(hint, False, False, 0)
        self.show_all()

    def values(self) -> dict[str, object]:
        colour = self.cursor_color.get_text().strip()
        ui_font = self.ui_font_entry.get_text().strip()
        return {
            "ui_font": ui_font,
            "ui_font_scale": float(self.ui_scale.get_value()),
            "font": self.font_entry.get_text().strip() or "Monospace 12",
            "use_system_font": bool(self.system_font.get_active()),
            "cursor_shape": self.cursor_combo.get_active_text() or "block",
            "cursor_color": colour or None,
            "cursor_blink": bool(self.cursor_blink.get_active()),
            "scrollback_lines": int(self.scrollback.get_value()),
            "terminal_palette": self.palette_combo.get_active_text() or "terminator",
            "side_pane_position": self.side_position_combo.get_active_text() or "right",
        }


class ActionPaletteDialog(Gtk.Dialog):
    """Universal command palette for actions, sessions, and templates."""

    def __init__(self, parent: Gtk.Window, items: list[PaletteItem]):
        super().__init__(title="Command palette", transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Run", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_modal(True)
        self.set_default_size(760, 520)
        _style(self, "td-overlay-dialog", "td-table-window")
        self.items = items
        self._selected: PaletteItem | None = None

        box = self.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        box.add(outer)

        intro = Gtk.Label(
            label="Search actions, sessions, and task templates. Pick a result and press Enter.",
            xalign=0,
        )
        intro.set_line_wrap(True)
        _style(intro, "td-muted")
        outer.pack_start(intro, False, False, 0)

        self.search = Gtk.SearchEntry(placeholder_text="try: master, codex, pause selected, cpu, ssh, shell")
        _style(self.search, "td-search-entry")
        _connect_debounced_refresh(self.search, self.refresh)
        self.search.connect("activate", lambda *_: self.response(Gtk.ResponseType.OK))
        outer.pack_start(self.search, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-selected", self._on_row_selected)
        self.list_box.connect("row-activated", lambda *_: self.response(Gtk.ResponseType.OK))
        scroller.add(self.list_box)
        outer.pack_start(scroller, True, True, 0)

        self.refresh()
        self.search.grab_focus()
        self.show_all()

    def refresh(self) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)
        rows = search_palette(self.search.get_text(), self.items)
        self._selected = rows[0] if rows else None
        first_row = None
        for item in rows:
            row = Gtk.ListBoxRow()
            row.item = item  # type: ignore[attr-defined]
            _style(row, "td-list-row")
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin=7)
            category = Gtk.Label(label=str(item.badge or item.kind or "action").upper(), xalign=0)
            _style(category, "td-category")
            title = Gtk.Label(label=item.label, xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            _style(title, "td-title")
            if item.destructive:
                title.get_style_context().add_class("error")
                _style(title, "td-badge-danger")
            description = Gtk.Label(label=item.description, xalign=0)
            description.set_line_wrap(True)
            _style(description, "td-muted")
            ident = Gtk.Label(label=item.id, xalign=0)
            _style(ident, "dim-label", "td-mono", "td-id")
            ident.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.pack_start(category, False, False, 0)
            row_box.pack_start(title, False, False, 0)
            row_box.pack_start(description, False, False, 0)
            row_box.pack_start(ident, False, False, 0)
            row.add(row_box)
            self.list_box.add(row)
            if first_row is None:
                first_row = row
        self.list_box.show_all()
        if first_row is not None:
            self.list_box.select_row(first_row)

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        item = getattr(row, "item", None)
        if isinstance(item, PaletteItem):
            self._selected = item

    def selected(self) -> PaletteItem | None:
        row = self.list_box.get_selected_row()
        if row is not None:
            item = getattr(row, "item", None)
            if isinstance(item, PaletteItem):
                return item
        return self._selected


class SidePane(Gtk.Box):
    def __init__(self, window: "TermDeckWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        self.window = window
        self.selected_ids: set[str] = set()
        self._refresh_alive = True
        self.connect("destroy", lambda *_: setattr(self, "_refresh_alive", False))
        self._last_refresh_signature = None
        # Rebuild dynamic CPU/RAM/health rows periodically even when session
        # metadata is unchanged; duplicate refreshes within the same bucket are
        # skipped to keep the UI responsive.
        self._refresh_bucket_seconds = max(1.0, float(getattr(window.app_context.settings, "ui_refresh_interval_seconds", 5.0)))
        _style(self, "td-side-pane")
        density = normalise_density(getattr(window.app_context.settings, "ui_density", "comfortable"))
        if density == "compact":
            _style(self, "td-density-compact")

        title = Gtk.Label(label="Sessions", xalign=0)
        _style(title, "title", "td-title")
        title_event = Gtk.EventBox()
        title_event.add(title)
        title_event.set_tooltip_text("Right-click to move the sessions pane left or right")
        title_event.connect("button-press-event", self._on_header_button_press)
        self.pack_start(title_event, False, False, 0)

        hint = Gtk.Label(label="Ctrl-click a tag to multi-select · right-click rows for actions", xalign=0)
        hint.set_line_wrap(True)
        _style(hint, "td-muted")
        self.pack_start(hint, False, False, 0)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="filter tags/sessions")
        _style(self.filter_entry, "td-search-entry")
        _connect_debounced_refresh(self.filter_entry, self.refresh)
        self.pack_start(self.filter_entry, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.session_box = Gtk.ListBox()
        self.session_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.add(self.session_box)
        self.pack_start(scroller, True, True, 0)

        self.prompt = Gtk.Entry(placeholder_text="send text/prompt to selected sessions")
        self.pack_start(self.prompt, False, False, 0)

        # Primary bulk actions stay visible; secondary live under a menu.
        primary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        _style(primary, "td-bottom-bar")
        for label, handler in [
            ("Send", self.send_prompt),
            ("Pause", self.pause_selected),
            ("Resume", self.resume_selected),
            ("Kill", self.kill_selected),
        ]:
            button = Gtk.Button(label=label)
            if label == "Kill":
                button.get_style_context().add_class("destructive-action")
            button.connect("clicked", lambda _b, h=handler: h())
            primary.pack_start(button, True, True, 0)
        self.pack_start(primary, False, False, 0)

        more = Gtk.MenuButton(label="More actions ▾")
        more.set_tooltip_text("Secondary session controls")
        menu = Gtk.Menu()
        secondary = [
            ("Launcher", self.window.open_launcher),
            ("New shell", self.window.new_shell_tab),
            ("Palette", self.window.open_command_palette),
            ("Rename", self.rename_selected),
            ("Retag", self.retag_selected),
            ("Terminal options", self.window.open_terminal_options),
            ("Status", self.send_status),
            ("Pin", self.pin_selected),
            ("Unpin", self.unpin_selected),
            ("Hide", self.hide_selected),
            ("Archive", self.archive_selected),
            ("Reopen", self.window.reopen_last_closed_tab),
            ("Pause hot", self.pause_hot_sessions),
            ("Restore", self.window.restore_previous_tabs),
            ("Master", self.window.open_master_window),
        ]
        for label, handler in secondary:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, h=handler: h())
            menu.append(item)
        menu.show_all()
        more.set_popup(menu)
        self.pack_start(more, False, False, 0)

        GLib.timeout_add_seconds(int(max(1, round(getattr(window.app_context.settings, "ui_refresh_interval_seconds", 5.0)))), self._refresh_timer)

    def _refresh_timer(self) -> bool:
        if not getattr(self, "_refresh_alive", False):
            return False
        if self.get_mapped() and self.get_visible():
            self.refresh()
        return True

    def _popup_menu(self, menu: Gtk.Menu, event: Gdk.EventButton) -> None:
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, int(getattr(event, "button", 3) or 3), int(getattr(event, "time", 0) or 0))

    def _menu_item(self, label: str, handler) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: handler())
        return item

    def _on_header_button_press(self, _widget, event: Gdk.EventButton) -> bool:
        if int(getattr(event, "button", 0) or 0) != 3:
            return False
        menu = Gtk.Menu()
        menu.append(self._menu_item("Move sessions pane to left", lambda: self.window.set_side_pane_position("left")))
        menu.append(self._menu_item("Move sessions pane to right", lambda: self.window.set_side_pane_position("right")))
        menu.append(self._menu_item("Toggle focus mode", self.window.toggle_focus_mode))
        menu.append(self._menu_item("Terminal options", self.window.open_terminal_options))
        self._popup_menu(menu, event)
        return True

    def _toggle_record_selection(self, record: SessionRecord, check: Gtk.CheckButton | None = None) -> None:
        if record.id in self.selected_ids:
            self.selected_ids.discard(record.id)
            active = False
        else:
            self.selected_ids.add(record.id)
            active = True
        if check is not None and check.get_active() != active:
            check.set_active(active)

    def _on_tag_button_press(self, _widget, event: Gdk.EventButton, record: SessionRecord, check: Gtk.CheckButton) -> bool:
        button = int(getattr(event, "button", 0) or 0)
        state = int(getattr(event, "state", 0) or 0)
        if button == 1 and state & Gdk.ModifierType.CONTROL_MASK:
            self._toggle_record_selection(record, check)
            return True
        if button == 3:
            self._show_session_menu(record, check, event)
            return True
        return False

    def _on_session_button_press(self, _widget, event: Gdk.EventButton, record: SessionRecord, check: Gtk.CheckButton) -> bool:
        if int(getattr(event, "button", 0) or 0) == 3:
            self._show_session_menu(record, check, event)
            return True
        return False

    def _context_records(self, record: SessionRecord) -> list[SessionRecord]:
        if record.id in self.selected_ids:
            records = self.selected_records()
            if records:
                return records
        return [record]

    def _context_label(self, action: str, records: list[SessionRecord]) -> str:
        return f"{action} selected ({len(records)})" if len(records) > 1 else action

    def _hide_records(self, records: list[SessionRecord]) -> None:
        for record in records:
            if record.id in self.window.tabs:
                self.window.hide_tab(record.id)
            else:
                hide_records(self.window.app_context.store, [record])
            self.selected_ids.discard(record.id)
        self.refresh()

    def _pin_records(self, records: list[SessionRecord]) -> None:
        if records:
            pin_records(self.window.app_context.store, records)
            self.refresh()

    def _unpin_records(self, records: list[SessionRecord]) -> None:
        if records:
            unpin_records(self.window.app_context.store, records)
            self.refresh()

    def _show_session_menu(self, record: SessionRecord, check: Gtk.CheckButton, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        records = self._context_records(record)
        selected_label = "Deselect" if record.id in self.selected_ids else "Select"
        menu.append(self._menu_item("Open", lambda: self.window.open_session(record)))
        menu.append(self._menu_item(selected_label, lambda: self._toggle_record_selection(record, check)))
        menu.append(self._menu_item("Rename", lambda: self.window.rename_session(record)))
        menu.append(self._menu_item(self._context_label("Retag", records), lambda: self.window.retag_sessions(records)))
        menu.append(self._menu_item(self._context_label("Pin", records), lambda: self._pin_records(records)))
        menu.append(self._menu_item(self._context_label("Unpin", records), lambda: self._unpin_records(records)))
        menu.append(self._menu_item(self._context_label("Hide", records), lambda: self._hide_records(records)))
        menu.append(self._menu_item(self._context_label("Kill", records), lambda: self._kill_records(records)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Move sessions pane to left", lambda: self.window.set_side_pane_position("left")))
        menu.append(self._menu_item("Move sessions pane to right", lambda: self.window.set_side_pane_position("right")))
        self._popup_menu(menu, event)

    def _kill_records(self, records: list[SessionRecord]) -> None:
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "kill",
            selection="side pane context menu",
        ):
            return
        for record in records:
            self.window.stop_session(record)
            self.selected_ids.discard(record.id)
        self.refresh()
        if records:
            self.window.show_toast(toast_message_for_action("killed", len(records)), kind="danger")

    def refresh(self) -> None:
        query = self.filter_entry.get_text().lower().strip()
        records = [
            record
            for record in self.window.app_context.store.list()
            if record.status != "stopped"
            and (not query or query in f"{record.name} {record.tag} {record.id} {record.command}".lower())
        ]
        live_ids = {record.id for record in records}
        self.selected_ids.intersection_update(live_ids)
        resource_bucket = int(time.monotonic() // self._refresh_bucket_seconds)
        signature = (
            query,
            resource_bucket,
            tuple((record.id, record.name, record.tag, record.status, record.visible, record.pinned, record.last_pid) for record in records),
            tuple(sorted(self.selected_ids)),
        )
        if signature == self._last_refresh_signature:
            return
        self._last_refresh_signature = signature
        for child in self.session_box.get_children():
            self.session_box.remove(child)
        # One tmux list-panes -a + fast /proc trees instead of N×(has-session+list-panes+full /proc).
        pids = self.window.app_context.backend.pane_pids(records)
        if not records:
            empty = Gtk.Label(label="No sessions yet.\nOpen a project or create a tab.", xalign=0)
            empty.set_line_wrap(True)
            _style(empty, "td-muted")
            row = Gtk.ListBoxRow()
            row.add(empty)
            self.session_box.add(row)
            self.session_box.show_all()
            return
        for record in records:
            pid = pids.get(record.id)
            if pid is None:
                pid = record.last_pid
            snap = self.window.app_context.sampler.snapshot(pid)
            health = classify_session(record, snap, self.window.app_context.budget)
            badge = health_badge(health)
            kind = kind_for_session(name=record.name, command=record.command, tag=record.tag)
            mem = None
            try:
                from .resources import format_bytes
                mem = format_bytes(snap.memory_bytes) if snap.exists else None
            except Exception:
                mem = None
            resource = format_resource_cue(snap.cpu_percent if snap.exists else None, mem)

            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=4)
            _style(card, "td-session-card")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            check = Gtk.CheckButton()
            check.set_active(record.id in self.selected_ids)
            check.connect("toggled", self._on_toggle, record.id)
            top.pack_start(check, False, False, 0)
            pin = "📌 " if record.pinned else ""
            title = Gtk.Label(label=f"{kind_glyph(kind)} {pin}{record.name}", xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            _style(title, "td-title")
            top.pack_start(title, True, True, 0)
            chip = Gtk.Label(label=record.tag)
            _style(chip, "td-chip")
            chip_event = Gtk.EventBox()
            chip_event.add(chip)
            chip_event.set_tooltip_text("Ctrl-click to add/remove this session from the multi-selection")
            chip_event.connect("button-press-event", self._on_tag_button_press, record, check)
            top.pack_start(chip_event, False, False, 0)
            card.pack_start(top, False, False, 0)

            meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            health_lbl = Gtk.Label(label=badge)
            _style(health_lbl, "td-badge", health_badge_class(badge))
            meta.pack_start(health_lbl, False, False, 0)
            id_lbl = Gtk.Label(label=record.id, xalign=0)
            _style(id_lbl, "td-id", "td-mono", "td-muted")
            id_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            meta.pack_start(id_lbl, True, True, 0)
            if resource:
                res = Gtk.Label(label=resource, xalign=1)
                _style(res, "td-resource")
                meta.pack_start(res, False, False, 0)
            card.pack_start(meta, False, False, 0)

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            rename = Gtk.Button(label="Rename")
            rename.set_tooltip_text("Rename this tab/session")
            rename.connect("clicked", lambda _b, r=record: self.window.rename_session(r))
            actions.pack_end(rename, False, False, 0)
            focus = Gtk.Button(label="Open")
            focus.connect("clicked", lambda _b, r=record: self.window.open_session(r))
            actions.pack_end(focus, False, False, 0)
            card.pack_start(actions, False, False, 0)

            card_event = Gtk.EventBox()
            card_event.add(card)
            card_event.connect("button-press-event", self._on_session_button_press, record, check)
            row = Gtk.ListBoxRow()
            row.add(card_event)
            self.session_box.add(row)
        self.session_box.show_all()

    def _on_toggle(self, button: Gtk.CheckButton, session_id: str) -> None:
        if button.get_active():
            self.selected_ids.add(session_id)
        else:
            self.selected_ids.discard(session_id)

    def selected_records(self) -> list[SessionRecord]:
        records = []
        for session_id in sorted(self.selected_ids):
            record = self.window.app_context.store.get(session_id)
            if record:
                records.append(record)
        return records

    def send_prompt(self) -> None:
        text = self.prompt.get_text()
        if not text.strip():
            return
        records = self.selected_records()
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "send",
            text=text,
            enter=True,
            selection="side pane selection",
        ):
            return
        for record in records:
            self.window.app_context.backend.send_text(record, text, enter=True)
        self.prompt.set_text("")

    def send_status(self) -> None:
        text = "Pause and summarise your current state in five concise lines. Do not modify files."
        records = self.selected_records()
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "send",
            text=text,
            enter=True,
            selection="side pane status request",
        ):
            return
        for record in records:
            self.window.app_context.backend.send_text(record, text, enter=True)

    def pause_selected(self) -> None:
        records = self.selected_records()
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "pause",
            selection="side pane selection",
        ):
            return
        for record in records:
            pid = self.window.app_context.backend.pause(record)
            self.window.app_context.store.update(record.id, status="paused", last_pid=pid)
        self.refresh()
        if records:
            self.window.show_toast(toast_message_for_action("paused", len(records)), kind="warn")

    def resume_selected(self) -> None:
        records = self.selected_records()
        for record in records:
            pid = self.window.app_context.backend.resume(record)
            self.window.app_context.store.update(record.id, status="running", last_pid=pid)
        self.refresh()
        if records:
            self.window.show_toast(toast_message_for_action("resumed", len(records)), kind="success")


    def pin_selected(self) -> None:
        records = self.selected_records()
        if records:
            pin_records(self.window.app_context.store, records)
            self.refresh()

    def unpin_selected(self) -> None:
        records = self.selected_records()
        if records:
            unpin_records(self.window.app_context.store, records)
            self.refresh()

    def rename_selected(self) -> None:
        records = self.selected_records()
        if not records:
            current = self.window.current_record()
            if current is not None:
                records = [current]
        if not records:
            self.window.show_toast("Select a session to rename", kind="warn")
            return
        # Rename acts on one tab at a time (same as CLI rename).
        self.window.rename_session(records[0])

    def retag_selected(self) -> None:
        records = self.selected_records()
        if not records:
            self.window.show_toast("Select sessions to retag", kind="warn")
            return
        self.window.retag_sessions(records)

    def pause_hot_sessions(self) -> None:
        self.window.pause_hot_sessions()

    def hide_selected(self) -> None:
        records = self.selected_records()
        if not records:
            return
        for record in records:
            if record.id in self.window.tabs:
                self.window.hide_tab(record.id)
            else:
                hide_records(self.window.app_context.store, [record])
        self.refresh()

    def archive_selected(self) -> None:
        records = self.selected_records()
        if not records:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Archive selected sessions?",
        )
        dialog.format_secondary_text("This removes them from normal browser views but does not stop their processes.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        for record in records:
            if record.id in self.window.tabs:
                self.window.hide_tab(record.id)
        archive_records(self.window.app_context.store, records)
        for record in records:
            self.selected_ids.discard(record.id)
        self.refresh()

    def kill_selected(self) -> None:
        records = self.selected_records()
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "kill",
            selection="side pane selection",
        ):
            return
        for record in records:
            self.window.stop_session(record)
            self.selected_ids.discard(record.id)
        self.refresh()
        if records:
            self.window.show_toast(toast_message_for_action("killed", len(records)), kind="danger")


class MasterWindow(Gtk.Window):
    COLUMNS = [
        "ID",
        "Window",
        "Name",
        "Tag",
        "Pin",
        "PID",
        "State",
        "Health",
        "CPU",
        "RAM",
        "Procs",
        "Runtime",
        "Focus Key",
        "Pause Key",
        "Command",
        "Directory",
    ]

    def __init__(self, app_context: "TermDeckApplication"):
        super().__init__(title="TermDeck Master Tasks")
        self.app_context = app_context
        self._refresh_alive = True
        self._capture_preview_cache: dict[tuple[str, str, int | None, int], str] = {}
        self._capture_preview_bucket_seconds = max(
            1.0,
            min(3.0, float(getattr(app_context.settings, "ui_refresh_interval_seconds", 5.0))),
        )
        self.connect("destroy", lambda *_: setattr(self, "_refresh_alive", False))
        _style(self, "td-table-window")
        dens = normalise_density(getattr(app_context.settings, "ui_density", "comfortable"))
        _style(self, f"td-density-{dens}")
        self.set_default_size(1180, 620)
        _wire_auxiliary_close(self)
        self.store = Gtk.ListStore(*([str] * len(self.COLUMNS)))
        self.tree = Gtk.TreeView(model=self.store)
        _style(self.tree, "td-master-tree")
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().connect("changed", lambda *_: self.update_capture_preview())
        for idx, column in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            if column in {"ID", "PID", "Focus Key", "Pause Key", "CPU", "RAM"}:
                renderer.set_property("family", "Monospace")
            if column == "Health":
                # Badge-like emphasis via weight; colour comes from CSS selection.
                renderer.set_property("weight", 600)
            col = Gtk.TreeViewColumn(column, renderer, text=idx)
            col.set_resizable(True)
            col.set_sort_column_id(idx)
            if column in {"Command", "Directory", "Health"}:
                col.set_expand(True)
            self.tree.append_column(col)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.filter_entry = Gtk.SearchEntry(placeholder_text="filter by id, tag, window, health, state, command, directory")
        _style(self.filter_entry, "td-search-entry")
        _connect_debounced_refresh(self.filter_entry, self.refresh)
        top.pack_start(self.filter_entry, True, True, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        top.pack_start(close, False, False, 0)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.filter_entry.set_text(""))
        top.pack_start(clear, False, False, 0)
        outer.pack_start(top, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)

        self.capture_view = Gtk.TextView()
        self.capture_view.set_editable(False)
        self.capture_view.set_cursor_visible(False)
        self.capture_view.set_monospace(True)
        self.capture_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        capture_scroller = Gtk.ScrolledWindow()
        capture_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        capture_scroller.set_size_request(-1, 150)
        capture_scroller.add(self.capture_view)
        outer.pack_start(capture_scroller, False, True, 0)

        prompt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prompt_entry = Gtk.Entry(placeholder_text="send text to selected sessions; use Send+Enter only when intentional")
        prompt_row.pack_start(self.prompt_entry, True, True, 0)
        for label, handler in [
            ("Send text", self.send_text_selected),
            ("Send+Enter", self.send_enter_selected),
            ("Status", self.status_selected),
        ]:
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, h=handler: h())
            prompt_row.pack_start(button, False, False, 0)
        outer.pack_start(prompt_row, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in [
            ("Focus", self.focus_selected),
            ("Rename", self.rename_selected),
            ("Pause", self.pause_selected),
            ("Resume", self.resume_selected),
            ("Pin", self.pin_selected),
            ("Unpin", self.unpin_selected),
            ("Hide", self.hide_selected),
            ("Kill", self.kill_selected),
            ("Archive", self.archive_selected),
            ("Pause Hot", self.pause_hot),
            ("Refresh", self.refresh),
        ]:
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, h=handler: h())
            actions.pack_start(button, False, False, 0)
        outer.pack_start(actions, False, False, 0)
        self.add(outer)
        GLib.timeout_add_seconds(int(max(1, round(getattr(app_context.settings, "ui_refresh_interval_seconds", 5.0)))), self._refresh_timer)
        self.refresh()

    def _refresh_timer(self) -> bool:
        if not getattr(self, "_refresh_alive", False):
            return False
        if self.get_visible():
            self.refresh()
        return True

    def selected_ids(self) -> list[str]:
        model, paths = self.tree.get_selection().get_selected_rows()
        ids = []
        for path in paths:
            iter_ = model.get_iter(path)
            ids.append(model.get_value(iter_, 0))
        return ids

    def selected_records(self) -> list[SessionRecord]:
        records = []
        for session_id in self.selected_ids():
            record = self.app_context.store.get(session_id)
            if record:
                records.append(record)
        return records

    def refresh(self, *_args) -> None:
        selected = set(self.selected_ids())
        self.store.clear()
        rows = []
        records = self.app_context.store.list()
        pids = self.app_context.backend.pane_pids(records)
        for record in records:
            pid = pids.get(record.id)
            if pid is None:
                pid = record.last_pid
            snap = self.app_context.sampler.snapshot(pid)
            shortcut = tab_shortcut(record.tab_index + 1) if record.visible else tab_shortcut(0)
            rows.append(
                row_from_record(
                    record,
                    pid=pid,
                    snapshot=snap,
                    focus_key=shortcut.focus,
                    pause_key=shortcut.pause_toggle,
                    policy=self.app_context.budget,
                )
            )
        for row in filter_rows(rows, self.filter_entry.get_text()):
            iter_ = self.store.append(row.as_list())
            if row.session_id in selected:
                path = self.store.get_path(iter_)
                self.tree.get_selection().select_path(path)
        self.update_capture_preview()

    def set_capture_text(self, text: str) -> None:
        buffer = self.capture_view.get_buffer()
        buffer.set_text(text)

    def _capture_preview_cache_key(self, record: SessionRecord) -> tuple[str, str, int | None, int]:
        bucket = int(time.monotonic() // self._capture_preview_bucket_seconds)
        return (record.id, record.status, record.last_pid, bucket)

    def capture_preview_text(self, record: SessionRecord) -> str:
        key = self._capture_preview_cache_key(record)
        cached = self._capture_preview_cache.get(key)
        if cached is not None:
            return cached
        capture = self.app_context.backend.capture(record, lines=80).rstrip()
        if capture:
            redacted = redact_text(capture)
            capture = redacted.text
            if redacted.replacements:
                labels = ", ".join(redacted.labels)
                capture = f"[TermDeck redacted {redacted.replacements} secret-like value(s): {labels}]\n\n{capture}"
        if not capture:
            capture = "No captured output yet, or the hidden session is not currently available."
        self._capture_preview_cache = {key: capture}
        return capture

    def update_capture_preview(self) -> None:
        records = self.selected_records()
        if not records:
            self.set_capture_text("Select a session to preview its latest terminal output.")
            return
        if len(records) > 1:
            names = ", ".join(record.name for record in records[:6])
            suffix = "..." if len(records) > 6 else ""
            self.set_capture_text(f"{len(records)} sessions selected: {names}{suffix}\nUse the buttons below to control them together.")
            return
        record = records[0]
        self.set_capture_text(self.capture_preview_text(record))

    def send_to_selected(self, *, enter: bool) -> None:
        text = self.prompt_entry.get_text()
        if not text:
            return
        records = self.selected_records()
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "send",
            text=text,
            enter=enter,
            selection="master task selection",
        ):
            return
        for record in records:
            self.app_context.backend.send_text(record, text, enter=enter)
        if enter:
            self.prompt_entry.set_text("")
        self.update_capture_preview()

    def send_text_selected(self, *_args) -> None:
        self.send_to_selected(enter=False)

    def send_enter_selected(self, *_args) -> None:
        self.send_to_selected(enter=True)

    def status_selected(self, *_args) -> None:
        text = "Pause and summarise your current state in five concise lines. Do not modify files."
        records = self.selected_records()
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "send",
            text=text,
            enter=True,
            selection="master task status request",
        ):
            return
        for record in records:
            self.app_context.backend.send_text(record, text, enter=True)
        self.update_capture_preview()

    def focus_selected(self, *_args) -> None:
        records = self.selected_records()
        if records and self.app_context.windows:
            self.app_context.windows[-1].open_session(records[0])

    def rename_selected(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        target = self.app_context.windows[-1] if self.app_context.windows else None
        if target is None:
            return
        target.rename_session(records[0])
        self.refresh()

    def pin_selected(self, *_args) -> None:
        records = self.selected_records()
        if records:
            pin_records(self.app_context.store, records)
        self.refresh()

    def unpin_selected(self, *_args) -> None:
        records = self.selected_records()
        if records:
            unpin_records(self.app_context.store, records)
        self.refresh()

    def pause_selected(self, *_args) -> None:
        records = self.selected_records()
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "pause",
            selection="master task selection",
        ):
            return
        for record in records:
            pid = self.app_context.backend.pause(record)
            self.app_context.store.update(record.id, status="paused", last_pid=pid)
        self.refresh()

    def resume_selected(self, *_args) -> None:
        for record in self.selected_records():
            pid = self.app_context.backend.resume(record)
            self.app_context.store.update(record.id, status="running", last_pid=pid)
        self.refresh()

    def pause_hot(self, *_args) -> None:
        records = []
        for record in self.selected_records() or self.app_context.store.list():
            if record.pinned:
                continue
            pid = self.app_context.backend.pane_pid(record)
            snap = self.app_context.sampler.snapshot(pid)
            health = classify_session(record, snap, self.app_context.budget)
            if should_pause_for_attention(health):
                records.append(record)
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "pause-hot",
            selection="master task hot sessions",
        ):
            return
        for record in records:
            new_pid = self.app_context.backend.pause(record)
            self.app_context.store.update(record.id, status="paused", last_pid=new_pid)
        self.refresh()

    def hide_selected(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        for record in records:
            hidden_in_live_window = False
            for window in self.app_context.windows:
                if record.id in window.tabs:
                    window.hide_tab(record.id)
                    hidden_in_live_window = True
            if not hidden_in_live_window:
                hide_records(self.app_context.store, [record])
        self.refresh()

    def archive_selected(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Archive selected sessions?",
        )
        dialog.format_secondary_text("This removes them from normal browser views but does not stop their processes.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        for record in records:
            for window in self.app_context.windows:
                if record.id in window.tabs:
                    window.hide_tab(record.id)
        archive_records(self.app_context.store, records)
        self.refresh()

    def kill_selected(self, *_args) -> None:
        records = self.selected_records()
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "kill",
            selection="master task selection",
        ):
            return
        for record in records:
            self.app_context.backend.terminate(record)
            self.app_context.store.update(record.id, status="stopped", visible=False)
        self.refresh()


class TermDeckWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        app_context: "TermDeckApplication",
        *,
        window_id: str | None = None,
        window_title: str | None = None,
        restore_records: list[SessionRecord] | None = None,
        open_initial_shell: bool = True,
    ):
        super().__init__(application=app_context, title="TermDeck")
        self.app_context = app_context
        self.keymap = KeyMap(
            app_context.theme,
            profile=app_context.settings.keyboard_profile,
            import_terminator=app_context.settings.import_terminator_keybindings,
        )
        self.window_id = window_id or short_id("win")
        self.window_title = window_title or "Main"
        self._restore_records = restore_records
        self.open_initial_shell = open_initial_shell
        self.tabs: dict[str, TerminalTab] = {}
        self.master_window: MasterWindow | None = None
        self.window_overview: WindowOverview | None = None
        self.shortcut_window: ShortcutWindow | None = None
        self.search_window: SessionSearchWindow | None = None
        self.set_default_size(app_context.theme.window_width or 1280, app_context.theme.window_height or 780)
        self.focus_mode = bool(getattr(app_context.settings, "focus_mode", False))
        self._toast_source = None
        # Relative point delta from the configured theme font (Ctrl++ / Ctrl+-).
        self.font_zoom_delta = 0

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label=f"TermDeck · {self.window_title}", xalign=0)
        _style(title, "td-title")
        subtitle = Gtk.Label(label="browser-like terminal tasks", xalign=0)
        _style(subtitle, "td-subtitle", "td-muted")
        title_box.pack_start(title, False, False, 0)
        title_box.pack_start(subtitle, False, False, 0)
        header.set_custom_title(title_box)
        self.set_titlebar(header)

        # Grouped browser chrome: create · discover · manage · window
        create_group = [
            ("New", "list-add-symbolic", "New task launcher", self.open_launcher, True),
            ("Shell", "utilities-terminal-symbolic", "New shell tab", self.new_shell_tab, False),
            ("Task", "document-new-symbolic", "New custom task", self.new_command_tab, False),
            ("Project", "folder-symbolic", "Detected project workspace", self.open_detected_project, False),
        ]
        discover_group = [
            ("Palette", "edit-find-symbolic", "Command palette (Ctrl+Shift+P)", self.open_command_palette, False),
            ("Search", "system-search-symbolic", "Search remembered tabs", self.open_search_window, False),
        ]
        manage_group = [
            ("Master", "utilities-system-monitor-symbolic", "Master task manager", self.open_master_window, False),
            ("Windows", "view-grid-symbolic", "Window overview", self.open_window_overview, False),
            ("Keys", "input-keyboard-symbolic", "Keyboard shortcuts", self.open_shortcut_window, False),
            ("Term", "preferences-desktop-font-symbolic", "Terminal font, cursor, palette, scrollback", self.open_terminal_options, False),
        ]
        window_group = [
            ("Reopen", "edit-undo-symbolic", "Reopen last closed tab", self.reopen_last_closed_tab, False),
            ("Focus", "view-fullscreen-symbolic", "Toggle focus mode (hide side pane)", self.toggle_focus_mode, False),
            ("Window", "window-new-symbolic", "New TermDeck window", self.new_window, False),
        ]
        for label, icon, tip, handler, suggested in create_group:
            header.pack_start(_icon_button(label, icon, tip, handler, suggested=suggested))
        for index, (label, icon, tip, handler, suggested) in enumerate(discover_group + manage_group + window_group):
            button = _icon_button(label, icon, tip, handler, suggested=suggested)
            if index == 0:
                button.get_style_context().add_class("td-header-group-end")
            header.pack_end(button)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.notebook = Gtk.Notebook(scrollable=True)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.notebook.set_scrollable(True)
        self.side = SidePane(self)
        self.side_position = app_context.settings.side_pane_position
        self._pack_side_pane()

        root = Gtk.Overlay()
        root.add(self.paned)
        self.toast_revealer = Gtk.Revealer()
        self.toast_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.toast_revealer.set_transition_duration(160)
        self.toast_revealer.set_halign(Gtk.Align.CENTER)
        self.toast_revealer.set_valign(Gtk.Align.END)
        self.toast_revealer.set_margin_bottom(18)
        self.toast_label = Gtk.Label(label="")
        _style(self.toast_label, "td-toast")
        self.toast_revealer.add(self.toast_label)
        root.add_overlay(self.toast_revealer)
        self.add(root)

        density = normalise_density(app_context.settings.ui_density)
        _style(self, f"td-density-{density}")
        if self.focus_mode:
            _style(self, "td-focus-mode")
            self.side.set_visible(False)

        self.connect("key-press-event", self.on_key_press)
        self.connect("delete-event", self.on_delete)
        self.show_all()
        if self.focus_mode:
            self.side.set_visible(False)
        self.side.refresh()
        restored = self.restore_previous_tabs(records=restore_records)
        if open_initial_shell and not restored and not self.app_context.store.list():
            self.new_shell_tab()

    def on_delete(self, *_args) -> bool:
        for tab in list(self.tabs.values()):
            tab.widget.detach()
        return False

    def _build_tab_label(self, record: SessionRecord) -> Gtk.Box:
        """Notebook tab with kind glyph, pin, attention cue, and close control."""
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        kind = kind_for_session(name=record.name, command=record.command, tag=record.tag)
        pin = "📌" if record.pinned else ""
        attention = ""
        try:
            pid = record.last_pid
            snap = self.app_context.sampler.snapshot(pid) if pid else None
            if snap is not None:
                health = classify_session(record, snap, self.app_context.budget)
                if should_pause_for_attention(health) or "critical" in health_badge(health).lower():
                    attention = "●"
        except Exception:
            attention = ""
        text = f"{kind_glyph(kind)} {pin}{attention}{record.name}".strip()
        name = Gtk.Label(label=text)
        name.set_tooltip_text(f"{record.tag} · {record.command}\nDouble-click to rename · Right-click for tab actions")
        name.set_events(name.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK)
        name.connect("button-press-event", self._on_tab_label_press, record.id)
        label_box.pack_start(name, True, True, 0)
        close = Gtk.Button(label="×")
        close.set_relief(Gtk.ReliefStyle.NONE)
        _style(close, "td-tab-close")
        close.set_tooltip_text("Pinned tab: right-click for Unpin and hide" if record.pinned else "Hide tab")
        close.connect("clicked", lambda _b, sid=record.id: self._hide_tab_from_close_button(sid))
        label_box.pack_start(close, False, False, 0)
        label_box.show_all()
        return label_box

    def _tab_menu_item(self, label: str, handler) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: handler())
        return item

    def _hide_tab_from_close_button(self, session_id: str) -> None:
        record = self.app_context.store.get(session_id)
        if record is not None and record.pinned:
            self.show_toast("Pinned tab: right-click and choose Unpin and hide tab", kind="warn")
            return
        self.hide_tab(session_id)

    def _pin_tab(self, record: SessionRecord, pinned: bool) -> None:
        if pinned:
            pin_records(self.app_context.store, [record])
        else:
            unpin_records(self.app_context.store, [record])
        updated = self.app_context.store.get(record.id) or record
        self.refresh_tab_label(updated)
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()

    def _unpin_and_hide_tab(self, record: SessionRecord) -> None:
        unpin_records(self.app_context.store, [record])
        self.hide_tab(record.id, include_pinned=True)
        self.show_toast("Unpinned and hid tab", kind="info")

    def _visible_tab_records(self) -> list[SessionRecord]:
        records = [tab.record for tab in self.tabs.values()]
        return sorted(records, key=lambda candidate: candidate.tab_index)

    def _hide_tab_records(self, records: list[SessionRecord]) -> None:
        hidden = 0
        for candidate in records:
            was_visible = candidate.id in self.tabs
            self.hide_tab(candidate.id)
            if was_visible and candidate.id not in self.tabs:
                hidden += 1
        if hidden:
            self.show_toast(f"Hid {hidden} tab(s)", kind="info")

    def _hide_other_tabs(self, record: SessionRecord) -> None:
        self._hide_tab_records([candidate for candidate in self._visible_tab_records() if candidate.id != record.id])

    def _hide_tabs_to_right(self, record: SessionRecord) -> None:
        self._hide_tab_records([candidate for candidate in self._visible_tab_records() if candidate.tab_index > record.tab_index])

    def _show_tab_context_menu(self, record: SessionRecord, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        menu.append(self._tab_menu_item("Rename", lambda: self.rename_session(record)))
        menu.append(self._tab_menu_item("Retag", lambda: self.retag_sessions([record])))
        pin_label = "Unpin tab" if record.pinned else "Pin tab"
        menu.append(self._tab_menu_item(pin_label, lambda: self._pin_tab(record, not record.pinned)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._tab_menu_item("Hide tab", lambda: self.hide_tab(record.id)))
        if record.pinned:
            menu.append(self._tab_menu_item("Unpin and hide tab", lambda: self._unpin_and_hide_tab(record)))
        menu.append(self._tab_menu_item("Hide other tabs", lambda: self._hide_other_tabs(record)))
        menu.append(self._tab_menu_item("Hide tabs to the right", lambda: self._hide_tabs_to_right(record)))
        menu.append(self._tab_menu_item("Kill session", lambda: self.stop_session(record)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._tab_menu_item("Move sessions pane to left", lambda: self.set_side_pane_position("left")))
        menu.append(self._tab_menu_item("Move sessions pane to right", lambda: self.set_side_pane_position("right")))
        menu.append(self._tab_menu_item("Terminal options", self.open_terminal_options))
        menu.append(self._tab_menu_item("Master tasks", self.open_master_window))
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, int(getattr(event, "button", 3) or 3), int(getattr(event, "time", 0) or 0))

    def _on_tab_label_press(self, _widget, event: Gdk.EventButton, session_id: str) -> bool:
        button = int(getattr(event, "button", 0) or 0)
        record = self.app_context.store.get(session_id)
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and button == 1:
            if record is not None:
                self.rename_session(record)
            return True
        if button == 3 and record is not None:
            self._show_tab_context_menu(record, event)
            return True
        return False

    def prompt_text(self, title: str, label: str, initial: str = "") -> str | None:
        """Simple modal text prompt. Returns None when cancelled."""
        dialog = Gtk.Dialog(title=title, transient_for=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_modal(True)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        prompt = Gtk.Label(label=label, xalign=0)
        entry = Gtk.Entry(text=initial)
        entry.set_activates_default(True)
        _style(entry, "td-search-entry")
        box.pack_start(prompt, False, False, 0)
        box.pack_start(entry, False, False, 0)
        dialog.show_all()
        entry.grab_focus()
        response = dialog.run()
        value = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not value:
            return None
        return value

    def refresh_tab_label(self, record: SessionRecord) -> None:
        tab = self.tabs.get(record.id)
        if tab is None:
            return
        tab.record = record
        page = self.notebook.page_num(tab.widget)
        if page < 0:
            return
        self.notebook.set_tab_label(tab.widget, self._build_tab_label(record))

    def rename_session(self, record: SessionRecord) -> SessionRecord | None:
        """Rename a browser tab/session without touching the running process."""
        new_name = self.prompt_text("Rename tab", f"New name for “{record.name}”:", record.name)
        if new_name is None or new_name == record.name:
            return None
        result = rename_record(self.app_context.store, record, new_name)
        if not result.ok:
            self.show_toast(result.detail or "Rename failed", kind="danger")
            return None
        updated = self.app_context.store.get(record.id) or record
        self.refresh_tab_label(updated)
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        for window in self.app_context.windows:
            if window is not self and record.id in window.tabs:
                window.refresh_tab_label(updated)
                window.side.refresh()
        self.show_toast(f"Renamed to {updated.name}", kind="success")
        return updated

    def retag_sessions(self, records: list[SessionRecord]) -> None:
        if not records:
            return
        sample = records[0].tag
        new_tag = self.prompt_text("Retag sessions", f"New tag for {len(records)} session(s):", sample)
        if new_tag is None:
            return
        retag_records(self.app_context.store, records, new_tag)
        for record in records:
            updated = self.app_context.store.get(record.id)
            if updated is not None:
                self.refresh_tab_label(updated)
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        self.show_toast(toast_message_for_action("retagged", len(records), detail=new_tag), kind="success")

    def rename_current_session(self) -> None:
        record = self.current_record()
        if record is None:
            self.show_toast("No active tab to rename", kind="warn")
            return
        self.rename_session(record)

    def open_terminal_options(self) -> None:
        dialog = TerminalOptionsDialog(self, self.app_context.settings, self.app_context.theme)
        response = dialog.run()
        values = dialog.values() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not values:
            return
        try:
            self.app_context.apply_terminal_options(**values)  # type: ignore[arg-type]
        except SettingsError as exc:
            self.show_toast(str(exc), kind="danger")
            return
        self.show_toast("Terminal options applied", kind="success")

    def show_toast(self, message: str, *, kind: str = "info", seconds: float | None = None) -> None:
        """Non-blocking lightweight feedback for bulk actions."""
        if not message:
            return
        for cls in ("td-toast-success", "td-toast-warn", "td-toast-danger", "td-toast-info"):
            self.toast_label.get_style_context().remove_class(cls)
        mapping = {
            "success": "td-toast-success",
            "ok": "td-toast-success",
            "warn": "td-toast-warn",
            "warning": "td-toast-warn",
            "danger": "td-toast-danger",
            "error": "td-toast-danger",
            "info": "td-toast-info",
        }
        self.toast_label.get_style_context().add_class(mapping.get(kind, "td-toast-info"))
        self.toast_label.set_text(message)
        self.toast_revealer.set_reveal_child(True)
        if self._toast_source is not None:
            try:
                GLib.source_remove(self._toast_source)
            except Exception:
                pass
        delay = seconds
        if delay is None:
            delay = spacing_for_density(self.app_context.settings.ui_density)["toast_seconds"]

        def _hide() -> bool:
            self.toast_revealer.set_reveal_child(False)
            self._toast_source = None
            return False

        self._toast_source = GLib.timeout_add(int(delay * 1000), _hide)

    def toggle_focus_mode(self) -> None:
        self.focus_mode = not self.focus_mode
        self.side.set_visible(not self.focus_mode)
        ctx = self.get_style_context()
        if self.focus_mode:
            ctx.add_class("td-focus-mode")
            self.show_toast("Focus mode on · side pane hidden", kind="info")
        else:
            ctx.remove_class("td-focus-mode")
            self.side.set_visible(True)
            self.side.refresh()
            self.show_toast("Focus mode off", kind="info")

    def open_detected_project(self) -> int:
        detection = detect_project(os.getcwd(), include_ai=False)
        opened = 0
        for index, recipe_window in enumerate(detection.recipe.windows, start=1):
            self.app_context._open_recipe_window(recipe_window, detection.name, index)
            opened += 1
        return opened

    def new_window(self) -> None:
        index = len(self.app_context.windows) + 1
        win = TermDeckWindow(self.app_context, window_title=f"Window {index}")
        self.app_context.windows.append(win)
        win.show_all()

    def open_launcher(self) -> None:
        dialog = CommandDialog(self)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            name, tag, command, cwd = dialog.values()
            dialog.destroy()
            self.create_session(name, tag, command, cwd)
        else:
            dialog.destroy()

    def new_shell_tab(self) -> None:
        shell = os.environ.get("SHELL", "/bin/sh")
        self.create_session("shell", "default", shell, os.getcwd())

    def open_command_palette(self) -> None:
        items = build_palette_items(self.app_context.store.list())
        dialog = ActionPaletteDialog(self, items)
        response = dialog.run()
        item = dialog.selected()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and item is not None:
            self.execute_palette_item(item)

    def execute_palette_item(self, item: PaletteItem) -> None:
        if item.kind == "builtin":
            actions = {
                "new_shell": self.new_shell_tab,
                "new_task": self.open_launcher,
                "project_detect": self.open_detected_project,
                "master": self.open_master_window,
                "window_overview": self.open_window_overview,
                "shortcuts": self.open_shortcut_window,
                "search_sessions": self.open_search_window,
                "reopen_closed": self.reopen_last_closed_tab,
                "restore": self.restore_previous_tabs,
                "toggle_side": self.toggle_side,
                "focus_mode": self.toggle_focus_mode,
                "focus_side": self.focus_side_prompt,
                "status_selected": self.side.send_status,
                "pause_selected": self.side.pause_selected,
                "resume_selected": self.side.resume_selected,
                "pin_selected": self.side.pin_selected,
                "unpin_selected": self.side.unpin_selected,
                "hide_selected": self.side.hide_selected,
                "pause_hot": self.pause_hot_sessions,
                "kill_selected": self.side.kill_selected,
                "archive_selected": self.side.archive_selected,
                "rename_selected": self.side.rename_selected,
                "rename_current": self.rename_current_session,
                "retag_selected": self.side.retag_selected,
                "terminal_options": self.open_terminal_options,
            }
            handler = actions.get(item.payload)
            if handler:
                handler()
            return
        if item.kind == "session":
            record = self.app_context.store.get(item.payload)
            if record:
                self.open_session(record)
            return
        if item.kind == "template":
            try:
                template = template_by_id(item.payload)
            except KeyError:
                return
            if template.id == "custom":
                self.open_launcher()
                return
            name, tag, command, cwd = template.materialise(os.getcwd())
            self.create_session(name, tag, command, cwd)

    def new_command_tab(self) -> None:
        dialog = CommandDialog(self)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            name, tag, command, cwd = dialog.values()
            dialog.destroy()
            self.create_session(name, tag, command, cwd)
        else:
            dialog.destroy()

    def create_session(self, name: str, tag: str, command: str, cwd: str) -> None:
        record = self.app_context.store.create(
            name=name,
            tag=tag,
            command=command,
            cwd=cwd,
            window_id=self.window_id,
            window_title=self.window_title,
            tab_index=self.notebook.get_n_pages(),
            visible=True,
        )
        self.app_context.backend.create_or_resume(record)
        pid = self.app_context.backend.pane_pid(record)
        record = self.app_context.store.update(record.id, status="running", last_pid=pid)
        self.open_session(record)
        self.side.refresh()


    def restore_previous_tabs(self, records: list[SessionRecord] | None = None) -> int:
        """Reattach remembered tabs for this window without restarting missing tasks."""
        restored = 0
        restore_records = sorted(records, key=lambda row: row.tab_index) if records is not None else self.app_context.store.restore_candidates(
            limit=12,
            window_id=self.window_id,
        )
        remember_layout = records is None
        for record in restore_records:
            if record.id in self.tabs:
                continue
            before = len(self.tabs)
            self.open_session(record, restart_missing=False, remember_layout=remember_layout)
            if len(self.tabs) > before:
                restored += 1
        self.side.refresh()
        return restored

    def reopen_last_closed_tab(self) -> int:
        """Browser-style reopen for the most recently hidden terminal tab."""

        records = recently_closed_records(self.app_context.store, limit=1)
        if not records:
            return 0
        results = reopen_closed_records(
            self.app_context.store,
            records,
            window_id=self.window_id,
            window_title=self.window_title,
        )
        reopened = 0
        for result in results:
            record = self.app_context.store.get(result.id)
            if not record:
                continue
            before = len(self.tabs)
            self.open_session(record, restart_missing=False, remember_layout=False)
            if record.id in self.tabs or len(self.tabs) > before:
                reopened += 1
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        return reopened

    def observed_status(self, record: SessionRecord, pid: int | None) -> str:
        snap = self.app_context.sampler.snapshot(pid)
        if snap.exists:
            return "paused" if snap.state == "T" else "running"
        if record.status == "paused":
            return "paused"
        return record.status if record.status in {"new", "stopped"} else "stopped"

    def toggle_pause_by_index(self, one_based: int) -> None:
        records = self.app_context.store.list()
        idx = one_based - 1
        if not (0 <= idx < len(records)):
            return
        record = records[idx]
        pid = self.app_context.backend.pane_pid(record)
        status = self.observed_status(record, pid)
        if status == "paused":
            pid = self.app_context.backend.resume(record)
            self.app_context.store.update(record.id, status="running", last_pid=pid)
        else:
            pid = self.app_context.backend.pause(record)
            self.app_context.store.update(record.id, status="paused", last_pid=pid)
        self.side.refresh()
        if self.master_window and self.master_window.get_visible():
            self.master_window.refresh()

    def open_session(self, record: SessionRecord, restart_missing: bool = True, remember_layout: bool = True) -> None:
        if not self.app_context.backend.session_exists(record):
            if not restart_missing:
                self.app_context.store.update(record.id, status="stopped", last_pid=None, visible=False)
                self.side.refresh()
                return
            self.app_context.backend.create_or_resume(record)
        pid = self.app_context.backend.pane_pid(record)
        status = self.observed_status(record, pid)
        layout_changes = {}
        if remember_layout:
            layout_changes = {
                "visible": True,
                "window_id": self.window_id,
                "window_title": self.window_title,
                "tab_index": self.notebook.get_n_pages(),
            }
        record = self.app_context.store.update(
            record.id,
            last_opened_at=utc_now(),
            status=status,
            last_pid=pid,
            **layout_changes,
        )
        if record.id in self.tabs:
            self._focus_session(record.id)
            return
        terminal = AttachTerminal(
            self.app_context.backend.attach_command(record),
            record.cwd,
            self.app_context.theme,
            key_press_handler=self.on_terminal_key_press,
            font_override=self.zoomed_font(),
        )
        tab = TerminalTab(record, terminal)
        self.tabs[record.id] = tab
        label_box = self._build_tab_label(record)
        page = self.notebook.append_page(terminal, label_box)
        self.notebook.set_current_page(page)
        terminal.show_all()
        self.side.refresh()

    def _focus_session(self, session_id: str) -> None:
        tab = self.tabs.get(session_id)
        if not tab:
            return
        page = self.notebook.page_num(tab.widget)
        if page >= 0:
            self.notebook.set_current_page(page)
            try:
                self.app_context.store.update(
                    session_id,
                    visible=True,
                    window_id=self.window_id,
                    window_title=self.window_title,
                    tab_index=page,
                )
            except KeyError:
                pass
            tab.widget.terminal.grab_focus()

    def current_session_id(self) -> str | None:
        page = self.notebook.get_current_page()
        widget = self.notebook.get_nth_page(page)
        for session_id, tab in self.tabs.items():
            if tab.widget is widget:
                return session_id
        return None

    def current_record(self) -> SessionRecord | None:
        session_id = self.current_session_id()
        return self.app_context.store.get(session_id) if session_id else None

    def _session_pause_state(self, record: SessionRecord) -> tuple[bool, int | None]:
        pid = self.app_context.backend.pane_pid(record)
        snap = self.app_context.sampler.snapshot(pid)
        if snap.exists:
            return snap.state == "T", pid
        return record.status == "paused", pid

    def toggle_pause_session(self, record: SessionRecord) -> None:
        paused, _pid = self._session_pause_state(record)
        if paused:
            pid = self.app_context.backend.resume(record)
            status = "running"
        else:
            pid = self.app_context.backend.pause(record)
            status = "paused"
        self.app_context.store.update(record.id, status=status, last_pid=pid)
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        self.show_toast(toast_message_for_action(status, 1, detail=record.name), kind="success" if status == "running" else "warn")

    def toggle_pause_current_session(self) -> None:
        record = self.current_record()
        if record:
            self.toggle_pause_session(record)

    def pause_hot_sessions(self) -> int:
        records = []
        for record in self.app_context.store.list():
            if record.pinned:
                continue
            pid = self.app_context.backend.pane_pid(record)
            snap = self.app_context.sampler.snapshot(pid)
            health = classify_session(record, snap, self.app_context.budget)
            if should_pause_for_attention(health):
                records.append(record)
        if not self.app_context.confirm_control_plan(
            self,
            records,
            "pause-hot",
            selection="visible hot sessions",
        ):
            return 0
        paused = 0
        for record in records:
            new_pid = self.app_context.backend.pause(record)
            self.app_context.store.update(record.id, status="paused", last_pid=new_pid)
            paused += 1
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        return paused

    def toggle_pause_tab(self, one_based: int) -> None:
        idx = one_based - 1
        if not (0 <= idx < self.notebook.get_n_pages()):
            return
        widget = self.notebook.get_nth_page(idx)
        for session_id, tab in self.tabs.items():
            if tab.widget is widget:
                record = self.app_context.store.get(session_id)
                if record:
                    self.toggle_pause_session(record)
                return

    def hide_current_tab(self) -> None:
        session_id = self.current_session_id()
        if session_id:
            self.hide_tab(session_id)

    def hide_tab(self, session_id: str, *, include_pinned: bool = False) -> None:
        record = self.app_context.store.get(session_id)
        if record and record.pinned and not include_pinned:
            return
        tab = self.tabs.pop(session_id, None)
        if not tab:
            return
        page = self.notebook.page_num(tab.widget)
        if page >= 0:
            tab.widget.detach()
            self.notebook.remove_page(page)
        try:
            self.app_context.store.update(session_id, visible=False)
        except KeyError:
            pass
        self._renumber_visible_tabs()
        self.side.refresh()

    def stop_current_session(self) -> None:
        session_id = self.current_session_id()
        if not session_id:
            return
        record = self.app_context.store.get(session_id)
        if record:
            self.stop_session(record)

    def stop_session(self, record: SessionRecord) -> None:
        self.app_context.backend.terminate(record)
        self.app_context.store.update(record.id, status="stopped", visible=False)
        self.hide_tab(record.id, include_pinned=True)
        self.side.selected_ids.discard(record.id)
        self.side.refresh()

    def open_master_window(self) -> None:
        if self.master_window is not None:
            try:
                if self.master_window.get_realized() and self.master_window.get_visible():
                    self.master_window.refresh()
                    self.master_window.present()
                    return
            except Exception:
                pass
            self.master_window = None
        self.master_window = MasterWindow(self.app_context)
        self.master_window.connect("destroy", lambda *_: setattr(self, "master_window", None))
        self.master_window.show_all()
        self.master_window.present()

    def open_window_overview(self) -> None:
        if self.window_overview is not None:
            try:
                if self.window_overview.get_realized() and self.window_overview.get_visible():
                    self.window_overview.present()
                    return
            except Exception:
                pass
            self.window_overview = None
        self.window_overview = WindowOverview(self.app_context)
        self.window_overview.connect("destroy", lambda *_: setattr(self, "window_overview", None))
        self.window_overview.show_all()
        self.window_overview.present()

    def open_search_window(self) -> None:
        if self.search_window is not None:
            try:
                if self.search_window.get_realized() and self.search_window.get_visible():
                    self.search_window.present()
                    return
            except Exception:
                pass
            self.search_window = None
        self.search_window = SessionSearchWindow(self.app_context)
        self.search_window.connect("destroy", lambda *_: setattr(self, "search_window", None))
        self.search_window.show_all()
        self.search_window.present()

    def open_shortcut_window(self) -> None:
        # Always rebuild after destroy so Close/Esc leave no stale reference.
        if self.shortcut_window is not None:
            try:
                if self.shortcut_window.get_realized() and self.shortcut_window.get_visible():
                    self.shortcut_window.present()
                    self.shortcut_window._redistribute_column_widths()
                    return
            except Exception:
                pass
            self.shortcut_window = None
        self.shortcut_window = ShortcutWindow(self.app_context, parent=self)
        self.shortcut_window.connect("destroy", lambda *_: setattr(self, "shortcut_window", None))
        self.shortcut_window.show_all()
        self.shortcut_window.present()
        # Match parent maximized state after map so the table fills the screen.
        if self.is_maximized():
            self.shortcut_window.maximize()
        GLib.idle_add(self.shortcut_window._redistribute_column_widths)

    def focus_side_prompt(self) -> None:
        self.side.prompt.grab_focus()

    def _pack_side_pane(self) -> None:
        for child in (self.notebook, self.side):
            try:
                if child.get_parent() is self.paned:
                    self.paned.remove(child)
            except Exception:
                pass
        if self.side_position == "left":
            self.paned.pack1(self.side, False, False)
            self.paned.pack2(self.notebook, True, False)
            self.paned.set_position(340)
        else:
            self.paned.pack1(self.notebook, True, False)
            self.paned.pack2(self.side, False, False)
            self.paned.set_position(980)
        self.paned.show_all()

    def set_side_pane_position(self, position: str, *, persist: bool = True) -> None:
        position = "left" if str(position).lower().startswith("left") else "right"
        self.side_position = position
        self._pack_side_pane()
        if persist:
            self.app_context.apply_side_pane_position(position)
        self.show_toast(f"Sessions pane moved to {position}", kind="info")

    def toggle_side(self) -> None:
        self.side.set_visible(not self.side.get_visible())

    def _renumber_visible_tabs(self) -> None:
        for index in range(self.notebook.get_n_pages()):
            widget = self.notebook.get_nth_page(index)
            for session_id, tab in self.tabs.items():
                if tab.widget is widget:
                    try:
                        self.app_context.store.update(
                            session_id,
                            tab_index=index,
                            window_id=self.window_id,
                            window_title=self.window_title,
                        )
                    except KeyError:
                        pass
                    break

    def next_tab(self) -> None:
        pages = self.notebook.get_n_pages()
        if pages:
            self.notebook.set_current_page((self.notebook.get_current_page() + 1) % pages)

    def prev_tab(self) -> None:
        pages = self.notebook.get_n_pages()
        if pages:
            self.notebook.set_current_page((self.notebook.get_current_page() - 1) % pages)

    def jump_to_tab(self, one_based: int) -> None:
        idx = one_based - 1
        if 0 <= idx < self.notebook.get_n_pages():
            self.notebook.set_current_page(idx)

    def base_font(self) -> str:
        return self.app_context.theme.font or "Monospace 12"

    def zoomed_font(self) -> str:
        return scale_font_description(self.base_font(), self.font_zoom_delta)

    def apply_font_zoom(self) -> None:
        """Push the current zoom level to every open VTE in this window."""
        font = self.zoomed_font()
        for tab in self.tabs.values():
            try:
                if self.app_context.theme.use_system_font and self.font_zoom_delta == 0:
                    # Keep system font only at identity zoom; still allow manual zoom.
                    apply_theme(tab.widget.terminal, self.app_context.theme)
                else:
                    apply_theme(tab.widget.terminal, self.app_context.theme, font_override=font)
                tab.widget.theme = self.app_context.theme
            except Exception:
                continue

    def zoom_font(self, delta: int) -> None:
        self.font_zoom_delta = max(-24, min(48, int(self.font_zoom_delta) + int(delta)))
        self.apply_font_zoom()
        _, size = parse_font_description(self.zoomed_font())
        self.show_toast(f"Font {size:g}pt", kind="info", seconds=1.2)

    def zoom_font_reset(self) -> None:
        self.font_zoom_delta = 0
        self.apply_font_zoom()
        _, size = parse_font_description(self.zoomed_font())
        self.show_toast(f"Font reset ({size:g}pt)", kind="info", seconds=1.2)

    def _is_zoom_event(self, event: Gdk.EventKey) -> str | None:
        """Return zoom_in/zoom_out/zoom_reset when event is a zoom chord, else None."""
        state = self.keymap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if not ctrl:
            return None
        # Allow Shift for keyboards where + is Shift+=.
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        # KP_Add / plus / equal (with or without shift) => zoom in
        if keyval in {Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add, Gdk.KEY_KP_Equal} or name in {
            "plus",
            "equal",
            "kp_add",
            "kp_equal",
        }:
            return "zoom_in"
        if keyval in {Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_underscore} or name in {
            "minus",
            "kp_subtract",
            "underscore",
        }:
            return "zoom_out"
        if keyval in {Gdk.KEY_0, Gdk.KEY_KP_0} or name in {"0", "kp_0"}:
            return "zoom_reset"
        return None

    def handle_zoom_event(self, event: Gdk.EventKey) -> bool:
        action = self._is_zoom_event(event)
        if action == "zoom_in":
            self.zoom_font(1)
            return True
        if action == "zoom_out":
            self.zoom_font(-1)
            return True
        if action == "zoom_reset":
            self.zoom_font_reset()
            return True
        return False

    def _current_attach_terminal(self):
        page = self.notebook.get_current_page()
        widget = self.notebook.get_nth_page(page)
        for tab in self.tabs.values():
            if tab.widget is widget:
                return tab.widget
        return None

    def _is_paste_event(self, event: Gdk.EventKey) -> bool:
        """Ctrl+Shift+V, Ctrl+V, or Shift+Insert → paste into terminal."""
        if self.keymap.matches(event, "paste"):
            return True
        state = self.keymap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        # Ctrl+V (browser-style paste into terminal)
        if ctrl and not shift and (keyval in {Gdk.KEY_v, Gdk.KEY_V} or name == "v"):
            return True
        # Shift+Insert
        if shift and not ctrl and keyval in {Gdk.KEY_Insert, Gdk.KEY_KP_Insert}:
            return True
        return False

    def _is_copy_event(self, event: Gdk.EventKey) -> bool:
        if self.keymap.matches(event, "copy"):
            return True
        state = self.keymap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        # Ctrl+Shift+C or Ctrl+C when there is a selection (don't steal shell ^C)
        if ctrl and shift and (keyval in {Gdk.KEY_c, Gdk.KEY_C} or name == "c"):
            return True
        return False

    def on_terminal_key_press(self, _terminal, event: Gdk.EventKey) -> bool:
        # Capture zoom / clipboard before VTE treats the chord as input.
        if self.handle_zoom_event(event):
            return True
        attach = self._current_attach_terminal()
        if attach is None:
            return False
        if self._is_copy_event(event):
            attach.copy_clipboard()
            return True
        if self._is_paste_event(event):
            attach.paste_clipboard()
            return True
        return False

    def on_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        key_name = Gdk.keyval_name(event.keyval) or ""
        if self.handle_zoom_event(event):
            return True
        if self.keymap.matches(event, "new_tab"):
            self.new_shell_tab()
            return True
        if self.keymap.matches(event, "new_command"):
            self.new_command_tab()
            return True
        if self.keymap.matches(event, "close_term"):
            self.hide_current_tab()
            return True
        if self.keymap.matches(event, "reopen_closed"):
            self.reopen_last_closed_tab()
            return True
        if self.keymap.matches(event, "stop_session"):
            self.stop_current_session()
            return True
        if self.keymap.matches(event, "focus_side"):
            self.focus_side_prompt()
            return True
        if self.keymap.matches(event, "launcher"):
            self.open_command_palette()
            return True
        if self.keymap.matches(event, "task_launcher"):
            self.open_launcher()
            return True
        if self.keymap.matches(event, "pause_current"):
            self.toggle_pause_current_session()
            return True
        if key_name == "F2":
            self.rename_current_session()
            return True
        if self.keymap.matches(event, "master_window") or (key_name == "Escape" and int(event.state & Gdk.ModifierType.SHIFT_MASK)):
            self.open_master_window()
            return True
        if self.keymap.matches(event, "toggle_side"):
            self.toggle_side()
            return True
        if self.keymap.matches(event, "next_tab"):
            self.next_tab()
            return True
        if self.keymap.matches(event, "prev_tab"):
            self.prev_tab()
            return True

        shortcut_index = index_from_key_name(key_name)
        if shortcut_index is not None and int(event.state & Gdk.ModifierType.MOD1_MASK):
            if int(event.state & Gdk.ModifierType.CONTROL_MASK):
                self.toggle_pause_tab(shortcut_index)
            elif not int(event.state & Gdk.ModifierType.SHIFT_MASK):
                self.jump_to_tab(shortcut_index)
            else:
                return False
            return True
        return False


class TermDeckApplication(Gtk.Application):
    def __init__(self, db_path: str | Path | None = None, workspace_path: str | Path | None = None):
        super().__init__(application_id="dev.termdeck.TermDeck", flags=0)
        self.store = SessionStore(db_path)
        self.backend = TmuxBackend()
        self.theme = load_terminator_theme()
        try:
            self.settings = AppSettings.load()
        except SettingsError:
            # Keep the GUI launchable even if the optional local config is
            # malformed; diagnostics and settings-sample expose enough detail
            # for repair without blocking terminal access.
            self.settings = AppSettings()
        # Built-in VTE packs override Terminator colours when selected; then
        # minimal appearance.terminal options (font/cursor/scrollback) win.
        self.theme = apply_terminal_palette(self.theme, self.settings.terminal_palette)
        self.theme = self.settings.apply_to_theme(self.theme)
        self.budget = HealthPolicy()
        self.sampler = ResourceSampler()
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.windows: list[TermDeckWindow] = []
        self._chrome_css = ""

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        try:
            self._chrome_css = apply_app_chrome(self.settings)
        except Exception:
            self._chrome_css = build_chrome_css(
                self.settings.chrome_theme,
                self.settings.ui_density,
                ui_font=self.settings.ui_font,
                ui_font_scale=self.settings.ui_font_scale,
            )

    def rebuild_theme(self) -> TerminalTheme:
        theme = load_terminator_theme()
        theme = apply_terminal_palette(theme, self.settings.terminal_palette)
        theme = self.settings.apply_to_theme(theme)
        self.theme = theme
        return theme

    def reapply_theme_to_open_terminals(self) -> None:
        for window in self.windows:
            window.keymap = KeyMap(
                self.theme,
                profile=self.settings.keyboard_profile,
                import_terminator=self.settings.import_terminator_keybindings,
            )
            # Keep per-window Ctrl++ zoom while refreshing palette/font base.
            window.apply_font_zoom()

    def apply_terminal_options(
        self,
        *,
        font: str,
        use_system_font: bool,
        cursor_shape: str,
        cursor_color: str | None,
        cursor_blink: bool,
        scrollback_lines: int,
        terminal_palette: str,
        ui_font: str | None = None,
        ui_font_scale: float | None = None,
        side_pane_position: str = "right",
    ) -> None:
        save_terminal_options(
            self.settings.path,
            font=font,
            use_system_font=use_system_font,
            cursor_shape=cursor_shape,
            cursor_color=cursor_color,
            cursor_blink=cursor_blink,
            scrollback_lines=scrollback_lines,
            terminal_palette=terminal_palette,
            ui_font=ui_font if ui_font is not None else "",
            ui_font_scale=ui_font_scale,
            side_pane_position=side_pane_position,
        )
        self.settings = AppSettings.load(self.settings.path)
        self.rebuild_theme()
        self.reapply_theme_to_open_terminals()
        for window in self.windows:
            window.set_side_pane_position(self.settings.side_pane_position, persist=False)
        # Reload chrome CSS so UI font/scale take effect immediately.
        try:
            self._chrome_css = apply_app_chrome(self.settings)
        except Exception:
            self._chrome_css = build_chrome_css(
                self.settings.chrome_theme,
                self.settings.ui_density,
                ui_font=self.settings.ui_font,
                ui_font_scale=self.settings.ui_font_scale,
            )

    def apply_side_pane_position(self, position: str) -> None:
        save_terminal_options(self.settings.path, side_pane_position=position)
        self.settings = AppSettings.load(self.settings.path)
        for window in self.windows:
            window.set_side_pane_position(self.settings.side_pane_position, persist=False)

    def _show_error(self, title: str, detail: str) -> None:
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def build_control_plan_for_gui(
        self,
        records: list[SessionRecord],
        action: str,
        *,
        text: str = "",
        enter: bool = False,
        selection: str = "visible selection",
    ) -> ControlPlan:
        return build_control_plan(
            records,
            action,
            text=text,
            enter=enter,
            selectors=[selection] if selection else [],
            pid_resolver=lambda record: self.backend.pane_pid(record),
            snapshot_resolver=lambda pid: self.sampler.snapshot(pid),
            policy=self.budget,
        )

    def confirm_control_plan(
        self,
        parent: Gtk.Window,
        records: list[SessionRecord],
        action: str,
        *,
        text: str = "",
        enter: bool = False,
        selection: str = "visible selection",
    ) -> bool:
        if not records:
            return False
        plan = self.build_control_plan_for_gui(
            records,
            action,
            text=text,
            enter=enter,
            selection=selection,
        )
        if not plan.requires_confirmation:
            return True

        dialog = Gtk.MessageDialog(
            transient_for=parent,
            flags=0,
            message_type=Gtk.MessageType.WARNING if plan.risk in {"high", "critical"} else Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Confirm {plan.action} for {plan.count} session(s)?",
        )
        dialog.format_secondary_text(render_control_plan_text(plan))
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _open_recipe_window(self, recipe_window: RecipeWindow, workspace_name: str, index: int) -> TermDeckWindow:
        window_id = short_id("win")
        title = recipe_window.title or f"{workspace_name} {index}"
        win = TermDeckWindow(
            self,
            window_id=window_id,
            window_title=title,
            restore_records=[],
            open_initial_shell=False,
        )
        self.windows.append(win)
        for tab_index, tab in enumerate(recipe_window.tabs):
            record = self.store.create(
                name=tab.name,
                tag=tab.tag,
                command=tab.command,
                cwd=tab.cwd,
                window_id=window_id,
                window_title=title,
                tab_index=tab_index,
                visible=tab.visible,
                pinned=tab.pinned,
            )
            self.backend.create_or_resume(record)
            pid = self.backend.pane_pid(record)
            record = self.store.update(record.id, status="running", last_pid=pid, visible=tab.visible)
            if tab.visible:
                win.open_session(record, restart_missing=True, remember_layout=False)
        win.side.refresh()
        win.show_all()
        return win

    def open_workspace_recipe(self, path: str | Path) -> int:
        recipe = load_recipe(path)
        opened = 0
        for index, recipe_window in enumerate(recipe.windows, start=1):
            self._open_recipe_window(recipe_window, recipe.name, index)
            opened += 1
        return opened

    def do_activate(self) -> None:
        if not self.backend.available:
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="tmux is required",
            )
            dialog.format_secondary_text("Install tmux, then run TermDeck again. On Debian/Ubuntu: sudo apt install tmux")
            dialog.run()
            dialog.destroy()
            return
        # Repair stale registry state before browser-style restore so lost
        # backend sessions do not reappear as live tabs after a crash/reboot.
        reconcile_store(self.store, backend=self.backend, sampler=self.sampler, hide_missing=True)

        if not self.windows and self.workspace_path is not None:
            try:
                self.open_workspace_recipe(self.workspace_path)
            except WorkspaceRecipeError as exc:
                self._show_error("Could not open workspace recipe", str(exc))
            if self.windows:
                self.windows[-1].present()
                return

        if not self.windows:
            groups = self.store.windows_with_visible_sessions()
            if groups:
                for window_id, window_title, records in groups:
                    win = TermDeckWindow(self, window_id=window_id, window_title=window_title, restore_records=records)
                    self.windows.append(win)
                    win.show_all()
            else:
                win = TermDeckWindow(self, window_id="main", window_title="Main")
                self.windows.append(win)
        self.windows[-1].present()


def run(db_path: str | Path | None = None, workspace_path: str | Path | None = None) -> int:
    app = TermDeckApplication(db_path=db_path, workspace_path=workspace_path)
    return app.run(sys.argv)
