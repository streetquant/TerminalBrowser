from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import gi  # type: ignore

# Pin GTK3 stack versions before any repository import. On systems that ship both
# Gdk 3.0 and 4.0, an unversioned Gdk import defaults to 4.0 and then breaks Gtk 3.0.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # type: ignore  # noqa: E402

from .capacity import (
    MASTER_ROW_LIMIT,
    SIDE_PANE_ROW_LIMIT,
    UI_RESOURCE_REFRESH_BUDGET,
    bounded_registry_records,
    bounded_startup_groups,
)
from .command_palette import PaletteItem, build_palette_items, search_palette
from .control_plan import ControlPlan, build_control_plan, render_control_plan_text
from .health import HealthPolicy, classify_session, health_badge, should_pause_for_attention
from .master_tasks import filter_rows
from .models import SessionRecord, TerminalTheme
from .redaction import redact_text
from .resources import ResourceSampler
from .session_lifecycle import archive_records, hide_records, move_records, pin_records, recently_closed_records, rename_record, reopen_closed_records, unpin_records
from .session_reconcile import reconcile_store
from .session_search import search_sessions
from .session_store import SessionStore, short_id, utc_now
from .settings import AppSettings, SettingsError, save_terminal_options
from .shortcuts import build_shortcut_map, index_from_key_name, shortcut_entries
from .task_snapshot import build_task_snapshot
from .command_render import default_shell, directory_tab_name, normalise_directory, render_command
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


def _icon_button(label, icon_name, tooltip, handler, *, suggested=False, show_label=True):
    button = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4 if show_label else 0)
    # Keep icon/label optically centred even when CSS enforces a larger
    # square hit target than the natural content size.
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    try:
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        box.pack_start(image, False, False, 0)
    except Exception:
        pass
    if show_label:
        box.pack_start(Gtk.Label(label=label), False, False, 0)
    button.add(box)
    button.set_tooltip_text(tooltip)
    try:
        button.get_accessible().set_name(label)
    except Exception:
        pass
    if suggested:
        button.get_style_context().add_class("suggested-action")
        button.get_style_context().add_class("td-accent")
    button.connect("clicked", lambda *_: handler())
    return button


def _wire_auxiliary_close(window: Gtk.Window) -> None:
    # Make helper windows closable even on minimal/tiled window managers.
    try:
        window.set_deletable(True)
    except Exception:
        pass

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


SIDE_PANE_HIDDEN_STATUSES = {"archived", "dead", "killed", "missing", "stopped", "terminated"}
KILLED_HISTORY_STATUSES = {"dead", "killed", "stopped", "terminated"}


def _partition_master_rows(records, rows):
    """Separate durable killed-tab history from currently actionable tabs."""

    history_ids = {
        record.id
        for record in records
        if record.status.casefold() in KILLED_HISTORY_STATUSES
    }
    return (
        [row for row in rows if row.session_id not in history_ids],
        [row for row in rows if row.session_id in history_ids],
    )


def _show_in_sessions_pane(record: SessionRecord) -> bool:
    return bool(record.visible) and record.status.casefold() not in SIDE_PANE_HIDDEN_STATUSES


def _tabs_pane_records(store, window_id: str, query: str = "") -> list[SessionRecord]:
    """Return registry rows that belong in one window's Tabs pane.

    The shared SQLite store holds every named window. The side pane must only
    list tabs for its own ``window_id``; otherwise every window's pane shows
    every other window's tabs.
    """
    needle = (query or "").lower().strip()
    return [
        record
        for record in store.list(window_id=window_id)
        if _show_in_sessions_pane(record)
        and (
            not needle
            or needle in f"{record.name} {record.tag} {record.id} {record.command}".lower()
        )
    ]


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
        live_focus_handler=None,
        scroll_handler=None,
        scrollback_extent: int | None = None,
        font_override: str | None = None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.argv = argv
        self.cwd = cwd
        self.theme = theme
        self.process: subprocess.Popen[bytes] | None = None
        self.pty_obj = None
        self._detached = False
        self._spawn_source_id = 0
        self._spawn_fallback_source_id = 0
        self._resize_sync_source_id = 0
        self._cursor_hide_source_id = 0
        self._last_allocated_grid = (0, 0)
        self._last_synced_grid = (0, 0)
        self._live_focus_handler = None
        self._scroll_handler = None
        self._key_press_handler_id = 0
        self._history_cursor_hidden = False
        self.terminal = Vte.Terminal()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        apply_theme(self.terminal, theme, font_override=font_override)
        # Match Terminator's terminal layout: VTE and a persistent scrollbar
        # share one row, with the bar representing the history behind tmux.
        self.terminal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.terminal_row.set_hexpand(True)
        self.terminal_row.set_vexpand(True)
        # VTE sees the tmux client in its alternate screen, whose adjustment has
        # no history range and therefore no drawable thumb. Keep a small proxy
        # adjustment for the history owner behind tmux instead.
        extent = float(max(100, int(scrollback_extent or theme.scrollback_lines)))
        page_size = min(24.0, extent - 1.0)
        self.scroll_adjustment = Gtk.Adjustment.new(
            extent - page_size,
            0.0,
            extent,
            3.0,
            page_size,
            page_size,
        )
        self._scrollbar_syncing = False
        self._last_scrollbar_value = float(self.scroll_adjustment.get_value())
        self.scrollbar = Gtk.Scrollbar.new(
            Gtk.Orientation.VERTICAL,
            adjustment=self.scroll_adjustment,
        )
        _style(self.scrollbar, "td-terminal-scrollbar")
        self.scrollbar.get_style_context().remove_class("overlay-indicator")
        self.scrollbar.set_size_request(16, -1)
        self.scrollbar.set_hexpand(False)
        self.scrollbar.set_vexpand(True)
        self.scrollbar.set_opacity(1.0)
        self.scroll_adjustment.connect(
            "value-changed",
            self._on_scrollbar_value_changed,
        )
        # Do not let VTE translate wheel motion into Up/Down keypresses for an
        # alternate-screen application. In Codex that cycles prompt history
        # instead of moving through terminal content.
        try:
            self.terminal.set_enable_fallback_scrolling(False)
        except Exception:
            pass
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_scroll_on_keystroke(True)
        # VTE keeps focus for typing; connect here so Ctrl++/- zoom is not swallowed.
        self.rebind_window_handlers(
            key_press_handler=key_press_handler,
            live_focus_handler=live_focus_handler,
            scroll_handler=scroll_handler,
        )
        # Select-to-copy: run *after* VTE handles the release so the selection exists.
        self.terminal.connect_after("button-release-event", self._on_button_release)
        self.terminal.connect("selection-changed", self._on_selection_changed)
        self.terminal.connect("button-press-event", self._on_button_press)
        self.terminal.connect("scroll-event", self._on_terminal_scroll)
        self.terminal.connect("size-allocate", self._on_terminal_size_allocate)
        self.connect("destroy", lambda *_: self._cancel_pending_spawn())
        self._selection_pending = False
        self._selection_autocopy_source_id = 0
        # Last select-to-copy payload — used when pasting back into this terminal
        # (VTE paste_* can miss text we put on the clipboard ourselves).
        self._last_copied_text = ""
        self.terminal_row.pack_start(self.terminal, True, True, 0)
        self.terminal_row.pack_start(self.scrollbar, False, True, 0)
        self.pack_start(self.terminal_row, True, True, 0)
        # tmux renders dotted padding when a client attaches at the PTY default
        # 80x24 and the GTK window is resized immediately afterwards. Debounce
        # the initial allocation so tmux receives the final VTE grid from frame 1.
        self._spawn_fallback_source_id = GLib.timeout_add(500, self._spawn_fallback)

    def rebind_window_handlers(
        self,
        *,
        key_press_handler,
        live_focus_handler,
        scroll_handler=None,
    ) -> None:
        """Retarget window-owned callbacks when this live widget is reparented."""

        if self._key_press_handler_id:
            try:
                self.terminal.disconnect(self._key_press_handler_id)
            except Exception:
                pass
            self._key_press_handler_id = 0
        if callable(key_press_handler):
            self._key_press_handler_id = self.terminal.connect(
                "key-press-event",
                key_press_handler,
            )
        self._live_focus_handler = live_focus_handler
        self._scroll_handler = scroll_handler

    def apply_window_theme(self, theme: TerminalTheme, *, font_override: str) -> None:
        self.theme = theme
        apply_theme(self.terminal, theme, font_override=font_override)
        if self._history_cursor_hidden:
            self._set_history_cursor_hidden(True)

    def _set_history_cursor_hidden(self, hidden: bool) -> None:
        """Hide a pager's roaming caret while preserving the live cursor style."""

        self._remove_glib_source(self._cursor_hide_source_id)
        self._cursor_hide_source_id = 0
        self._history_cursor_hidden = bool(hidden)
        self.terminal.feed(b"\x1b[?25l" if self._history_cursor_hidden else b"\x1b[?25h")
        # tmux/Codex redraws can re-enable the DEC cursor mid-review, so also
        # recolour the caret into the background while history is open. Colour
        # is emulator-side state the child never rewrites, so it cannot race.
        try:
            if self._history_cursor_hidden:
                self.terminal.set_color_cursor(rgba(self.theme.background_color, "#000000"))
                self.terminal.set_color_cursor_foreground(rgba(self.theme.foreground_color, "#d3d7cf"))
            else:
                self.terminal.set_color_cursor(
                    rgba(self.theme.cursor_color) if self.theme.cursor_color else None
                )
                self.terminal.set_color_cursor_foreground(None)
        except Exception:
            pass
        if self._history_cursor_hidden:
            # The tmux/Codex redraw triggered by the scroll command arrives on
            # the PTY asynchronously. Reassert once after that frame so its
            # pager caret cannot flash back into the reviewed content.
            self._cursor_hide_source_id = GLib.timeout_add(
                40,
                self._confirm_history_cursor_hidden,
            )

    def _confirm_history_cursor_hidden(self) -> bool:
        self._cursor_hide_source_id = 0
        if self._history_cursor_hidden and not self._detached:
            self.terminal.feed(b"\x1b[?25l")
        return False

    @staticmethod
    def _remove_glib_source(source_id: int) -> None:
        if not source_id:
            return
        try:
            GLib.source_remove(source_id)
        except Exception:
            pass

    def _cancel_pending_spawn(self) -> None:
        self._remove_glib_source(self._spawn_source_id)
        self._remove_glib_source(self._spawn_fallback_source_id)
        self._remove_glib_source(self._resize_sync_source_id)
        self._remove_glib_source(self._cursor_hide_source_id)
        self._spawn_source_id = 0
        self._spawn_fallback_source_id = 0
        self._resize_sync_source_id = 0
        self._cursor_hide_source_id = 0

    def _on_terminal_size_allocate(self, _terminal, _allocation) -> None:
        columns = max(0, int(self.terminal.get_column_count()))
        rows = max(0, int(self.terminal.get_row_count()))
        self._last_allocated_grid = (columns, rows)
        if self.process is not None:
            self._sync_pty_geometry(columns=columns, rows=rows)
            # VTE updates its character grid as part of allocation. Read it
            # again after GTK finishes the frame so a Paned/window expansion
            # cannot leave tmux at the previous, smaller grid.
            self.request_geometry_sync()
            return
        if self._detached or columns < 2 or rows < 2:
            return
        self._remove_glib_source(self._spawn_source_id)
        self._spawn_source_id = GLib.timeout_add(60, self._spawn_after_stable_allocation)

    def _spawn_after_stable_allocation(self) -> bool:
        self._spawn_source_id = 0
        if self.process is not None or self._detached:
            return False
        columns, rows = self._last_allocated_grid
        if columns < 2 or rows < 2:
            return False
        self._spawn(columns=columns, rows=rows)
        return False

    def request_geometry_sync(self, *, delay_ms: int = 45) -> None:
        if self._detached:
            return
        self._remove_glib_source(self._resize_sync_source_id)
        self._resize_sync_source_id = GLib.timeout_add(
            max(0, int(delay_ms)),
            self._sync_after_stable_geometry,
        )

    def _sync_after_stable_geometry(self) -> bool:
        self._resize_sync_source_id = 0
        if self._detached or self.process is None:
            return False
        columns = max(0, int(self.terminal.get_column_count()))
        rows = max(0, int(self.terminal.get_row_count()))
        self._last_allocated_grid = (columns, rows)
        self._sync_pty_geometry(columns=columns, rows=rows, force=True)
        return False

    def _spawn_fallback(self) -> bool:
        self._spawn_fallback_source_id = 0
        if self.process is not None or self._detached:
            return False
        columns = max(2, int(self.terminal.get_column_count() or 80))
        rows = max(2, int(self.terminal.get_row_count() or 24))
        self._spawn(columns=columns, rows=rows)
        return False

    def _sync_pty_geometry(self, *, columns: int, rows: int, force: bool = False) -> None:
        if columns < 2 or rows < 2 or self.pty_obj is None:
            return
        grid = (columns, rows)
        if grid == self._last_synced_grid and not force:
            return
        try:
            self.pty_obj.set_size(rows, columns)
            self._last_synced_grid = grid
        except Exception:
            return
        # VTE may already have applied the same kernel winsize by the time a
        # debounced layout callback runs. An explicit SIGWINCH makes the tmux
        # client re-read it even when a repeated TIOCSWINSZ is a no-op.
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

    def _spawn(self, *, columns: int, rows: int) -> None:
        if self.process is not None or self._detached:
            return
        self._cancel_pending_spawn()
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        winsize = struct.pack("HHHH", rows, columns, 0, 0)
        for fd in (master_fd, slave_fd):
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass
        try:
            self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)
            self._last_synced_grid = (0, 0)
            self._sync_pty_geometry(columns=columns, rows=rows)
            self.terminal.set_pty(self.pty_obj)
            self.process = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
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
        self._detached = True
        self._cancel_pending_spawn()
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

    def scroll_to_bottom(self) -> None:
        """Return the VTE viewport to the live terminal screen."""

        self.reset_scrollbar()
        try:
            adjustment = self.terminal.get_vadjustment()
            bottom = max(
                adjustment.get_lower(),
                adjustment.get_upper() - adjustment.get_page_size(),
            )
            adjustment.set_value(bottom)
        except Exception:
            pass

    def _set_scrollbar_value(self, value: float) -> None:
        adjustment = self.scroll_adjustment
        lower = float(adjustment.get_lower())
        upper = max(
            lower,
            float(adjustment.get_upper()) - float(adjustment.get_page_size()),
        )
        self._scrollbar_syncing = True
        try:
            adjustment.set_value(min(upper, max(lower, float(value))))
            self._last_scrollbar_value = float(adjustment.get_value())
        finally:
            self._scrollbar_syncing = False

    def _move_scrollbar(self, lines: int) -> None:
        self._set_scrollbar_value(self._last_scrollbar_value + int(lines))

    def reset_scrollbar(self) -> None:
        adjustment = self.scroll_adjustment
        self._set_scrollbar_value(
            float(adjustment.get_upper()) - float(adjustment.get_page_size())
        )

    def _scrollbar_at_live_edge(self) -> bool:
        """True when the proxy scrollbar sits at the live (bottom) edge."""

        adjustment = self.scroll_adjustment
        bottom = float(adjustment.get_upper()) - float(adjustment.get_page_size())
        return float(adjustment.get_value()) >= bottom - 0.5

    def _on_scrollbar_value_changed(self, adjustment) -> None:
        if self._scrollbar_syncing:
            return
        previous = float(self._last_scrollbar_value)
        requested = float(adjustment.get_value())
        delta = int(round(requested - previous))
        if not delta:
            return
        handler = self._scroll_handler
        try:
            if callable(handler) and handler(delta):
                self._last_scrollbar_value = requested
                if delta > 0 and self._scrollbar_at_live_edge():
                    # Dragged back to the live edge — leave the history view.
                    self.prepare_for_live_input()
                else:
                    self._set_history_cursor_hidden(True)
                return
        except Exception:
            pass
        self._set_scrollbar_value(previous)

    def _feed_text(self, text: str) -> bool:
        """Paste text through VTE so bracketed-paste mode can prevent submit."""
        if not text:
            return False
        # Avoid pasting the active highlight into itself weirdly — clear selection.
        try:
            if self.terminal.get_has_selection():
                self.terminal.unselect_all()
        except Exception:
            pass
        clean_text = text if isinstance(text, str) else bytes(text).decode("utf-8", errors="replace")
        try:
            # Unlike feed_child(), paste_text() applies VTE's clipboard
            # processing, including bracketed paste when the child enables it.
            self.terminal.paste_text(clean_text)
            return True
        except Exception:
            pass
        data = clean_text.encode("utf-8")
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

    @staticmethod
    def _key_event_bytes(event: Gdk.EventKey) -> bytes | None:
        """Encode one GTK key press for direct delivery to the child PTY."""

        keyval = int(getattr(event, "keyval", 0) or 0)
        state = int(getattr(event, "state", 0) or 0)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        modifier = 1 + int(shift) + (2 * int(alt)) + (4 * int(ctrl))

        simple = {
            Gdk.KEY_Return: b"\r",
            Gdk.KEY_KP_Enter: b"\r",
            Gdk.KEY_Tab: b"\t",
            Gdk.KEY_BackSpace: b"\x7f",
            Gdk.KEY_Escape: b"\x1b",
        }
        if keyval == Gdk.KEY_ISO_Left_Tab:
            return b"\x1b[Z"
        if keyval in simple:
            return simple[keyval]

        final_keys = {
            Gdk.KEY_Up: "A",
            Gdk.KEY_Down: "B",
            Gdk.KEY_Right: "C",
            Gdk.KEY_Left: "D",
            Gdk.KEY_Home: "H",
            Gdk.KEY_End: "F",
        }
        if keyval in final_keys:
            final = final_keys[keyval]
            return (f"\x1b[{final}" if modifier == 1 else f"\x1b[1;{modifier}{final}").encode()

        tilde_keys = {
            Gdk.KEY_Insert: 2,
            Gdk.KEY_Delete: 3,
            Gdk.KEY_Page_Up: 5,
            Gdk.KEY_Page_Down: 6,
            Gdk.KEY_F5: 15,
            Gdk.KEY_F6: 17,
            Gdk.KEY_F7: 18,
            Gdk.KEY_F8: 19,
            Gdk.KEY_F9: 20,
            Gdk.KEY_F10: 21,
            Gdk.KEY_F11: 23,
            Gdk.KEY_F12: 24,
        }
        if keyval in tilde_keys:
            number = tilde_keys[keyval]
            return (f"\x1b[{number}~" if modifier == 1 else f"\x1b[{number};{modifier}~").encode()

        function_keys = {
            Gdk.KEY_F1: "P",
            Gdk.KEY_F2: "Q",
            Gdk.KEY_F3: "R",
            Gdk.KEY_F4: "S",
        }
        if keyval in function_keys:
            final = function_keys[keyval]
            return (f"\x1bO{final}" if modifier == 1 else f"\x1b[1;{modifier}{final}").encode()

        codepoint = int(Gdk.keyval_to_unicode(keyval) or 0)
        if not codepoint:
            return None
        character = chr(codepoint)
        if ctrl:
            lowered = character.casefold()
            if "a" <= lowered <= "z":
                data = bytes([ord(lowered) - ord("a") + 1])
            elif character in {" ", "@", "2"}:
                data = b"\x00"
            elif character in {"[", "3"}:
                data = b"\x1b"
            elif character in {"\\", "4"}:
                data = b"\x1c"
            elif character in {"]", "5"}:
                data = b"\x1d"
            elif character in {"^", "6"}:
                data = b"\x1e"
            elif character in {"_", "7", "/"}:
                data = b"\x1f"
            elif character in {"?", "8"}:
                data = b"\x7f"
            else:
                return None
        else:
            data = character.encode("utf-8")
        return (b"\x1b" + data) if alt else data

    def feed_key_event(self, event: Gdk.EventKey) -> bool:
        """Deliver the first key pressed while another window control had focus."""

        data = self._key_event_bytes(event)
        if not data:
            return False
        try:
            if self.pty_obj is not None:
                os.write(self.pty_obj.get_fd(), data)
                return True
        except (AttributeError, BlockingIOError, OSError):
            pass
        try:
            if hasattr(self.terminal, "feed_child_binary"):
                self.terminal.feed_child_binary(data)
            else:
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

    def _paste_from_selection(self, selection, *, prefer_last: bool = False) -> bool:
        """Request clipboard text without blocking GTK's main loop."""

        def _normalise(text) -> str:
            if isinstance(text, (bytes, bytearray)):
                return text.decode("utf-8", errors="replace")
            return text if isinstance(text, str) else ""

        def _vte_fallback() -> None:
            if getattr(self, "_detached", False):
                return
            try:
                if selection == Gdk.SELECTION_PRIMARY:
                    self.terminal.paste_primary()
                else:
                    self.terminal.paste_clipboard()
            except Exception:
                pass

        def _finish(text, *, allow_clipboard_fallback: bool) -> None:
            if getattr(self, "_detached", False):
                return
            clean_text = _normalise(text)
            if clean_text:
                self._feed_text(clean_text)
                return
            if prefer_last and self._last_copied_text:
                self._feed_text(self._last_copied_text)
                return
            if allow_clipboard_fallback and selection != Gdk.SELECTION_CLIPBOARD:
                if _request(Gdk.SELECTION_CLIPBOARD, lambda fallback: _finish(fallback, allow_clipboard_fallback=False)):
                    return
            _vte_fallback()

        def _request(requested_selection, callback) -> bool:
            clipboard = self._system_clipboard(requested_selection)
            if clipboard is None:
                return False

            def _received(_clipboard, text, *_args) -> None:
                callback(text)

            try:
                clipboard.request_text(_received, None)
                return True
            except TypeError:
                try:
                    clipboard.request_text(_received)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

        if _request(selection, lambda text: _finish(text, allow_clipboard_fallback=True)):
            return True
        _finish(None, allow_clipboard_fallback=True)
        return True

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
        # Let VTE keep PRIMARY ownership so the selected range stays visibly
        # highlighted. Reclaiming PRIMARY through Gtk.Clipboard.set_text()
        # immediately clears VTE's selection. CLIPBOARD is separate, so keep a
        # durable plain-text owner there for paste after the highlight clears.
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
        self._set_clipboard_text(text, Gdk.SELECTION_CLIPBOARD)
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
        if button == 1:
            # Let VTE see the complete press/drag/release gesture first. Live
            # viewport restoration happens on release only when no selection
            # was created, so ordinary drag-to-copy does not get cancelled.
            return False
        # Middle-click paste from PRIMARY / last selection (classic terminal).
        if button == 2:
            self.paste_primary()
            return True
        if button == 3:
            self._show_terminal_context_menu(event)
            return True
        return False

    def _on_terminal_scroll(self, terminal, event: Gdk.EventScroll) -> bool:
        """Scroll the owning session without synthesizing composer key input."""

        direction = getattr(event, "direction", None)
        if direction not in {
            Gdk.ScrollDirection.UP,
            Gdk.ScrollDirection.DOWN,
            Gdk.ScrollDirection.SMOOTH,
        }:
            return False
        if direction == Gdk.ScrollDirection.UP:
            delta = -3.0
        elif direction == Gdk.ScrollDirection.DOWN:
            delta = 3.0
        else:
            delta = float(getattr(event, "delta_y", 0.0) or 0.0)
        handler = getattr(self, "_scroll_handler", None)
        if callable(handler):
            line_delta = int(round(delta))
            if not line_delta and delta:
                line_delta = -1 if delta < 0 else 1
            try:
                if line_delta and handler(line_delta):
                    mover = getattr(self, "_move_scrollbar", None)
                    if callable(mover):
                        mover(line_delta)
                    if line_delta > 0 and self._scrollbar_at_live_edge():
                        # Scrolled back down to the live edge — return to the
                        # live screen instead of leaving the history pager
                        # frozen until the next keypress.
                        self.prepare_for_live_input()
                    else:
                        self._set_history_cursor_hidden(True)
                    return True
            except Exception:
                pass
        adjustment = terminal.get_vadjustment()
        step = max(1.0, float(adjustment.get_step_increment() or 1.0))
        lower = float(adjustment.get_lower())
        upper = max(
            lower,
            float(adjustment.get_upper()) - float(adjustment.get_page_size()),
        )
        value = float(adjustment.get_value()) + (delta * step)
        adjustment.set_value(min(upper, max(lower, value)))
        return True

    def prepare_for_live_input(self) -> None:
        self.reset_scrollbar()
        handler = self._live_focus_handler
        if callable(handler):
            try:
                handler()
            except Exception:
                pass
        self._set_history_cursor_hidden(False)
        self.request_geometry_sync(delay_ms=0)

    def _on_button_release(self, _terminal, event: Gdk.EventButton) -> bool:
        # Left-button release after a drag/select → immediate auto-copy.  This
        # keeps the browser-like feel while the selection-changed debounce covers
        # keyboard/double-click paths that may not deliver a useful release edge.
        button = int(getattr(event, "button", 0) or 0)
        if button == 1:
            try:
                has_selection = bool(self.terminal.get_has_selection())
            except Exception:
                has_selection = False
            if has_selection:
                source_id = getattr(self, "_selection_autocopy_source_id", 0)
                if source_id:
                    try:
                        GLib.source_remove(source_id)
                    except Exception:
                        pass
                    self._selection_autocopy_source_id = 0
                # Idle so VTE finishes updating the selection buffer first.
                GLib.idle_add(self._copy_selection_to_clipboards_idle)
            elif not (
                int(getattr(event, "state", 0) or 0)
                & Gdk.ModifierType.SHIFT_MASK
            ):
                self.prepare_for_live_input()
                self.scroll_to_bottom()
                self.terminal.grab_focus()
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
        label = Gtk.Label(label="One summary row per named window: tab counts, aggregate resource load, and health")
        _style(label, "td-section-description")
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
            snapshot_refresh_limit=UI_RESOURCE_REFRESH_BUDGET,
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

    COLUMNS = ["ID", "Window", "Name", "Status", "Fields", "Output", "Command"]

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
            label="Typo-tolerant search across tab names, commands, directories, windows, status, and optional recent output.",
            xalign=0,
        )
        label.set_line_wrap(True)
        outer.pack_start(label, False, False, 0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.query = Gtk.SearchEntry(placeholder_text="search tabs, including small typos: pyetst, api server, build error")
        _connect_debounced_refresh(self.query, self.refresh)
        self.query.connect("activate", lambda *_: self.open_selected())
        top.pack_start(self.query, True, True, 0)
        self.include_output = Gtk.CheckButton(label="include recent output")
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
        self.set_preview_text("Type to search tabs. Small typos are tolerated. Enable recent output to search the latest 120 terminal lines.")
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
                match.status,
                ",".join(field for field in match.matched_fields if field != "tag") or "-",
                str(match.output_match_count) if match.output_match_count else "-",
                match.command,
            ])
        if not query:
            self.set_preview_text("Type to search tabs. Small typos are tolerated. Enable recent output to search the latest 120 terminal lines.")
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
            f"{match.name}",
            f"ID: {match.session_id}",
            f"Window: {match.window_title}",
            f"Status: {match.status}",
            f"Directory: {match.cwd}",
            f"Command: {match.command}",
            f"Matched fields: {', '.join(field for field in match.matched_fields if field != 'tag') or '-'}",
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


class WindowNameDialog(Gtk.Dialog):
    """Small window-level naming dialog; tabs are created separately."""

    def __init__(
        self,
        parent: Gtk.Window | None,
        *,
        title: str,
        initial: str = "",
        confirm_label: str = "Save",
        description: str = "",
    ):
        super().__init__(title=title, transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, confirm_label, Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_modal(True)
        self.set_default_size(420, -1)
        _style(self, "td-overlay-dialog")
        content = self.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=14)
        content.add(box)
        if description:
            intro = Gtk.Label(label=description, xalign=0)
            intro.set_line_wrap(True)
            _style(intro, "td-muted")
            box.pack_start(intro, False, False, 0)
        label = Gtk.Label(label="Window name", xalign=0)
        _style(label, "td-muted")
        box.pack_start(label, False, False, 0)
        self.entry = Gtk.Entry(text=initial)
        self.entry.set_activates_default(True)
        self.entry.set_placeholder_text("e.g. Client work, Backend, Research")
        box.pack_start(self.entry, False, False, 0)
        self.entry.select_region(0, -1)
        self.show_all()

    def value(self) -> str:
        return self.entry.get_text().strip()


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
            ("Tabs pane", self.side_position_combo),
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
    """Universal command palette for window actions and remembered tabs."""

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
            label="Search window actions and remembered tabs. Pick a result and press Enter.",
            xalign=0,
        )
        intro.set_line_wrap(True)
        _style(intro, "td-muted")
        outer.pack_start(intro, False, False, 0)

        self.search = Gtk.SearchEntry(placeholder_text="try: new tab, rename window, search tabs, pause selected")
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
        self._visible_record_count = 0
        self._shown_record_count = 0
        self._refresh_alive = True
        self.connect("destroy", lambda *_: setattr(self, "_refresh_alive", False))
        self._last_refresh_signature = None
        # Rebuild dynamic CPU/RAM/health rows periodically even when session
        # metadata is unchanged; duplicate refreshes within the same bucket are
        # skipped to keep the UI responsive.
        self._refresh_bucket_seconds = max(10.0, float(getattr(window.app_context.settings, "ui_refresh_interval_seconds", 5.0)) * 3.0)
        _style(self, "td-side-pane")
        density = normalise_density(getattr(window.app_context.settings, "ui_density", "comfortable"))
        if density == "compact":
            _style(self, "td-density-compact")

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heading_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        eyebrow = Gtk.Label(label="WINDOW", xalign=0)
        _style(eyebrow, "td-eyebrow")
        title = Gtk.Label(label="Tabs", xalign=0)
        _style(title, "title", "td-title")
        heading_stack.pack_start(eyebrow, False, False, 0)
        heading_stack.pack_start(title, False, False, 0)
        title_event = Gtk.EventBox()
        title_event.add(heading_stack)
        title_event.set_tooltip_text("Right-click to move the tabs pane left or right")
        title_event.connect("button-press-event", self._on_header_button_press)
        header_row.pack_start(title_event, True, True, 0)
        self.count_label = Gtk.Label(label="0 tabs")
        _style(self.count_label, "td-count-pill")
        header_row.pack_end(self.count_label, False, False, 0)

        pane_menu = Gtk.Menu()
        for label, handler in [
            ("Move pane left", lambda: self.window.set_side_pane_position("left")),
            ("Move pane right", lambda: self.window.set_side_pane_position("right")),
            ("Hide tabs pane", self.window.toggle_tabs_pane),
            ("Terminal appearance", self.window.open_terminal_options),
        ]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, h=handler: h())
            pane_menu.append(item)
        pane_menu.show_all()
        pane_more = Gtk.MenuButton(label="⋯")
        pane_more.set_tooltip_text("Tabs pane options")
        pane_more.set_popup(pane_menu)
        _style(pane_more, "td-ghost-action")
        header_row.pack_end(pane_more, False, False, 0)
        self.pack_start(header_row, False, False, 0)

        hint = Gtk.Label(label="Ctrl+left-click any tab card to select multiple tabs", xalign=0)
        hint.set_line_wrap(True)
        _style(hint, "td-muted")
        self.pack_start(hint, False, False, 0)

        self.filter_entry = Gtk.SearchEntry(placeholder_text="Search tabs, directories, or commands")
        _style(self.filter_entry, "td-search-entry")
        _connect_debounced_refresh(self.filter_entry, self.refresh)
        self.pack_start(self.filter_entry, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.session_box = Gtk.ListBox()
        self.session_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.session_box.set_activate_on_single_click(True)
        self.session_box.connect("row-activated", self._on_session_row_activated)
        scroller.add(self.session_box)
        self.pack_start(scroller, True, True, 0)

        self.prompt = Gtk.Entry(placeholder_text="Send a command or prompt to selected tabs")
        _style(self.prompt, "td-search-entry")
        self.pack_start(self.prompt, False, False, 0)

        # Primary bulk actions remain visible with clear semantic emphasis.
        primary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        _style(primary, "td-bottom-bar")
        for label, handler, style_class in [
            ("Send", self.send_prompt, "td-primary-action"),
            ("Pause", self.pause_selected, "td-ghost-action"),
            ("Resume", self.resume_selected, "td-ghost-action"),
            ("Kill", self.kill_selected, "td-danger-action"),
        ]:
            button = Gtk.Button(label=label)
            _style(button, style_class)
            button.connect("clicked", lambda _b, h=handler: h())
            primary.pack_start(button, True, True, 0)
        self.pack_start(primary, False, False, 0)

        more = Gtk.MenuButton(label="More actions ▾")
        more.set_tooltip_text("Secondary tab controls")
        _style(more, "td-more-actions")
        menu = Gtk.Menu()
        secondary = [
            ("New tab", self.window.new_tab),
            ("Rename window", self.window.rename_window),
            ("Palette", self.window.open_command_palette),
            ("Rename", self.rename_selected),
            ("Terminal options", self.window.open_terminal_options),
            ("Status", self.send_status),
            ("Pin", self.pin_selected),
            ("Unpin", self.unpin_selected),
            ("Hide", self.hide_selected),
            ("Archive", self.archive_selected),
            ("Reopen", self.window.reopen_last_closed_tab),
            ("Pause hot", self.pause_hot_sessions),
            ("Restore", self.window.restore_previous_tabs),
            ("Tab manager", self.window.open_master_window),
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
        menu.append(self._menu_item("Move tabs pane to left", lambda: self.window.set_side_pane_position("left")))
        menu.append(self._menu_item("Move tabs pane to right", lambda: self.window.set_side_pane_position("right")))
        menu.append(self._menu_item("Hide tabs pane", self.window.toggle_tabs_pane))
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

    def _on_session_button_press(self, _widget, event: Gdk.EventButton, record: SessionRecord, check: Gtk.CheckButton) -> bool:
        button = int(getattr(event, "button", 0) or 0)
        state = int(getattr(event, "state", 0) or 0)
        if button == 1 and state & Gdk.ModifierType.CONTROL_MASK:
            self._toggle_record_selection(record, check)
            return True
        if button == 3:
            self._show_session_menu(record, check, event)
            return True
        return False

    def _on_session_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        record = getattr(row, "_termdeck_session_record", None)
        if record is not None:
            self.window.open_session(record)

    def _on_session_card_press(
        self,
        card_event: Gtk.EventBox,
        event: Gdk.EventButton,
        record: SessionRecord,
        check: Gtk.CheckButton,
    ) -> bool:
        if self._on_session_button_press(card_event, event, record, check):
            return True
        if int(getattr(event, "button", 0) or 0) != 1:
            return False

        # Buttons and the checkbox own their click. Some GTK themes propagate
        # their press to the containing EventBox before emitting ``clicked``;
        # do not turn Rename, Open, or selection into an extra navigation.
        try:
            source = Gtk.get_event_widget(event)
        except Exception:
            source = None
        while source is not None and source is not card_event:
            if isinstance(source, Gtk.Button):
                return False
            try:
                source = source.get_parent()
            except Exception:
                break

        self.window.open_session(record)
        return True

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
        menu.append(self._menu_item(self._context_label("Pin", records), lambda: self._pin_records(records)))
        menu.append(self._menu_item(self._context_label("Unpin", records), lambda: self._unpin_records(records)))
        menu.append(self._menu_item(self._context_label("Hide", records), lambda: self._hide_records(records)))
        menu.append(self._menu_item(self._context_label("Kill", records), lambda: self._kill_records(records)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._menu_item("Move tabs pane to left", lambda: self.window.set_side_pane_position("left")))
        menu.append(self._menu_item("Move tabs pane to right", lambda: self.window.set_side_pane_position("right")))
        self._popup_menu(menu, event)

    def _kill_records(self, records: list[SessionRecord]) -> None:
        if not self.window.app_context.confirm_control_plan(
            self.window,
            records,
            "kill",
            selection="side pane context menu",
        ):
            return
        terminate_records = getattr(self.window.app_context, "terminate_records", None)
        if callable(terminate_records):
            terminate_records(records)
        else:
            for record in records:
                self.window.stop_session(record)
                self.selected_ids.discard(record.id)
            self.refresh()
        if records:
            self.window.show_toast(toast_message_for_action("killed", len(records)), kind="danger")

    def _update_count_label(self) -> None:
        selected_count = len(self.selected_ids)
        total = self._visible_record_count
        shown = self._shown_record_count
        display = f"{shown} shown · {total} tabs" if shown < total else f"{total} tabs"
        if selected_count:
            display = f"{selected_count} selected · {display}"
        self.count_label.set_text(display)

    def refresh(self) -> None:
        # The pane is optional and hidden by default. Direct refresh requests
        # still arrive as tabs change; do no tmux, /proc, or widget work until
        # the user actually opens it.
        if not self.get_visible():
            self._last_refresh_signature = None
            return
        query = self.filter_entry.get_text().lower().strip()
        matching_records = _tabs_pane_records(
            self.window.app_context.store,
            self.window.window_id,
            query,
        )
        live_ids = {record.id for record in matching_records}
        self._visible_record_count = len(matching_records)
        self.selected_ids.intersection_update(live_ids)
        records = bounded_registry_records(
            matching_records,
            limit=SIDE_PANE_ROW_LIMIT,
            preferred_ids=self.selected_ids,
        )
        self._shown_record_count = len(records)
        self._update_count_label()
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
        # The model and resource working set stays bounded even when the
        # registry contains thousands of live background sessions.
        items = build_task_snapshot(
            records,
            backend=self.window.app_context.backend,
            sampler=self.window.app_context.sampler,
            policy=self.window.app_context.budget,
            snapshot_refresh_limit=UI_RESOURCE_REFRESH_BUDGET,
        )
        items_by_id = {item.record.id: item for item in items}
        if not records:
            empty_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            _style(empty_card, "td-empty-state")
            empty_icon = Gtk.Label(label="✦")
            _style(empty_icon, "td-empty-icon")
            empty_card.pack_start(empty_icon, False, False, 0)
            empty_title = Gtk.Label(label="No tabs in this window", xalign=0.5)
            _style(empty_title, "td-title")
            empty_card.pack_start(empty_title, False, False, 0)
            empty_detail = Gtk.Label(label="Open a shell immediately, then use cd to move wherever you need.", xalign=0.5)
            empty_detail.set_line_wrap(True)
            empty_detail.set_justify(Gtk.Justification.CENTER)
            _style(empty_detail, "td-muted")
            empty_card.pack_start(empty_detail, False, False, 0)
            empty_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            shell_button = Gtk.Button(label="New tab")
            _style(shell_button, "td-primary-action")
            shell_button.connect("clicked", lambda *_: self.window.new_tab())
            empty_actions.pack_start(shell_button, True, True, 0)
            rename_button = Gtk.Button(label="Name window")
            _style(rename_button, "td-ghost-action")
            rename_button.connect("clicked", lambda *_: self.window.rename_window())
            empty_actions.pack_start(rename_button, True, True, 0)
            empty_card.pack_start(empty_actions, False, False, 0)
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            row.add(empty_card)
            self.session_box.add(row)
            self.session_box.show_all()
            return
        for record in records:
            item = items_by_id[record.id]
            snap = item.snapshot
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

            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, margin=3)
            _style(card, "td-session-card")
            if record.id in self.selected_ids:
                _style(card, "td-session-selected")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            check = Gtk.CheckButton()
            check.set_active(record.id in self.selected_ids)
            check._termdeck_session_card = card  # type: ignore[attr-defined]
            check.connect("toggled", self._on_toggle, record.id)
            check.connect("button-press-event", self._on_session_button_press, record, check)
            top.pack_start(check, False, False, 0)
            pin = "📌 " if record.pinned else ""
            title = Gtk.Label(label=f"{kind_glyph(kind)} {pin}{record.name}", xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            _style(title, "td-title")
            top.pack_start(title, True, True, 0)
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

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            rename = Gtk.Button(label="Rename")
            rename.set_tooltip_text("Rename this tab")
            _style(rename, "td-ghost-action")
            rename.connect("clicked", lambda _b, r=record: self.window.rename_session(r))
            rename.connect("button-press-event", self._on_session_button_press, record, check)
            actions.pack_end(rename, False, False, 0)
            focus = Gtk.Button(label="Open")
            _style(focus, "td-primary-action")
            focus.connect("clicked", lambda _b, r=record: self.window.open_session(r))
            focus.connect("button-press-event", self._on_session_button_press, record, check)
            actions.pack_end(focus, False, False, 0)
            card.pack_start(actions, False, False, 0)

            card_event = Gtk.EventBox()
            card_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            card_event.add(card)
            card_event.connect("button-press-event", self._on_session_card_press, record, check)
            row = Gtk.ListBoxRow()
            # GtkListBox only emits its single-click activation path for rows
            # that participate in pointer selection. The list itself remains
            # in SelectionMode.NONE, so this enables activation without adding
            # a second visual selection state beside the explicit checkbox.
            row.set_selectable(True)
            row.set_activatable(True)
            row._termdeck_session_record = record  # type: ignore[attr-defined]
            row.add(card_event)
            self.session_box.add(row)
        self.session_box.show_all()

    def _on_toggle(self, button: Gtk.CheckButton, session_id: str) -> None:
        active = bool(button.get_active())
        if active:
            self.selected_ids.add(session_id)
        else:
            self.selected_ids.discard(session_id)
        card = getattr(button, "_termdeck_session_card", None)
        if card is not None:
            context = card.get_style_context()
            if active:
                context.add_class("td-session-selected")
            else:
                context.remove_class("td-session-selected")
        if hasattr(self, "count_label"):
            self._update_count_label()

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
            self.window.show_toast("Select a tab to rename", kind="warn")
            return
        # Rename acts on one tab at a time (same as CLI rename).
        self.window.rename_session(records[0])

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
            text="Archive selected tabs?",
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
        terminate_records = getattr(self.window.app_context, "terminate_records", None)
        if callable(terminate_records):
            terminate_records(records)
        else:
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

    def _build_session_tree(self, model: Gtk.ListStore) -> Gtk.TreeView:
        tree = Gtk.TreeView(model=model)
        _style(tree, "td-master-tree")
        tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        tree.get_selection().connect("changed", lambda *_: self.update_capture_preview())
        tree.get_selection().connect("changed", lambda *_: self.update_remove_sensitivity())
        tree.connect("button-press-event", self._on_tree_button_press)
        for idx, column in enumerate(self.COLUMNS):
            renderer = Gtk.CellRendererText()
            if column in {"ID", "PID", "Focus Key", "Pause Key", "CPU", "RAM"}:
                renderer.set_property("family", "Monospace")
            if column == "Health":
                renderer.set_property("weight", 600)
            col = Gtk.TreeViewColumn(column, renderer, text=idx)
            col.set_resizable(True)
            col.set_sort_column_id(idx)
            if column in {"Command", "Directory", "Health"}:
                col.set_expand(True)
            tree.append_column(col)
        return tree

    def __init__(self, app_context: "TermDeckApplication"):
        super().__init__(title="TermDeck Tab Manager")
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
        self.history_store = Gtk.ListStore(*([str] * len(self.COLUMNS)))
        self.tree = self._build_session_tree(self.store)
        self.history_tree = self._build_session_tree(self.history_store)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        explanation = Gtk.Label(
            label=(
                "Tabs contains current processes; Killed history keeps stopped tabs until Remove. "
                "Right-click a row to move that tab to another or a new window."
            ),
            xalign=0,
        )
        _style(explanation, "td-section-description")
        explanation.set_line_wrap(True)
        outer.pack_start(explanation, False, False, 0)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.filter_entry = Gtk.SearchEntry(placeholder_text="filter individual tabs by id, window, health, state, command, or directory")
        _style(self.filter_entry, "td-search-entry")
        _connect_debounced_refresh(self.filter_entry, self.refresh)
        top.pack_start(self.filter_entry, True, True, 0)
        self.count_label = Gtk.Label(label="0 tabs")
        _style(self.count_label, "td-count-pill")
        top.pack_start(self.count_label, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        top.pack_start(close, False, False, 0)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.filter_entry.set_text(""))
        top.pack_start(clear, False, False, 0)
        outer.pack_start(top, False, False, 0)

        self.view_notebook = Gtk.Notebook()
        for label, tree in (
            ("Tabs", self.tree),
            ("Killed history", self.history_tree),
        ):
            scroller = Gtk.ScrolledWindow()
            scroller.add(tree)
            self.view_notebook.append_page(scroller, Gtk.Label(label=label))
        self.view_notebook.connect_after(
            "switch-page",
            lambda *_: self.refresh(),
        )
        outer.pack_start(self.view_notebook, True, True, 0)

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
        self.prompt_entry = Gtk.Entry(placeholder_text="send text to selected tabs; use Send+Enter only when intentional")
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
            ("Remove", self.remove_selected),
            ("Kill", self.kill_selected),
            ("Archive", self.archive_selected),
            ("Pause Hot", self.pause_hot),
            ("Refresh", self.refresh),
        ]:
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, h=handler: h())
            if label == "Remove":
                self.remove_button = button
                button.set_tooltip_text("Permanently forget selected stopped tabs")
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

    @staticmethod
    def _selected_ids_for_tree(tree: Gtk.TreeView) -> list[str]:
        model, paths = tree.get_selection().get_selected_rows()
        ids = []
        for path in paths:
            iter_ = model.get_iter(path)
            ids.append(model.get_value(iter_, 0))
        return ids

    def selected_ids(self) -> list[str]:
        tree = self.history_tree if self.view_notebook.get_current_page() == 1 else self.tree
        return self._selected_ids_for_tree(tree)

    def selected_records(self) -> list[SessionRecord]:
        records = []
        for session_id in self.selected_ids():
            record = self.app_context.store.get(session_id)
            if record:
                records.append(record)
        return records

    def _move_record_to_window(self, record: SessionRecord, destination: "TermDeckWindow") -> None:
        if self.app_context.move_tab_to_window(record.id, destination) is None:
            self.refresh()

    def _move_record_to_new_window(self, record: SessionRecord) -> None:
        destination = self.app_context.prompt_for_new_window(
            parent=self,
            open_initial_tab=False,
        )
        if destination is not None:
            self._move_record_to_window(record, destination)

    def _show_row_context_menu(self, record: SessionRecord, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        move_item = Gtk.MenuItem(label="Move to window")
        destinations = self.app_context.available_move_windows(record)
        submenu = Gtk.Menu()
        if destinations:
            for destination in destinations:
                item = Gtk.MenuItem(label=destination.window_title)
                item.connect(
                    "activate",
                    lambda _item, target=destination: self._move_record_to_window(record, target),
                )
                submenu.append(item)
        else:
            unavailable = Gtk.MenuItem(label="No other open windows")
            unavailable.set_sensitive(False)
            submenu.append(unavailable)
        new_window = Gtk.MenuItem(label="New window…")
        new_window.connect(
            "activate",
            lambda _item: self._move_record_to_new_window(record),
        )
        submenu.append(new_window)
        move_item.set_submenu(submenu)
        menu.append(move_item)
        menu.show_all()
        # Keep a reference while the popup is active. This also gives the real
        # GTK smoke a stable way to verify the menu created by a pointer click.
        self._row_context_menu = menu
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(
                None,
                None,
                None,
                None,
                int(getattr(event, "button", 3) or 3),
                int(getattr(event, "time", 0) or 0),
            )

    def _on_tree_button_press(self, _tree, event: Gdk.EventButton) -> bool:
        if int(getattr(event, "button", 0) or 0) != 3:
            return False
        target = _tree.get_path_at_pos(
            int(getattr(event, "x", 0) or 0),
            int(getattr(event, "y", 0) or 0),
        )
        if target is None:
            return False
        path, column = target[0], target[1]
        selection = _tree.get_selection()
        selection.unselect_all()
        selection.select_path(path)
        _tree.set_cursor(path, column, False)
        model = _tree.get_model()
        iter_ = model.get_iter(path)
        session_id = model.get_value(iter_, 0)
        record = self.app_context.store.get(session_id)
        if record is None:
            return False
        self._show_row_context_menu(record, event)
        return True

    def refresh(self, *_args) -> None:
        selected_tabs = set(self._selected_ids_for_tree(self.tree))
        selected_history = set(self._selected_ids_for_tree(self.history_tree))
        self.store.clear()
        self.history_store.clear()
        records = self.app_context.store.list()
        items = build_task_snapshot(
            records,
            backend=self.app_context.backend,
            sampler=self.app_context.sampler,
            policy=self.app_context.budget,
            snapshot_refresh_limit=UI_RESOURCE_REFRESH_BUDGET,
        )
        matching = filter_rows([item.row for item in items], self.filter_entry.get_text())
        tab_rows, history_rows = _partition_master_rows(records, matching)
        showing_history = self.view_notebook.get_current_page() == 1
        current_rows = history_rows if showing_history else tab_rows
        shown = current_rows[:MASTER_ROW_LIMIT]
        if len(shown) < len(current_rows):
            self.count_label.set_text(f"{len(shown)} shown · {len(current_rows)} matching · {len(records)} total")
        elif len(current_rows) != len(records):
            self.count_label.set_text(f"{len(current_rows)} matching · {len(records)} total")
        else:
            self.count_label.set_text(f"{len(records)} tabs")
        for model, tree, rows, selected in (
            (self.store, self.tree, tab_rows, selected_tabs),
            (self.history_store, self.history_tree, history_rows, selected_history),
        ):
            for row in rows[:MASTER_ROW_LIMIT]:
                values = row.as_list()
                iter_ = model.append(values[:3] + values[4:])
                if row.session_id in selected:
                    path = model.get_path(iter_)
                    tree.get_selection().select_path(path)
        self.update_capture_preview()
        self.update_remove_sensitivity()

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
            capture = "No captured output yet, or the hidden tab process is not currently available."
        self._capture_preview_cache = {key: capture}
        return capture

    def update_capture_preview(self) -> None:
        records = self.selected_records()
        if not records:
            self.set_capture_text("Select a tab to preview its latest terminal output.")
            return
        if len(records) > 1:
            names = ", ".join(record.name for record in records[:6])
            suffix = "..." if len(records) > 6 else ""
            self.set_capture_text(f"{len(records)} tabs selected: {names}{suffix}\nUse the buttons below to control them together.")
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
            selection="tab manager selection",
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
            selection="tab manager status request",
        ):
            return
        for record in records:
            self.app_context.backend.send_text(record, text, enter=True)
        self.update_capture_preview()

    def focus_selected(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        record = records[0]
        target = self.app_context.window_for_tab(record.id)
        if target is None:
            target = next(
                (
                    window
                    for window in self.app_context.windows
                    if window.window_id == record.window_id
                ),
                None,
            )
        if target is None:
            return
        target.open_session(
            record,
            restart_missing=False,
            remember_layout=False,
        )
        target.present()

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
            selection="tab manager selection",
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
            selection="tab manager hot tabs",
        ):
            return
        for record in records:
            new_pid = self.app_context.backend.pause(record)
            self.app_context.store.update(record.id, status="paused", last_pid=new_pid)
        self.refresh()

    def remove_selected(self, *_args) -> None:
        """Remove selected tabs from the manager display and session registry.

        Remove is post-stop cleanup only. It never signals a process and never
        deletes a live, paused, new, missing, or pinned tab.
        """

        records = self.selected_records()
        if not records or any(
            record.status.casefold() != "stopped" or record.pinned
            for record in records
        ):
            MasterWindow.update_remove_sensitivity(self)
            return
        for record in records:
            for window in self.app_context.windows:
                if record.id in getattr(window, "tabs", {}):
                    remove_widget = getattr(window, "_remove_tab_widget", None)
                    if callable(remove_widget) and remove_widget(record.id):
                        renumber = getattr(window, "_renumber_visible_tabs", None)
                        if callable(renumber):
                            renumber()
                        side = getattr(window, "side", None)
                        refresh_side = getattr(side, "refresh", None) if side is not None else None
                        if callable(refresh_side):
                            refresh_side()
            try:
                self.app_context.store.delete(record.id)
            except Exception:
                pass
        self.refresh()

    def update_remove_sensitivity(self) -> None:
        records = self.selected_records()
        allowed = bool(records) and all(
            record.status.casefold() == "stopped" and not record.pinned
            for record in records
        )
        button = getattr(self, "remove_button", None)
        if button is not None:
            button.set_sensitive(allowed)

    # Backward-compatible alias used by older call sites / tests.
    hide_selected = remove_selected

    def archive_selected(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Archive selected tabs?",
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
            selection="tab manager selection",
        ):
            return
        self.app_context.terminate_records(records)
        self.refresh()


class TermDeckWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        app_context: "TermDeckApplication",
        *,
        window_id: str | None = None,
        window_title: str | None = None,
        restore_records: list[SessionRecord] | None = None,
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
        self.tabs: dict[str, TerminalTab] = {}
        self.master_window: MasterWindow | None = None
        self.window_overview: WindowOverview | None = None
        self.shortcut_window: ShortcutWindow | None = None
        self.search_window: SessionSearchWindow | None = None
        self.set_default_size(app_context.theme.window_width or 1280, app_context.theme.window_height or 780)
        self.tabs_pane_visible = bool(getattr(app_context.settings, "tabs_pane_visible", False))
        self._toast_source = None
        self._tab_directory_refresh_alive = True
        self._terminal_layout_sync_source_id = 0
        self._side_pane_layout_source_id = 0
        # Relative point delta from the configured theme font (Ctrl++ / Ctrl+-).
        self.font_zoom_delta = 0

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title = Gtk.Label(label=self.window_title, xalign=0.5)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(28)
        title.set_tooltip_text("Window name · double-click to rename")
        _style(title, "td-title")
        self.window_title_label = title
        title_event = Gtk.EventBox()
        title_event.set_visible_window(False)
        title_event.add(title)
        title_event.set_events(title_event.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK)
        title_event.connect("button-press-event", self._on_window_title_press)
        header.set_custom_title(title_event)
        self.set_titlebar(header)
        self.set_title(f"TermDeck · {self.window_title}")

        new_tab = _icon_button(
            "New tab",
            "list-add-symbolic",
            "New tab · Ctrl+T · opens in the home directory",
            self.new_tab,
            suggested=False,
            show_label=False,
        )
        _style(new_tab, "td-new-tab", "td-header-icon")
        # Keep the button a centred square instead of stretching to the full
        # headerbar height (noticeable at large UI font scales).
        new_tab.set_valign(Gtk.Align.CENTER)
        header.pack_start(new_tab)

        overflow_header_actions = [
            ("Rename window", self.rename_window),
            ("New TermDeck window", self.new_window),
            ("Search tabs", self.open_search_window),
            ("Command palette", self.open_command_palette),
            ("Reopen closed tab", self.reopen_last_closed_tab),
            ("Show/hide Tabs pane", self.toggle_tabs_pane),
            ("Tab manager · per-tab details", self.open_master_window),
            ("Window overview · window totals", self.open_window_overview),
            ("Keyboard shortcuts", self.open_shortcut_window),
            ("Terminal appearance", self.open_terminal_options),
            ("Quit TermDeck", self.quit_termdeck),
        ]
        overflow_menu = Gtk.Menu()
        for label, handler in overflow_header_actions:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, h=handler: h())
            overflow_menu.append(item)
        overflow_menu.show_all()
        overflow = Gtk.MenuButton(label="⋯")
        overflow.set_tooltip_text("More actions · Ctrl+Shift+P opens the command palette")
        try:
            overflow.get_accessible().set_name("More actions")
        except Exception:
            pass
        overflow.set_popup(overflow_menu)
        _style(overflow, "td-header-overflow", "td-header-icon")
        header.pack_end(overflow)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_hexpand(True)
        self.paned.set_vexpand(True)
        self.notebook = Gtk.Notebook(scrollable=True)
        _style(self.notebook, "td-terminal-notebook")
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.notebook.set_scrollable(True)
        self.notebook.connect("page-reordered", self._on_page_reordered)
        # Browser-style new-tab button docked at the end of the tab strip,
        # complementing the headerbar action (GTK hides action widgets when
        # the notebook has no pages, so the headerbar button still covers
        # the empty state).
        strip_new_tab = _icon_button(
            "New tab",
            "list-add-symbolic",
            "New tab · Ctrl+T · opens in the home directory",
            self.new_tab,
            suggested=False,
            show_label=False,
        )
        _style(strip_new_tab, "td-new-tab", "td-tabstrip-new")
        strip_new_tab.set_halign(Gtk.Align.START)
        strip_new_tab.set_valign(Gtk.Align.START)
        strip_new_tab.show_all()
        strip_new_tab.set_no_show_all(True)
        self._strip_new_tab = strip_new_tab
        self._strip_new_tab_idle = 0
        self.notebook.connect("size-allocate", lambda *_a: self._queue_strip_new_tab_position())
        self.notebook.connect("map", lambda *_a: self._queue_strip_new_tab_position())
        self.notebook.connect("page-added", lambda *_a: self._queue_strip_new_tab_position())
        self.notebook.connect("page-removed", lambda *_a: self._queue_strip_new_tab_position())
        self.notebook.connect("page-reordered", lambda *_a: self._queue_strip_new_tab_position())
        self.side = SidePane(self)
        self.side_position = app_context.settings.side_pane_position
        self._pack_side_pane()

        root = Gtk.Overlay()
        _style(root, "td-main-root")
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.add(self.paned)
        self._chrome_root = root
        root.add_overlay(strip_new_tab)
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

        self.connect("key-press-event", self.on_key_press)
        self.connect("delete-event", self.on_delete)
        self.connect("destroy", self._on_destroy)
        self.show_all()
        self.side.set_visible(self.tabs_pane_visible)
        if self.tabs_pane_visible:
            self._queue_side_pane_layout_repack()
        self.side.refresh()
        self.restore_previous_tabs(records=restore_records)
        GLib.timeout_add_seconds(
            int(max(1, round(getattr(app_context.settings, "ui_refresh_interval_seconds", 5.0)))),
            self._refresh_tab_directories_timer,
        )

    def on_delete(self, *_args) -> bool:
        # Detach VTE clients only. Leave tab rows visible so browser-style
        # multi-window restore can reopen this named window on the next launch.
        for tab in list(self.tabs.values()):
            tab.widget.detach()
        return False

    def _on_destroy(self, *_args) -> None:
        self._tab_directory_refresh_alive = False
        if self._terminal_layout_sync_source_id:
            try:
                GLib.source_remove(self._terminal_layout_sync_source_id)
            except Exception:
                pass
            self._terminal_layout_sync_source_id = 0
        if self._side_pane_layout_source_id:
            try:
                GLib.source_remove(self._side_pane_layout_source_id)
            except Exception:
                pass
            self._side_pane_layout_source_id = 0
        windows = getattr(self.app_context, "windows", None)
        if isinstance(windows, list) and self in windows:
            windows.remove(self)

    def _on_window_title_press(self, _widget, event: Gdk.EventButton) -> bool:
        """Double-click the window title in the headerbar to rename it."""
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and int(getattr(event, "button", 0) or 0) == 1:
            self.rename_window()
            return True
        return False

    def _queue_strip_new_tab_position(self) -> None:
        """Coalesce re-anchoring of the floating new-tab button to idle."""
        if getattr(self, "_strip_new_tab_idle", 0):
            return
        self._strip_new_tab_idle = GLib.idle_add(self._position_strip_new_tab)

    def _position_strip_new_tab(self) -> bool:
        """Park the + button just to the right of the last tab, like a browser."""
        self._strip_new_tab_idle = 0
        btn = getattr(self, "_strip_new_tab", None)
        root = getattr(self, "_chrome_root", None)
        if btn is None or root is None:
            return False
        pages = self.notebook.get_n_pages()
        if pages <= 0:
            btn.hide()
            return False
        last = self.notebook.get_nth_page(pages - 1)
        tab_label = self.notebook.get_tab_label(last) if last is not None else None
        if tab_label is None or not tab_label.get_mapped():
            btn.hide()
            return False
        coords = tab_label.translate_coordinates(root, 0, 0)
        if coords is None:
            btn.hide()
            return False
        x, y = coords
        alloc = tab_label.get_allocation()
        gap = 6
        btn_w = btn.get_allocated_width() or 30
        btn_h = btn.get_allocated_height() or 30
        left = x + alloc.width + gap
        # If the strip has scrolled the last tab past the visible notebook
        # width, fall back to the headerbar button rather than overlapping.
        strip_right = self.notebook.get_allocated_width()
        if left + btn_w > strip_right:
            btn.hide()
            return False
        btn.set_margin_start(max(0, left))
        btn.set_margin_top(max(0, y + (alloc.height - btn_h) // 2))
        btn.show()
        return False

    def _build_tab_label(self, record: SessionRecord) -> Gtk.Box:
        """Minimal notebook tab containing only the tab name and close control."""
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        name = Gtk.Label(label=record.name)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_width_chars(min(20, max(4, len(record.name))))
        name.set_max_width_chars(20)
        state = "Pinned · " if record.pinned else ""
        name.set_tooltip_text(
            f"{state}{record.cwd} · {record.command}\n"
            "Double-click to rename · Right-click for tab actions"
        )
        name.set_events(name.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK)
        name.connect("button-press-event", self._on_tab_label_press, record.id)
        label_box.pack_start(name, True, True, 0)
        close = Gtk.Button(label="×")
        close.set_relief(Gtk.ReliefStyle.NONE)
        _style(close, "td-tab-close")
        close.set_tooltip_text(f"Close {record.name} · Ctrl+W · session keeps running")
        try:
            close.get_accessible().set_name(f"Close {record.name}")
        except Exception:
            pass
        close.connect("clicked", lambda _b, sid=record.id: self._hide_tab_from_close_button(sid))
        close.connect("button-press-event", self._on_tab_label_press, record.id)
        label_box.pack_start(close, False, False, 0)
        label_box.show_all()
        return label_box

    def _tab_menu_item(self, label: str, handler) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda *_: handler())
        return item

    def _hide_tab_from_close_button(self, session_id: str) -> None:
        self.hide_tab(session_id, include_pinned=True)

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
        pin_label = "Unpin tab" if record.pinned else "Pin tab"
        menu.append(self._tab_menu_item(pin_label, lambda: self._pin_tab(record, not record.pinned)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._tab_menu_item("Hide tab", lambda: self.hide_tab(record.id)))
        if record.pinned:
            menu.append(self._tab_menu_item("Unpin and hide tab", lambda: self._unpin_and_hide_tab(record)))
        menu.append(self._tab_menu_item("Hide other tabs", lambda: self._hide_other_tabs(record)))
        menu.append(self._tab_menu_item("Hide tabs to the right", lambda: self._hide_tabs_to_right(record)))
        menu.append(self._tab_menu_item("Kill tab", lambda: self.stop_session(record)))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._tab_menu_item("Move tabs pane to left", lambda: self.set_side_pane_position("left")))
        menu.append(self._tab_menu_item("Move tabs pane to right", lambda: self.set_side_pane_position("right")))
        menu.append(self._tab_menu_item("Terminal options", self.open_terminal_options))
        menu.append(self._tab_menu_item("Tab manager", self.open_master_window))
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

    @staticmethod
    def _directory_tracking_changes(record: SessionRecord, live_directory: str) -> dict[str, str]:
        old_directory = normalise_directory(record.cwd)
        new_directory = normalise_directory(live_directory)
        if old_directory == new_directory:
            return {}
        changes = {"cwd": new_directory}
        # Default names follow cwd; a deliberate custom rename remains an
        # override because it no longer matches the previous cwd basename.
        if record.name == directory_tab_name(old_directory):
            changes["name"] = directory_tab_name(new_directory)
        return changes

    def refresh_tab_directories(self) -> int:
        records = [tab.record for tab in self.tabs.values()]
        resolve_many = getattr(self.app_context.backend, "current_directories", None)
        if not records or not callable(resolve_many):
            return 0
        try:
            live_by_id = resolve_many(records)
        except Exception:
            return 0
        changed = 0
        for session_id, tab in list(self.tabs.items()):
            live_directory = live_by_id.get(session_id)
            if not live_directory:
                continue
            record = self.app_context.store.get(session_id) or tab.record
            changes = self._directory_tracking_changes(record, live_directory)
            if not changes:
                continue
            try:
                updated = self.app_context.store.update(
                    session_id,
                    last_opened_at=record.last_opened_at,
                    **changes,
                )
            except KeyError:
                continue
            self.refresh_tab_label(updated)
            changed += 1
        if changed:
            self.side.refresh()
            if self.master_window is not None and self.master_window.get_visible():
                self.master_window.refresh()
        return changed

    def _refresh_tab_directories_timer(self) -> bool:
        if not getattr(self, "_tab_directory_refresh_alive", False):
            return False
        if self.get_mapped() and self.get_visible():
            self.refresh_tab_directories()
        return True

    def rename_session(self, record: SessionRecord) -> SessionRecord | None:
        """Rename a tab without touching the running process."""
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

    def set_tabs_pane_visible(
        self,
        visible: bool,
        *,
        persist: bool = True,
        notify: bool = True,
    ) -> None:
        self.tabs_pane_visible = bool(visible)
        self.side.set_visible(self.tabs_pane_visible)
        repack = getattr(self, "_pack_side_pane", None)
        if callable(repack):
            repack()
        if self.tabs_pane_visible:
            self.side.refresh()
            queue_repack = getattr(self, "_queue_side_pane_layout_repack", None)
            if callable(queue_repack):
                queue_repack()
        if persist:
            self.app_context.apply_tabs_pane_visibility(self.tabs_pane_visible)
        if notify:
            self.show_toast(
                "Tabs pane shown" if self.tabs_pane_visible else "Tabs pane hidden",
                kind="info",
            )

    def toggle_tabs_pane(self) -> None:
        self.set_tabs_pane_visible(not self.tabs_pane_visible)

    def _prompt_window_name(self, *, title: str, initial: str) -> str | None:
        dialog = WindowNameDialog(self, title=title, initial=initial)
        response = dialog.run()
        value = dialog.value()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not value:
            return None
        return value

    def set_window_name(self, title: str) -> None:
        clean_title = title.strip() or "Window"
        self.window_title = clean_title
        self.set_title(f"TermDeck · {clean_title}")
        self.window_title_label.set_text(clean_title)
        self.app_context.store.rename_window(self.window_id, clean_title)
        for session_id, tab in self.tabs.items():
            updated = self.app_context.store.get(session_id)
            if updated is not None:
                tab.record = updated
        self.side.refresh()
        self.show_toast(f"Window renamed to {clean_title}", kind="success")

    def rename_window(self) -> None:
        title = self._prompt_window_name(title="Rename TermDeck window", initial=self.window_title)
        if title is not None:
            self.set_window_name(title)

    def new_window(self) -> None:
        self.app_context.prompt_for_new_window(parent=self)

    def _active_directory(self) -> str:
        """Return the active pane's live cwd, then stored cwd, then user home."""

        record = self.current_record()
        if record is not None:
            live_directory = self.app_context.backend.current_directory(record)
            if live_directory:
                return normalise_directory(live_directory)
            if record.cwd:
                return normalise_directory(record.cwd)
        return normalise_directory(Path.home())

    def new_tab(self) -> None:
        """Open a shell immediately in the home directory."""

        self.new_shell_tab(Path.home())

    def new_directory_tab(self) -> None:
        """Backward-compatible alias for the direct, non-modal New tab action."""

        self.new_tab()

    def new_shell_tab(self, cwd: str | Path | None = None) -> None:
        directory = normalise_directory(cwd or Path.home())
        self.create_session(directory_tab_name(directory), "terminal", default_shell(), directory)

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
                "new_tab": self.new_tab,
                "rename_window": self.rename_window,
                "new_window": self.new_window,
                "master": self.open_master_window,
                "window_overview": self.open_window_overview,
                "shortcuts": self.open_shortcut_window,
                "search_sessions": self.open_search_window,
                "reopen_closed": self.reopen_last_closed_tab,
                "restore": self.restore_previous_tabs,
                "toggle_side": self.toggle_side,
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

    def create_session(self, name: str, tag: str, command: str, cwd: str) -> None:
        directory = normalise_directory(cwd)
        clean_name = name.strip()
        if not clean_name or clean_name.casefold() in {"shell", "terminal"}:
            clean_name = directory_tab_name(directory)
        record = self.app_context.store.create(
            name=clean_name,
            tag=tag.strip() or "terminal",
            command=command.strip() or default_shell(),
            cwd=directory,
            window_id=self.window_id,
            window_title=self.window_title,
            tab_index=self.notebook.get_n_pages(),
            visible=True,
        )
        self.app_context.backend.create_or_resume(record)
        pid = self.app_context.backend.pane_pid(record)
        record = self.app_context.store.update(record.id, status="running", last_pid=pid)
        self.open_session(
            record,
            known_live=True,
            known_pid=pid,
            known_pid_resolved=True,
            refresh_side=False,
        )
        self.side.refresh()


    def restore_previous_tabs(self, records: list[SessionRecord] | None = None) -> int:
        """Reattach remembered tabs for this window without restarting missing processes."""
        restored = 0
        restore_records = sorted(records, key=lambda row: row.tab_index) if records is not None else self.app_context.store.restore_candidates(
            limit=12,
            window_id=self.window_id,
        )
        remember_layout = records is None
        pending = [record for record in restore_records if record.id not in self.tabs]
        live_by_id: dict[str, bool] | None = None
        pids_by_id: dict[str, int | None] = {}
        pids_resolved = False
        exists_many = getattr(self.app_context.backend, "session_exists_many", None)
        if callable(exists_many):
            try:
                live_by_id = dict(exists_many(pending))
                live_records = [record for record in pending if live_by_id.get(record.id, False)]
                pane_pids = getattr(self.app_context.backend, "pane_pids", None)
                if callable(pane_pids):
                    pids_by_id = dict(pane_pids(live_records))
                    pids_resolved = True
            except Exception:
                # Preserve the existing per-session fallback when a third-party
                # backend cannot provide a batch snapshot.
                live_by_id = None
                pids_by_id = {}
                pids_resolved = False
        for record in restore_records:
            if record.id in self.tabs:
                continue
            before = len(self.tabs)
            self.open_session(
                record,
                restart_missing=False,
                remember_layout=remember_layout,
                known_live=live_by_id.get(record.id) if live_by_id is not None else None,
                known_pid=pids_by_id.get(record.id),
                known_pid_resolved=pids_resolved,
                refresh_side=False,
            )
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
        if getattr(self.app_context.backend, "pane_snapshot_stale", False):
            if record.status == "paused":
                return "paused"
            if record.status in {"running", "missing"}:
                return "running"
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

    def open_session(
        self,
        record: SessionRecord,
        restart_missing: bool = True,
        remember_layout: bool = True,
        *,
        known_live: bool | None = None,
        known_pid: int | None = None,
        known_pid_resolved: bool = False,
        refresh_side: bool = True,
    ) -> None:
        live_session = self.app_context.backend.session_exists(record) if known_live is None else known_live
        if not live_session:
            if not restart_missing:
                self.app_context.store.update(record.id, status="stopped", last_pid=None, visible=False)
                if refresh_side:
                    self.side.refresh()
                return
            self.app_context.backend.create_or_resume(record)
            known_pid = None
        pid = (
            known_pid
            if live_session and known_live is not None and known_pid_resolved
            else self.app_context.backend.pane_pid(record)
        )
        status = self.observed_status(record, pid)
        layout_changes = {}
        if remember_layout:
            layout_changes = {
                "visible": True,
                "tab_index": self.notebook.get_n_pages(),
            }
            # Never reassign a tab that already belongs to another named window.
            # Stealing window_id here is what collapses multi-window restore into
            # whichever window last opened the tab (search, palette, or Restore).
            if not record.window_id or record.window_id == self.window_id:
                layout_changes["window_id"] = self.window_id
                layout_changes["window_title"] = self.window_title
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
        configure = getattr(self.app_context.backend, "configure_interactive_session", None)
        if callable(configure):
            configure(record)
        terminal = AttachTerminal(
            self.app_context.backend.attach_command(record),
            record.cwd,
            self.app_context.theme,
            key_press_handler=self.on_terminal_key_press,
            live_focus_handler=lambda r=record: self._restore_live_terminal(r),
            scroll_handler=lambda lines, r=record: self._scroll_terminal(r, lines),
            scrollback_extent=getattr(
                self.app_context.backend,
                "history_limit",
                self.app_context.theme.scrollback_lines,
            ),
            font_override=self.zoomed_font(),
        )
        tab = TerminalTab(record, terminal)
        self.tabs[record.id] = tab
        label_box = self._build_tab_label(record)
        page = self.notebook.append_page(terminal, label_box)
        self.notebook.set_tab_reorderable(terminal, True)
        terminal.show_all()
        self.notebook.set_current_page(page)
        terminal.terminal.grab_focus()
        if refresh_side:
            self.side.refresh()

    def _focus_session(self, session_id: str) -> None:
        tab = self.tabs.get(session_id)
        if not tab:
            return
        page = self.notebook.page_num(tab.widget)
        if page >= 0:
            self.notebook.set_current_page(page)
            try:
                updates = {"visible": True, "tab_index": page}
                if not tab.record.window_id or tab.record.window_id == self.window_id:
                    updates["window_id"] = self.window_id
                    updates["window_title"] = self.window_title
                self.app_context.store.update(session_id, **updates)
            except KeyError:
                pass
            tab.widget.prepare_for_live_input()
            tab.widget.terminal.grab_focus()
            self.present()

    def _restore_live_terminal(self, record: SessionRecord) -> None:
        restore = getattr(self.app_context.backend, "restore_live_session", None)
        if callable(restore):
            restore(record)

    def _scroll_terminal(self, record: SessionRecord, lines: int) -> bool:
        scroll = getattr(self.app_context.backend, "scroll_session", None)
        return bool(scroll(record, lines)) if callable(scroll) else False

    def _sync_current_terminal_geometry(self) -> bool:
        self._terminal_layout_sync_source_id = 0
        session_id = self.current_session_id()
        tab = self.tabs.get(session_id) if session_id else None
        if tab is not None:
            tab.widget.request_geometry_sync(delay_ms=0)
        return False

    def _queue_current_terminal_geometry_sync(self) -> None:
        if self._terminal_layout_sync_source_id:
            try:
                GLib.source_remove(self._terminal_layout_sync_source_id)
            except Exception:
                pass
        self._terminal_layout_sync_source_id = GLib.timeout_add(
            80,
            self._sync_current_terminal_geometry,
        )

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
            selection="visible hot tabs",
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

    def close_current_tab(self) -> None:
        session_id = self.current_session_id()
        if not session_id:
            return
        record = self.app_context.store.get(session_id)
        self.hide_tab(session_id, include_pinned=True)
        if record is not None:
            self.show_toast(f"Closed {record.name} · session still running", kind="info")

    def hide_current_tab(self) -> None:
        # Backward-compatible method name used by older call sites.
        self.close_current_tab()

    def quit_termdeck(self) -> None:
        self.app_context.quit_preserving_sessions()

    def _remove_tab_widget(self, session_id: str) -> bool:
        tab = self.tabs.pop(session_id, None)
        if not tab:
            return False
        page = self.notebook.page_num(tab.widget)
        if page >= 0:
            tab.widget.detach()
            self.notebook.remove_page(page)
        return True

    def hide_tab(self, session_id: str, *, include_pinned: bool = False) -> None:
        record = self.app_context.store.get(session_id)
        if record and record.pinned and not include_pinned:
            return
        if not self._remove_tab_widget(session_id):
            return
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
        self.app_context.terminate_records([record])

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

    def _queue_side_pane_layout_repack(self) -> None:
        if self._side_pane_layout_source_id:
            try:
                GLib.source_remove(self._side_pane_layout_source_id)
            except Exception:
                pass
        self._side_pane_layout_source_id = GLib.idle_add(
            self._repack_side_pane_after_style,
        )

    def _repack_side_pane_after_style(self) -> bool:
        self._side_pane_layout_source_id = 0
        self._pack_side_pane()
        return False

    def _side_pane_layout_width(self) -> int:
        """Return a sidebar width that cannot clip its scaled UI controls."""

        minimum = 0
        get_preferred_width = getattr(self.side, "get_preferred_width", None)
        if callable(get_preferred_width):
            try:
                minimum, _natural = get_preferred_width()
            except Exception:
                minimum = 0
        return max(340, int(minimum))

    def _paned_layout_width(self) -> int:
        get_allocated_width = getattr(self.paned, "get_allocated_width", None)
        if callable(get_allocated_width):
            try:
                available = int(get_allocated_width())
            except Exception:
                available = 0
            if available > 400:
                return available
        get_default_size = getattr(self, "get_default_size", None)
        if callable(get_default_size):
            try:
                default_width, _default_height = get_default_size()
                if int(default_width) > 400:
                    return int(default_width)
            except Exception:
                pass
        # Retain the historical 980px divider fallback for a 340px right pane.
        return 1320

    def _pack_side_pane(self) -> None:
        for child in (self.notebook, self.side):
            try:
                if child.get_parent() is self.paned:
                    self.paned.remove(child)
            except Exception:
                pass
        if not bool(getattr(self, "tabs_pane_visible", True)):
            # Hiding a GtkPaned child does not reliably surrender its previous
            # allocation. Unparent the pane so the notebook receives every
            # available pixel instead of retaining a sidebar-sized dead strip.
            self.paned.pack1(self.notebook, True, True)
            self.paned.show_all()
            self.side.set_visible(False)
            sync_geometry = getattr(self, "_queue_current_terminal_geometry_sync", None)
            if callable(sync_geometry):
                sync_geometry()
            return
        side_width = TermDeckWindow._side_pane_layout_width(self)
        if self.side_position == "left":
            self.paned.pack1(self.side, False, True)
            self.paned.pack2(self.notebook, True, True)
            self.paned.set_position(side_width)
        else:
            self.paned.pack1(self.notebook, True, True)
            self.paned.pack2(self.side, False, True)
            available = TermDeckWindow._paned_layout_width(self)
            self.paned.set_position(max(1, available - side_width))
        self.paned.show_all()
        sync_geometry = getattr(self, "_queue_current_terminal_geometry_sync", None)
        if callable(sync_geometry):
            sync_geometry()

    def set_side_pane_position(self, position: str, *, persist: bool = True) -> None:
        position = "left" if str(position).lower().startswith("left") else "right"
        self.side_position = position
        self._pack_side_pane()
        queue_repack = getattr(self, "_queue_side_pane_layout_repack", None)
        if bool(getattr(self, "tabs_pane_visible", True)) and callable(queue_repack):
            queue_repack()
        if persist:
            self.app_context.apply_side_pane_position(position)
        self.show_toast(f"Tabs pane moved to {position}", kind="info")

    def toggle_side(self) -> None:
        self.toggle_tabs_pane()

    def _on_page_reordered(self, _notebook, _child, _page_num: int) -> None:
        self._renumber_visible_tabs()
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()

    def _renumber_visible_tabs(self) -> None:
        for index in range(self.notebook.get_n_pages()):
            widget = self.notebook.get_nth_page(index)
            for session_id, tab in self.tabs.items():
                if tab.widget is widget:
                    try:
                        tab.record = self.app_context.store.update(
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
        """Ctrl+Shift+V, Ctrl+V, or Shift+Insert → system clipboard paste."""
        state = self.keymap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        is_v = keyval in {Gdk.KEY_v, Gdk.KEY_V} or name == "v"
        # Ctrl+Shift+V is unconditional: imported keymaps must not displace the
        # standard terminal clipboard shortcut.
        if ctrl and shift and is_v:
            return True
        if self.keymap.matches(event, "paste"):
            return True
        # Ctrl+V (browser-style paste into terminal)
        if ctrl and not shift and is_v:
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
        # Ctrl+Shift+C always copies; plain Ctrl+C is selection-aware below.
        if ctrl and shift and (keyval in {Gdk.KEY_c, Gdk.KEY_C} or name == "c"):
            return True
        return False

    def _is_plain_ctrl_c_event(self, event: Gdk.EventKey) -> bool:
        state = self.keymap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        return ctrl and not shift and (keyval in {Gdk.KEY_c, Gdk.KEY_C} or name == "c")

    @staticmethod
    def _terminal_has_selection(attach) -> bool:
        try:
            return bool(attach.terminal.get_has_selection())
        except Exception:
            return False

    def on_terminal_key_press(self, _terminal, event: Gdk.EventKey) -> bool:
        # Browser-style app actions must be handled before VTE consumes them.
        if self.keymap.matches(event, "new_tab"):
            self.new_tab()
            return True
        if self.keymap.matches(event, "quit_app"):
            self.quit_termdeck()
            return True
        if self.keymap.matches(event, "close_term"):
            self.close_current_tab()
            return True
        # Capture zoom / clipboard before VTE treats the chord as input.
        if self.handle_zoom_event(event):
            return True
        attach = self._current_attach_terminal()
        if attach is None:
            return False
        if self._is_copy_event(event):
            attach.copy_clipboard()
            return True
        if self._is_plain_ctrl_c_event(event) and self._terminal_has_selection(attach):
            attach.copy_clipboard()
            return True
        if self._is_paste_event(event):
            record_getter = getattr(self, "current_record", None)
            record = record_getter() if callable(record_getter) else None
            backend = getattr(getattr(self, "app_context", None), "backend", None)
            active = getattr(backend, "scroll_view_active", None)
            if record is not None and callable(active) and active(record):
                self._restore_live_terminal(record)
                show_cursor = getattr(attach, "_set_history_cursor_hidden", None)
                if callable(show_cursor):
                    show_cursor(False)
                reset_scrollbar = getattr(attach, "reset_scrollbar", None)
                if callable(reset_scrollbar):
                    reset_scrollbar()
            attach.paste_clipboard()
            return True
        record_getter = getattr(self, "current_record", None)
        record = record_getter() if callable(record_getter) else None
        backend = getattr(getattr(self, "app_context", None), "backend", None)
        active = getattr(backend, "scroll_view_active", None)
        if record is not None and callable(active) and active(record):
            self._restore_live_terminal(record)
            show_cursor = getattr(attach, "_set_history_cursor_hidden", None)
            if callable(show_cursor):
                show_cursor(False)
            reset_scrollbar = getattr(attach, "reset_scrollbar", None)
            if callable(reset_scrollbar):
                reset_scrollbar()
        return False

    @staticmethod
    def _is_text_input_focus(widget) -> bool:
        return isinstance(widget, (Gtk.Entry, Gtk.TextView))

    def _route_unhandled_key_to_terminal(self, event: Gdk.EventKey) -> bool:
        """Focus VTE for keys pressed on non-editable window chrome."""

        attach = self._current_attach_terminal()
        if attach is None:
            return False
        try:
            focus = self.get_focus()
        except Exception:
            focus = None
        if focus is attach.terminal or self._is_text_input_focus(focus):
            return False
        attach.prepare_for_live_input()
        attach.scroll_to_bottom()
        attach.terminal.grab_focus()
        # GTK chooses the key-event target before this handler runs. Merely
        # moving focus therefore loses the first character (or lets the old
        # control consume it). Deliver the existing event directly to the PTY and
        # consume the original dispatch so every key arrives exactly once.
        if self.on_terminal_key_press(attach.terminal, event):
            return True
        return attach.feed_key_event(event)

    def on_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        key_name = Gdk.keyval_name(event.keyval) or ""
        if self.handle_zoom_event(event):
            return True
        if self.keymap.matches(event, "quit_app"):
            self.quit_termdeck()
            return True
        if self.keymap.matches(event, "new_tab"):
            self.new_tab()
            return True
        if self.keymap.matches(event, "close_term"):
            self.close_current_tab()
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
        return self._route_unhandled_key_to_terminal(event)


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
        refresh_ttl = max(1.0, float(self.settings.ui_refresh_interval_seconds))
        self.backend.pane_cache_ttl = refresh_ttl
        self.sampler = ResourceSampler(snapshot_cache_ttl=refresh_ttl)
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
        for window in self.windows:
            window.set_side_pane_position(
                self.settings.side_pane_position,
                persist=False,
            )

    def apply_side_pane_position(self, position: str) -> None:
        save_terminal_options(self.settings.path, side_pane_position=position)
        self.settings = AppSettings.load(self.settings.path)
        for window in self.windows:
            window.set_side_pane_position(self.settings.side_pane_position, persist=False)

    def apply_tabs_pane_visibility(self, visible: bool) -> None:
        save_terminal_options(self.settings.path, tabs_pane_visible=visible)
        self.settings = AppSettings.load(self.settings.path)
        for window in self.windows:
            window.set_tabs_pane_visible(
                self.settings.tabs_pane_visible,
                persist=False,
                notify=False,
            )

    def close_session_tabs(self, session_ids: set[str]) -> None:
        session_ids = set(session_ids)
        for window in list(self.windows):
            removed = False
            for session_id in session_ids:
                removed = window._remove_tab_widget(session_id) or removed
            window.side.selected_ids.difference_update(session_ids)
            if removed:
                window._renumber_visible_tabs()
            window.side.refresh()

    def window_for_tab(self, session_id: str) -> TermDeckWindow | None:
        """Return the open window that currently owns a materialised tab."""

        for window in self.windows:
            if session_id in window.tabs:
                return window
        return None

    def available_move_windows(self, record: SessionRecord) -> list[TermDeckWindow]:
        """List open destination windows, excluding the tab's current owner."""

        source = self.window_for_tab(record.id)
        source_window_id = source.window_id if source is not None else record.window_id
        destinations = [
            window
            for window in self.windows
            if window.window_id != source_window_id
        ]
        return sorted(
            destinations,
            key=lambda window: (window.window_title.casefold(), window.window_id),
        )

    def _refresh_tab_management_views(self) -> None:
        refreshed: set[int] = set()
        for window in self.windows:
            for auxiliary in (window.master_window, window.window_overview):
                if auxiliary is None or id(auxiliary) in refreshed:
                    continue
                try:
                    if auxiliary.get_visible():
                        auxiliary.refresh()
                        refreshed.add(id(auxiliary))
                except Exception:
                    continue

    def move_tab_to_window(
        self,
        session_id: str,
        destination: TermDeckWindow,
    ) -> SessionRecord | None:
        """Move one tab to an open window without restarting its tmux task."""

        if not any(window is destination for window in self.windows):
            return None
        record = self.store.get(session_id)
        if record is None:
            return None
        source = self.window_for_tab(session_id)
        if source is destination or (source is None and record.window_id == destination.window_id):
            return record

        result = move_records(
            self.store,
            [record],
            window_id=destination.window_id,
            window_title=destination.window_title,
            visible=True,
        )
        if not result:
            return None
        updated = self.store.get(session_id)
        if updated is None:
            return None

        tab = source.tabs.pop(session_id, None) if source is not None else None
        if tab is not None:
            page = source.notebook.page_num(tab.widget)
            if page >= 0:
                source.notebook.remove_page(page)
            tab.record = updated
            tab.widget.rebind_window_handlers(
                key_press_handler=destination.on_terminal_key_press,
                live_focus_handler=lambda r=updated: destination._restore_live_terminal(r),
                scroll_handler=lambda lines, r=updated: destination._scroll_terminal(r, lines),
            )
            tab.widget.apply_window_theme(
                self.theme,
                font_override=destination.zoomed_font(),
            )
            destination.tabs[session_id] = tab
            position = min(max(0, updated.tab_index), destination.notebook.get_n_pages())
            destination.notebook.insert_page(
                tab.widget,
                destination._build_tab_label(updated),
                position,
            )
            destination.notebook.set_tab_reorderable(tab.widget, True)
            destination.notebook.set_current_page(destination.notebook.page_num(tab.widget))
            tab.widget.show_all()
            tab.widget.prepare_for_live_input()
            tab.widget.terminal.grab_focus()
        else:
            # A manager row may represent a hidden live tab or a stopped tab.
            # Reattach only when the tmux session still exists; never recreate it.
            destination.open_session(
                updated,
                restart_missing=False,
                remember_layout=False,
                refresh_side=False,
            )
            reopened = destination.tabs.get(session_id)
            if reopened is not None:
                position = min(max(0, updated.tab_index), destination.notebook.get_n_pages() - 1)
                destination.notebook.reorder_child(reopened.widget, position)

        if source is not None:
            source._renumber_visible_tabs()
            source.side.refresh()
        destination._renumber_visible_tabs()
        destination.side.refresh()
        destination.present()
        if session_id in destination.tabs:
            destination._focus_session(session_id)
            destination._queue_current_terminal_geometry_sync()
            detail = f"Moved {updated.name} here"
            kind = "success"
        else:
            detail = f"Moved {updated.name} here · session is stopped"
            kind = "info"
        destination.show_toast(detail, kind=kind)
        self._refresh_tab_management_views()
        return self.store.get(session_id)

    def terminate_records(self, records: list[SessionRecord]) -> int:
        unique = {record.id: record for record in records}
        if not unique:
            return 0
        for record in unique.values():
            self.backend.terminate(record)
            self.store.update(record.id, status="stopped", visible=False, last_pid=None)
        self.close_session_tabs(set(unique))
        return len(unique)

    def _show_error(self, title: str, detail: str) -> None:
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def create_named_window(
        self,
        title: str,
        *,
        initial_directory: str | Path | None = None,
        open_initial_tab: bool = True,
    ) -> TermDeckWindow:
        """Create one named window and optionally open its first shell immediately."""

        clean_title = title.strip() or f"Window {len(self.windows) + 1}"
        win = TermDeckWindow(
            self,
            window_title=clean_title,
            restore_records=[],
        )
        self.windows.append(win)
        win.present()
        if open_initial_tab:
            win.new_shell_tab(initial_directory or Path.home())
        return win

    def prompt_for_new_window(
        self,
        *,
        parent: Gtk.Window | None = None,
        first_run: bool = False,
        open_initial_tab: bool = True,
    ) -> TermDeckWindow | None:
        """Prompt for a window name and optionally open its first shell."""

        default_title = f"Window {len(self.windows) + 1}"
        dialog = WindowNameDialog(
            parent,
            title="Create your first TermDeck window" if first_run else "New TermDeck window",
            initial=default_title,
            confirm_label="Create window",
            description=(
                "A window groups related terminal tabs. Its first shell opens immediately; "
                "use cd inside the terminal."
                if open_initial_tab
                else "A window groups related terminal tabs. The selected tab will move into it."
            ),
        )
        if parent is None:
            dialog.set_application(self)
        response = dialog.run()
        title = dialog.value()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not title:
            return None
        # Interactive creation follows the same contract as New tab: its first
        # shell starts in the user's home directory. Relocation creates an empty
        # destination so the moved live terminal is its only tab.
        return self.create_named_window(
            title,
            initial_directory=Path.home(),
            open_initial_tab=open_initial_tab,
        )

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
            text=f"Confirm {plan.action} for {plan.count} tab(s)?",
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
        )
        self.windows.append(win)
        for tab_index, tab in enumerate(recipe_window.tabs):
            command = render_command(tab.command, cwd=tab.cwd)
            record = self.store.create(
                name=tab.name,
                tag=tab.tag,
                command=command,
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
                win.open_session(
                    record,
                    restart_missing=True,
                    remember_layout=False,
                    known_live=True,
                    known_pid=pid,
                    known_pid_resolved=True,
                    refresh_side=False,
                )
        win.side.refresh()
        return win

    def open_workspace_recipe(self, path: str | Path) -> int:
        recipe = load_recipe(path)
        opened = 0
        for index, recipe_window in enumerate(recipe.windows, start=1):
            self._open_recipe_window(recipe_window, recipe.name, index)
            opened += 1
        return opened

    def quit_preserving_sessions(self) -> None:
        """Close every TermDeck window while leaving tmux sessions alive."""

        for window in list(self.windows):
            for tab in list(window.tabs.values()):
                tab.widget.detach()
            try:
                window.destroy()
            except Exception:
                pass
        self.windows.clear()
        self.quit()

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
        reconcile_store(
            self.store,
            backend=self.backend,
            sampler=self.sampler,
            hide_missing=True,
            resource_sample_limit=UI_RESOURCE_REFRESH_BUDGET,
        )

        # Drop destroyed windows so a single-instance re-activate does not skip
        # restore while holding stale ApplicationWindow references.
        live_windows = []
        for window in self.windows:
            try:
                if window.get_realized():
                    live_windows.append(window)
            except Exception:
                continue
        self.windows = live_windows

        if not self.windows and self.workspace_path is not None:
            try:
                self.open_workspace_recipe(self.workspace_path)
            except WorkspaceRecipeError as exc:
                self._show_error("Could not open saved window layout", str(exc))
            if self.windows:
                self._present_all_windows()
                return

        if not self.windows:
            groups = bounded_startup_groups(self.store.windows_with_visible_sessions())
            if groups:
                for window_id, window_title, records in groups:
                    win = TermDeckWindow(
                        self,
                        window_id=window_id,
                        window_title=window_title,
                        restore_records=records,
                    )
                    self.windows.append(win)
            else:
                self.prompt_for_new_window(first_run=True)
                return
        self._present_all_windows()

    def _present_all_windows(self) -> None:
        """Raise every restored named window, not only the last one created."""

        for window in self.windows:
            try:
                window.present()
            except Exception:
                continue


def run(db_path: str | Path | None = None, workspace_path: str | Path | None = None) -> int:
    app = TermDeckApplication(db_path=db_path, workspace_path=workspace_path)
    # The argparse CLI has already consumed TermDeck-specific flags. Passing
    # them to Gtk.Application a second time makes GTK reject options such as
    # --db and --workspace, so expose only the executable name here.
    return app.run([sys.argv[0]])
