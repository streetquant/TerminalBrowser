from __future__ import annotations

import hashlib
import os
import pty
import re
import shlex
import subprocess
import sys
import termios
import tempfile
import threading
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import gi  # type: ignore

# Pin GTK3 stack versions before any repository import. On systems that ship both
# Gdk 3.0 and 4.0, an unversioned Gdk import defaults to 4.0 and then breaks Gtk 3.0.
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # type: ignore  # noqa: E402

from .capacity import (
    MASTER_ROW_LIMIT,
    SIDE_PANE_ROW_LIMIT,
    UI_RESOURCE_REFRESH_BUDGET,
    bounded_registry_records,
)
from .captain import build_captain_envelope
from .codex_archive import LegacyRecoveryResult, repair_legacy_recovery
from .codex_auth import CodexAuthError, CodexAuthManager, CodexAuthState, CodexSwitchResult
from .command_palette import PaletteItem, build_palette_items, search_palette
from .control_plan import ControlPlan, build_control_plan, render_control_plan_text
from .frame_budget import FrameBudgetMonitor, hud_enabled, install_glib_probe
from .gateway_models import (
    GatewayModelStatus,
    gateway_model_status as classify_gateway_model_status,
    process_start_epoch,
    record_uses_gateway_models,
)
from .health import HealthPolicy, classify_session, health_badge, should_pause_for_attention
from .history_control import ScrollDisposition, ScrollOutcome, unhandled_scroll
from .liveness import merge_confirmed_pane_snapshot
from .master_tasks import filter_rows
from .models import SessionRecord, TerminalTheme
from .persistence import PersistenceResult, persist_live_sessions
from .redaction import redact_text
from .resources import ResourceSampler
from .session_lifecycle import archive_records, hide_records, move_records, pin_records, recently_closed_records, rename_record, reopen_closed_records, unpin_records
from .session_reconcile import reconcile_store
from .session_search import search_sessions
from .session_recipe import (
    SessionRecipeError,
    automatic_recovery_allowed,
    recipe_from_record,
)
from .session_store import DEFAULT_DB_PATH, SessionStore, short_id, utc_now
from .settings import AppSettings, SettingsError, save_terminal_options
from .shortcuts import build_shortcut_map, index_from_key_name, shortcut_entries
from .task_snapshot import build_task_snapshot
from .pane_stream import (
    PaneStreamController,
    bind_transport_to_parent_death,
    terminate_orphan_control_clients,
)
from .tmux_control import ClientState, ControlEvent, ControlModeClient
from .command_render import default_shell, directory_tab_name, normalise_directory, render_command
from .window_snapshot import build_window_snapshots
from .terminator_config import load_terminator_theme
from .tmux_server_lifecycle import process_exit_requires_tmux_isolation
from .tmux_backend import (
    AuthActivationResult,
    AuthTransactionBusy,
    TmuxBackend,
    TmuxError,
    auth_transaction_lock,
    execute_auth_change,
)
from .ansi_contrast import SgrContrastFilter
from .ui_theme import (
    apply_terminal_palette,
    ensure_legible_theme,
    greyscale_contrast_overrides,
    osc4_sequence,
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
from .vte_stack import merge_vte_attach_environment
from .workspace_recipe import RecipeWindow, WorkspaceRecipeError, load_recipe


# The worker thread can briefly outrun GTK during a repaint or compositor stall.
# Beyond this finite backlog, retaining an arbitrary suffix would corrupt VTE's
# terminal state; reconnecting the control transport gives us an authoritative
# tmux replay while leaving the persistent pane/process untouched.
_MAX_STREAM_OUTPUT_BUFFER_BYTES = 16 * 1024 * 1024
_INPUT_REJECTED_MESSAGE = (
    b"\r\n\x1b[1;31m[T1000 input buffer full; this payload was not sent]\x1b[0m\r\n"
)
# Scrollbar drags can generate dozens of deltas. Two workers are enough to hide
# tmux round-trip latency while a finite admission bound prevents a large tab
# fleet from turning one drag into an unbounded executor queue.
_HISTORY_SCROLL_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="termdeck-history",
)
_HISTORY_SCROLL_ADMISSION = threading.BoundedSemaphore(value=8)


def _usable_glib_source_id(source_id) -> bool:
    return isinstance(source_id, int) and not isinstance(source_id, bool) and source_id > 0


def _private_resize_handler(backend, record):
    """Build a worker-safe resize callback from immutable row identity.

    ``PaneStreamController`` invokes this callback on its ordered worker.
    SessionStore's SQLite connection is GTK-thread-affine, so the callback must
    never resolve the row later. ``auth_scope_id`` and ``codex_home`` are
    immutable identity fields in SessionStore; capture them once while the
    terminal is constructed on the GTK thread.
    """

    propagate_resize = getattr(backend, "propagate_terminal_resize", None)
    codex_home = str(getattr(record, "codex_home", "") or "").strip()
    auth_scope_id = str(getattr(record, "auth_scope_id", "") or "").strip()
    if not callable(propagate_resize) or not codex_home or not auth_scope_id:
        return None

    def resize_applied(pane_root_pid: int, _columns: int, _rows: int):
        return propagate_resize(
            pane_root_pid,
            codex_home=codex_home,
            auth_scope_id=auth_scope_id,
        )

    return resize_applied


def _schedule_glib_callback(callback, *args, delay_ms: int = 0) -> tuple[bool, str]:
    """Schedule one main-context callback with an independent source fallback."""

    delay_ms = max(0, int(delay_ms))
    errors: list[str] = []
    try:
        if delay_ms:
            source_id = GLib.timeout_add(delay_ms, callback, *args)
        else:
            source_id = GLib.idle_add(callback, *args)
        if _usable_glib_source_id(source_id):
            return True, ""
        errors.append(f"primary scheduler returned unusable source id {source_id!r}")
    except Exception as exc:
        errors.append(f"primary scheduler failed: {exc}")

    source = None
    try:
        source = GLib.timeout_source_new(delay_ms) if delay_ms else GLib.idle_source_new()
        source.set_callback(callback, *args)
        source_id = source.attach(GLib.MainContext.default())
        if _usable_glib_source_id(source_id):
            return True, ""
        errors.append(f"fallback scheduler returned unusable source id {source_id!r}")
    except Exception as exc:
        errors.append(f"fallback scheduler failed: {exc}")
    if source is not None:
        try:
            source.destroy()
        except Exception:
            pass
    return False, "; ".join(errors)


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


def _install_notebook_end_action(notebook: Gtk.Notebook, widget: Gtk.Widget) -> None:
    """Install a permanent tab-strip action outside notebook overflow."""

    widget.set_no_show_all(False)
    notebook.set_action_widget(widget, Gtk.PackType.END)
    widget.show_all()


# ---------------------------------------------------------------------------
# Modifier-only key guard
# ---------------------------------------------------------------------------
# Keyvals that carry no printable input and must NOT exit the scroll view when
# pressed.  Covers every common modifier key across X11 and Wayland layouts.
_MODIFIER_ONLY_KEYVALS: frozenset[int] = frozenset((
    Gdk.KEY_Shift_L,    Gdk.KEY_Shift_R,
    Gdk.KEY_Control_L,  Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L,      Gdk.KEY_Alt_R,
    Gdk.KEY_Meta_L,     Gdk.KEY_Meta_R,
    Gdk.KEY_Super_L,    Gdk.KEY_Super_R,   # Windows / Command key
    Gdk.KEY_Hyper_L,    Gdk.KEY_Hyper_R,
    Gdk.KEY_Caps_Lock,
    Gdk.KEY_Num_Lock,
    Gdk.KEY_Scroll_Lock,
    Gdk.KEY_ISO_Level3_Shift,              # AltGr
    Gdk.KEY_ISO_Level5_Shift,
    Gdk.KEY_Mode_switch,
    Gdk.KEY_Multi_key,
))


def _is_modifier_only_keyval(keyval: int) -> bool:
    """Return True when *keyval* is a bare modifier key with no text payload."""
    return keyval in _MODIFIER_ONLY_KEYVALS


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
    # SOTA-terminal minimum contrast: never let a palette pack or an imported
    # Terminator theme render invisible ANSI text on its own background.
    theme = ensure_legible_theme(theme)
    if theme.use_system_font and not font_override:
        pass
    else:
        font = font_override or theme.font
        terminal.set_font(Pango.FontDescription(font))
    terminal.set_scrollback_lines(theme.scrollback_lines)
    palette = [rgba(colour) for colour in theme.palette]
    terminal.set_colors(rgba(theme.foreground_color, "#dfe6f2"), rgba(theme.background_color, "#0a0d13"), palette)
    indexed_overrides = greyscale_contrast_overrides(theme)
    if indexed_overrides:
        # Lift near-invisible xterm-256 greys (dim TUI text) as well; the
        # 16-slot palette above cannot reach indexed colors 16-255.
        terminal.feed(osc4_sequence(indexed_overrides))
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
        selection_history_handler=None,
        attach_exit_handler=None,
        attach_stable_handler=None,
        pane_dead_handler=None,
        scrollback_extent: int | None = None,
        font_override: str | None = None,
        child_environment: dict[str, str] | None = None,
        prepare_target_handler=None,
        prepare_target_error_handler=None,
        resize_applied_handler=None,
        repair_on_setup: bool = True,
        connect_enabled: bool = True,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.argv = argv
        self.cwd = cwd
        self.theme = theme
        self.controller: PaneStreamController | None = None
        self.pty_obj = None
        self._loopback_fd = -1
        self._loopback_watch_id = 0
        self._loopback_channel = None
        self._detached = False
        self._terminally_stopped = False
        self._attach_restart_deferred = False
        self._connect_source_id = 0
        self._connect_fallback_source_id = 0
        # Geometry-only recipe probes reuse this exact VTE/scrollbar layout but
        # must never create a control transport or tmux target.
        self._connect_enabled = bool(connect_enabled)
        # Notebook selection can precede the delayed stream connect.
        # Latch that demand so the visible tab cannot lose queue priority.
        self._setup_priority_requested = False
        self._attach_stable_source_id = 0
        self._resize_sync_source_id = 0
        self._cursor_hide_source_id = 0
        self._scroll_idle_source_id = 0
        self._last_allocated_grid = (0, 0)
        self._last_synced_grid = (0, 0)
        self._live_focus_handler = None
        self._scroll_handler = None
        self._selection_history_handler = selection_history_handler
        self._attach_exit_handler = attach_exit_handler
        self._attach_stable_handler = attach_stable_handler
        self._pane_dead_handler = pane_dead_handler
        # A GUI-owned missing tmux target is created only after VTE has a
        # settled allocation. This prevents full-screen TUIs such as Codex from
        # drawing their first splash at tmux's implicit 80x24 and then being
        # resized underneath that frame.
        self._prepare_target_handler = prepare_target_handler
        self._prepare_target_error_handler = prepare_target_error_handler
        self._resize_applied_handler = resize_applied_handler
        self._repair_on_setup = bool(repair_on_setup)
        self._child_environment = merge_vte_attach_environment(child_environment)
        self._scrollback_extent = int(scrollback_extent or theme.scrollback_lines)
        self._key_press_handler_id = 0
        self._key_press_handler = None
        self._history_cursor_hidden = False
        self._clipboard_image_paths: set[str] = set()
        self._output_lock = threading.Lock()
        self._output_buffer = bytearray()
        self._output_drain_scheduled = False
        self._stream_output_overflowed = False
        self._stream_recovery_scheduled = False
        self._last_stream_scheduling_error = ""
        self._input_rejection_reported = False
        # CONNECTED is an ordered emulator-reset marker. It shares the output
        # drain with replay bytes so a late GTK idle can never erase a screen
        # that the setup worker already reconstructed.
        self._stream_reset_pending = False
        self._stream_generation = 0
        # Programs that choose absolute colours (Codex styles headings
        # near-black when it mis-detects a dark terminal as light) cannot
        # be reached by the VTE palette, so pane bytes pass through a
        # contrast floor on the way in.
        self._contrast_filter = SgrContrastFilter(theme)
        self.terminal = Vte.Terminal()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        # Content breathing room like modern terminal apps. Widget margins are
        # used instead of CSS padding because VTE paints over its CSS box; the
        # surrounding sunken surface matches the default terminal background,
        # so the inset reads as seamless padding.
        self.terminal.set_margin_start(10)
        self.terminal.set_margin_end(4)
        self.terminal.set_margin_top(8)
        self.terminal.set_margin_bottom(4)
        apply_theme(self.terminal, theme, font_override=font_override)
        self.terminal.set_scrollback_lines(self._scrollback_extent)
        # Normal terminal review and VTE text selection have different history
        # owners.  The persistent scrollbar represents the application-aware
        # tmux/Codex history controller; VTE's native adjustment is retained
        # separately and is bound to the bar only while a real VTE selection is
        # active.  Keeping the two coordinate systems distinct prevents both
        # deep-drag blanking and false "live edge" detection.
        self.terminal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.terminal_row.set_hexpand(True)
        self.terminal_row.set_vexpand(True)
        self.native_adjustment = self.terminal.get_vadjustment()
        extent = float(max(100, int(scrollback_extent or theme.scrollback_lines)))
        page_size = min(24.0, max(1.0, extent - 1.0))
        self.history_adjustment = Gtk.Adjustment.new(
            extent,
            0.0,
            extent + page_size,
            3.0,
            page_size,
            page_size,
        )
        self._history_scrollbar_syncing = False
        self._last_history_scrollbar_value = float(self.history_adjustment.get_value())
        self._history_drag_last_requested = self._last_history_scrollbar_value
        self._scrollbar_native_bound = False
        self._history_request_lock = threading.Lock()
        self._history_scroll_generation = 0
        self._history_scroll_sequence = 0
        self._history_last_applied_sequence = 0
        self._pending_history_delta = 0
        self._history_worker_active = False
        self._history_worker_generation: int | None = None
        self._pending_history_apply = None
        self._history_apply_scheduled = False
        self.scrollbar = Gtk.Scrollbar.new(
            Gtk.Orientation.VERTICAL,
            adjustment=self.history_adjustment,
        )
        _style(self.scrollbar, "td-terminal-scrollbar")
        self.scrollbar.get_style_context().remove_class("overlay-indicator")
        self.scrollbar.set_size_request(16, -1)
        self.scrollbar.set_hexpand(False)
        self.scrollbar.set_vexpand(True)
        self.scrollbar.set_opacity(1.0)
        self.history_adjustment.connect(
            "value-changed",
            self._on_history_scrollbar_value_changed,
        )
        # Do not let VTE synthesize Up/Down fallback keys for an application
        # history gesture. Alternate-screen applications that negotiate mouse
        # reporting still receive their own wheel protocol through VTE.
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
            selection_history_handler=selection_history_handler,
            attach_exit_handler=attach_exit_handler,
            attach_stable_handler=attach_stable_handler,
            pane_dead_handler=pane_dead_handler,
        )
        # Select-to-copy: run *after* VTE handles the release so the selection exists.
        self.terminal.connect_after("button-release-event", self._on_button_release)
        self.terminal.connect("selection-changed", self._on_selection_changed)
        self.terminal.connect("button-press-event", self._on_button_press)
        self.terminal.connect("scroll-event", self._on_terminal_scroll)
        self.terminal.connect("size-allocate", self._on_terminal_size_allocate)
        self.connect("destroy", lambda *_: self.detach())
        self._selection_pending = False
        self._selection_autocopy_source_id = 0
        # Last select-to-copy payload — used when pasting back into this terminal
        # (VTE paste_* can miss text we put on the clipboard ourselves).
        self._last_copied_text = ""
        self.terminal_row.pack_start(self.terminal, True, True, 0)
        self.terminal_row.pack_start(self.scrollbar, False, True, 0)
        self.pack_start(self.terminal_row, True, True, 0)
        # The pane connects after the first stable size-allocate so tmux sees
        # the final VTE grid from frame 1. This fallback covers widgets that
        # never receive an allocation (e.g. hidden restored tabs). A geometry-
        # only recipe probe deliberately owns no fallback or control transport.
        if self._connect_enabled:
            self._connect_fallback_source_id = GLib.timeout_add(
                500,
                self._connect_fallback,
            )

    def rebind_window_handlers(
        self,
        *,
        key_press_handler,
        live_focus_handler,
        scroll_handler=None,
        selection_history_handler=None,
        attach_exit_handler=None,
        attach_stable_handler=None,
        pane_dead_handler=None,
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
        self._key_press_handler = key_press_handler
        self._live_focus_handler = live_focus_handler
        self._scroll_handler = scroll_handler
        self._selection_history_handler = selection_history_handler
        self._attach_exit_handler = attach_exit_handler
        self._attach_stable_handler = attach_stable_handler
        if pane_dead_handler is not None:
            self._pane_dead_handler = pane_dead_handler

    def apply_window_theme(self, theme: TerminalTheme, *, font_override: str) -> None:
        self.theme = theme
        self._contrast_filter.set_theme(theme)
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
                self.terminal.set_color_cursor(rgba(self.theme.background_color, "#0a0d13"))
                self.terminal.set_color_cursor_foreground(rgba(self.theme.foreground_color, "#dfe6f2"))
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

    def _cancel_pending_connect(self) -> None:
        for attr in (
            "_connect_source_id",
            "_connect_fallback_source_id",
            "_attach_stable_source_id",
            "_resize_sync_source_id",
            "_cursor_hide_source_id",
            "_scroll_idle_source_id",
        ):
            self._remove_glib_source(getattr(self, attr, 0))
            setattr(self, attr, 0)

    def _on_terminal_size_allocate(self, _terminal, _allocation) -> None:
        columns = max(0, int(self.terminal.get_column_count()))
        rows = max(0, int(self.terminal.get_row_count()))
        self._last_allocated_grid = (columns, rows)
        if self.controller is not None:
            # VTE updates its character grid as part of allocation. Read it
            # again after GTK finishes the frame so a Paned/window expansion
            # cannot leave tmux at the previous, smaller grid.
            self.request_geometry_sync()
            return
        if (
            not bool(getattr(self, "_connect_enabled", True))
            or self._detached
            or self._terminally_stopped
            or self._attach_restart_deferred
            or columns < 2
            or rows < 2
        ):
            return
        self._remove_glib_source(self._connect_source_id)
        self._connect_source_id = GLib.timeout_add(60, self._connect_after_stable_allocation)

    def _connect_after_stable_allocation(self) -> bool:
        self._connect_source_id = 0
        if (
            not bool(getattr(self, "_connect_enabled", True))
            or self.controller is not None
            or self._detached
            or self._terminally_stopped
            or self._attach_restart_deferred
        ):
            return False
        columns, rows = self._last_allocated_grid
        if columns < 2 or rows < 2:
            return False
        self._connect_stream(columns=columns, rows=rows)
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
        if self._detached or self.controller is None:
            return False
        columns = max(0, int(self.terminal.get_column_count()))
        rows = max(0, int(self.terminal.get_row_count()))
        self._last_allocated_grid = (columns, rows)
        self._sync_stream_geometry(columns=columns, rows=rows, force=True)
        return False

    def apply_canonical_grid(self, *, columns: int, rows: int) -> None:
        """Apply one settled window grid to this VTE and its tmux client."""

        columns = max(0, int(columns))
        rows = max(0, int(rows))
        if self._detached or columns < 2 or rows < 2:
            return
        grid = (columns, rows)
        if grid == self._last_allocated_grid and grid == self._last_synced_grid:
            return
        try:
            self.terminal.set_size(columns, rows)
        except Exception:
            pass
        else:
            self._last_allocated_grid = grid
        self._sync_stream_geometry(columns=columns, rows=rows, force=True)

    def _connect_fallback(self) -> bool:
        self._connect_fallback_source_id = 0
        if (
            not bool(getattr(self, "_connect_enabled", True))
            or self.controller is not None
            or self._detached
            or self._terminally_stopped
            or self._attach_restart_deferred
        ):
            return False
        columns = max(2, int(self.terminal.get_column_count() or 80))
        rows = max(2, int(self.terminal.get_row_count() or 24))
        self._connect_stream(columns=columns, rows=rows)
        return False

    def _ensure_loopback_pty(self) -> None:
        """Give VTE a loopback PTY so its input side has a real terminal.

        No child process runs on the slave. Everything VTE writes to the
        master (keystrokes, bracketed paste, DA/XTVERSION probe replies) is
        read back off the slave and forwarded to the tmux pane through the
        control-mode client — exactly the bytes a directly attached terminal
        would send. Nothing is ever written to the slave: pane output goes
        through ``terminal.feed`` so the PTY stays a one-way input channel.
        """

        if self.pty_obj is not None:
            return
        master_fd, slave_fd = pty.openpty()
        tty.setraw(slave_fd, when=termios.TCSANOW)
        os.set_blocking(slave_fd, False)
        self._loopback_fd = slave_fd
        self.pty_obj = Vte.Pty.new_foreign_sync(master_fd, None)
        self.terminal.set_pty(self.pty_obj)
        # Keep a Python reference to the channel; GLib does not extend its
        # lifetime past the watch registration.
        self._loopback_channel = GLib.IOChannel.unix_new(slave_fd)
        self._loopback_watch_id = GLib.io_add_watch(
            self._loopback_channel,
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_loopback_readable,
        )

    def _on_loopback_readable(self, _channel, condition) -> bool:
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self._loopback_watch_id = 0
            return False
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(self._loopback_fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                self._loopback_watch_id = 0
                return False
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunk) < 65536:
                break
        data = b"".join(chunks)
        if data and self.controller is not None and not self._detached:
            try:
                accepted = self.controller.send_bytes(data)
            except Exception:
                accepted = False
            if accepted is False:
                if not self._input_rejection_reported:
                    self._input_rejection_reported = True
                    try:
                        self.terminal.feed(_INPUT_REJECTED_MESSAGE)
                    except Exception:
                        pass
            elif accepted is True:
                self._input_rejection_reported = False
        return True

    def _sync_stream_geometry(self, *, columns: int, rows: int, force: bool = False) -> None:
        if columns < 2 or rows < 2:
            return
        grid = (columns, rows)
        if grid == self._last_synced_grid and not force:
            return
        self._last_synced_grid = grid
        if self.pty_obj is not None:
            try:
                self.pty_obj.set_size(rows, columns)
            except Exception:
                pass
        controller = self.controller
        if controller is not None:
            try:
                controller.resize(columns=columns, rows=rows)
            except Exception:
                pass

    def _connect_stream(self, *, columns: int, rows: int) -> None:
        if (
            not bool(getattr(self, "_connect_enabled", True))
            or self.controller is not None
            or self._detached
            or self._terminally_stopped
            or self._attach_restart_deferred
        ):
            return
        self._remove_glib_source(self._connect_source_id)
        self._remove_glib_source(self._connect_fallback_source_id)
        self._connect_source_id = 0
        self._connect_fallback_source_id = 0
        prepare_target = getattr(self, "_prepare_target_handler", None)
        if prepare_target is not None:
            try:
                prepared = prepare_target(columns, rows)
                if prepared is False:
                    raise RuntimeError("terminal target preparation was refused")
            except Exception as exc:
                self._terminally_stopped = True
                error_handler = getattr(self, "_prepare_target_error_handler", None)
                if callable(error_handler):
                    try:
                        error_handler(exc)
                    except Exception:
                        pass
                return
            # The target now exists at this exact grid. Never execute the
            # potentially stateful launch callback a second time on resize or
            # reconnect; subsequent geometry changes belong to the controller.
            self._prepare_target_handler = None
            self._prepare_target_error_handler = None
        self._ensure_loopback_pty()
        self._sync_stream_geometry(columns=columns, rows=rows, force=True)
        self._stream_generation += 1
        generation = self._stream_generation
        self.controller = PaneStreamController(
            attach_argv=list(self.argv),
            environment=dict(self._child_environment),
            cwd=self.cwd,
            on_output=lambda data: self._on_stream_output(generation, data),
            on_pane_dead=lambda: self._on_stream_pane_dead(generation),
            on_state_change=lambda state: self._on_stream_state(generation, state),
            on_resize_applied=getattr(self, "_resize_applied_handler", None),
            repair_on_setup=bool(getattr(self, "_repair_on_setup", True)),
            history_lines=self._scrollback_extent,
        )
        self._contrast_filter.reset()
        if getattr(self, "_setup_priority_requested", False):
            # ``switch-page`` commonly runs before allocation creates
            # the controller. Apply its latched priority before start,
            # so the first setup enters the gate ahead of background tabs.
            try:
                self.controller.prioritize_setup()
            except Exception:
                # Priority is a latency optimization; controller startup is the
                # liveness boundary and must continue if promotion ever fails.
                pass
        self.controller.start(columns=columns, rows=rows)

    def _remember_stream_scheduling_error(self, error: str) -> None:
        self._last_stream_scheduling_error = str(error or "")[-2000:]

    def _request_stream_output_drain(self, generation: int) -> bool:
        """Schedule the one ordered GTK drain without leaving a stuck latch."""

        if generation != self._stream_generation or self._detached:
            return False
        with self._output_lock:
            if generation != self._stream_generation or self._detached:
                return False
            if self._output_drain_scheduled:
                return True
            if not self._output_buffer and not self._stream_reset_pending:
                return True
            self._output_drain_scheduled = True
        scheduled, error = _schedule_glib_callback(
            self._drain_stream_output,
            generation,
        )
        if scheduled:
            self._remember_stream_scheduling_error("")
            return True
        with self._output_lock:
            if generation == self._stream_generation:
                self._output_drain_scheduled = False
        self._remember_stream_scheduling_error(error)
        return False

    def _request_stream_overflow_recovery(self, generation: int) -> bool:
        """Schedule one control-transport reconnect after a GTK backlog overflow."""

        if generation != self._stream_generation or self._detached:
            return False
        with self._output_lock:
            if generation != self._stream_generation or self._detached:
                return False
            if self._stream_recovery_scheduled:
                return True
            self._stream_recovery_scheduled = True
        scheduled, error = _schedule_glib_callback(
            self._recover_stream_output_overflow,
            generation,
        )
        if scheduled:
            self._remember_stream_scheduling_error("")
            return True
        with self._output_lock:
            if generation == self._stream_generation:
                self._stream_recovery_scheduled = False
        self._remember_stream_scheduling_error(error)
        return False

    def _recover_stream_output_overflow(self, generation: int) -> bool:
        """Reconnect only the client transport; the persistent tmux pane survives."""

        with self._output_lock:
            self._stream_recovery_scheduled = False
            if (
                generation != self._stream_generation
                or self._detached
                or not self._stream_output_overflowed
            ):
                return False
            self._stream_output_overflowed = False
            self._output_buffer.clear()
            self._output_drain_scheduled = False
            self._stream_reset_pending = False
        if self._terminally_stopped or self._attach_restart_deferred:
            return False
        self._teardown_stream()
        if self._detached or self._terminally_stopped or self._attach_restart_deferred:
            return False
        columns = max(2, int(self.terminal.get_column_count() or 80))
        rows = max(2, int(self.terminal.get_row_count() or 24))
        self._connect_stream(columns=columns, rows=rows)
        return False

    def _on_stream_output(self, generation: int, data: bytes) -> None:
        # Runs on a PaneStreamController worker thread. Coalesce bursts under
        # the lock and schedule at most one main-loop drain at a time.
        if generation != self._stream_generation or self._detached or not data:
            return
        overflowed = False
        with self._output_lock:
            if self._stream_output_overflowed:
                overflowed = True
            elif len(self._output_buffer) + len(data) > _MAX_STREAM_OUTPUT_BUFFER_BYTES:
                # An arbitrary suffix cannot be fed safely into a stateful
                # terminal emulator. Drop the incomplete backlog and reconnect
                # the client so tmux can replay an authoritative screen.
                self._output_buffer.clear()
                self._stream_output_overflowed = True
                overflowed = True
            else:
                self._output_buffer.extend(data)
        if overflowed:
            self._request_stream_overflow_recovery(generation)
        else:
            self._request_stream_output_drain(generation)

    def _drain_stream_output(self, generation: int) -> bool:
        with self._output_lock:
            overflowed = bool(getattr(self, "_stream_output_overflowed", False))
            reset_pending = bool(getattr(self, "_stream_reset_pending", False))
            self._stream_reset_pending = False
            payload = bytes(self._output_buffer)
            self._output_buffer.clear()
            self._output_drain_scheduled = False
        if generation != self._stream_generation or self._detached:
            return False
        if overflowed:
            self._request_stream_overflow_recovery(generation)
            return False
        if reset_pending:
            # Reset and replay are consumed by this one GTK callback. The
            # CONNECTED marker may be drained before or together with replay,
            # but it can never run later and erase an already-fed screen.
            try:
                self.terminal.reset(True, True)
                self._contrast_filter.reset()
            except Exception:
                pass
        if not payload:
            return False
        try:
            self.terminal.feed(self._contrast_filter.process(payload))
        except Exception:
            pass
        return False

    def _on_stream_pane_dead(self, generation: int) -> None:
        # Runs on a PaneStreamController worker thread.
        scheduled, error = _schedule_glib_callback(
            self._apply_pane_dead,
            generation,
        )
        if not scheduled:
            self._remember_stream_scheduling_error(error)

    def _apply_pane_dead(self, generation: int) -> bool:
        if generation != self._stream_generation or self._detached:
            return False
        handler = getattr(self, "_pane_dead_handler", None)
        if callable(handler):
            try:
                if handler():
                    return False
            except Exception:
                # Recovery is best effort; retain truthful terminal-exit output
                # when the owning window cannot schedule a replacement.
                pass
        try:
            self.terminal.feed(b"\r\n\x1b[2m[process exited]\x1b[0m\r\n")
        except Exception:
            pass
        return False

    def _on_stream_state(self, generation: int, state: ClientState) -> None:
        # Runs on a PaneStreamController worker thread.
        if generation != self._stream_generation or self._detached:
            return
        if state is ClientState.CONNECTED:
            with self._output_lock:
                # Controller ordering guarantees this marker is published before
                # setup can emit replay. Discard only bytes from the previous
                # connection, then serialize reset with the new replay through
                # the same GTK drain instead of racing two independent idles.
                self._output_buffer.clear()
                self._stream_reset_pending = True
                self._stream_output_overflowed = False
                self._stream_recovery_scheduled = False
            self._input_rejection_reported = False
            self._request_stream_output_drain(generation)
        scheduled, error = _schedule_glib_callback(
            self._apply_stream_state,
            generation,
            state,
        )
        if not scheduled:
            self._remember_stream_scheduling_error(error)

    def _apply_stream_state(self, generation: int, state: ClientState) -> bool:
        if generation != self._stream_generation or self._detached:
            return False
        if state is ClientState.CONNECTED:
            # Emulator reset belongs to the ordered output drain above. Retry
            # that scheduling here: a transient worker-thread scheduling
            # failure must not leave the committed replay buffered forever.
            self._request_stream_output_drain(generation)
            self._remove_glib_source(self._attach_stable_source_id)
            self._attach_stable_source_id = GLib.timeout_add(2000, self._mark_attach_stable)
        elif state is ClientState.RECONNECTING:
            self._remove_glib_source(self._attach_stable_source_id)
            self._attach_stable_source_id = 0
            self._classify_stream_interruption()
        return False

    def _classify_stream_interruption(self) -> None:
        """Decide whether a lost control-mode client should keep retrying.

        ``True`` — transient failure: the controller keeps reconnecting on
        its own. ``None`` — an auth transition owns the tmux target; pause
        every connect path until the completed transaction can classify the
        replacement exactly once. ``False`` — the session is gone for good.
        """

        should_retry: bool | None = True
        handler = getattr(self, "_attach_exit_handler", None)
        if callable(handler):
            try:
                decision = handler()
                should_retry = None if decision is None else bool(decision)
            except Exception:
                # A failed liveness check is inconclusive. Preserve the normal
                # client-only retry path instead of declaring the task dead.
                should_retry = True
        if should_retry is True:
            return
        if should_retry is None:
            self._attach_restart_deferred = True
        else:
            self._terminally_stopped = True
        self._teardown_stream()

    def _teardown_stream(self) -> None:
        controller = self.controller
        self.controller = None
        self._stream_generation += 1
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                pass
        with self._output_lock:
            self._output_buffer.clear()
            self._output_drain_scheduled = False
            self._stream_reset_pending = False
            self._stream_output_overflowed = False
            self._stream_recovery_scheduled = False
        self._input_rejection_reported = False

    def defer_attach_for_target_replacement(self) -> bool:
        """Atomically retire the old stream before its tmux target is replaced."""

        if self._detached or self._terminally_stopped:
            return False
        if self._attach_restart_deferred:
            return True
        self._attach_restart_deferred = True
        self._cancel_pending_connect()
        self._teardown_stream()
        return True

    def resolve_deferred_attach(self) -> bool:
        """Classify and resume one pane stream paused for an auth transition."""

        if (
            self._detached
            or self._terminally_stopped
            or not self._attach_restart_deferred
        ):
            return False
        handler = getattr(self, "_attach_exit_handler", None)
        try:
            decision = handler() if callable(handler) else True
        except Exception:
            # As on an ordinary interruption, an inconclusive liveness query
            # keeps the non-destructive retry path available.
            decision = True
        if decision is None:
            return False
        self._attach_restart_deferred = False
        if not bool(decision):
            self._terminally_stopped = True
            self._cancel_pending_connect()
            return False
        # ``stop()`` is terminal for a controller, so resume with a fresh one
        # sized to the current VTE grid.
        columns = max(2, int(self.terminal.get_column_count() or 80))
        rows = max(2, int(self.terminal.get_row_count() or 24))
        self._connect_stream(columns=columns, rows=rows)
        return False

    def _mark_attach_stable(self) -> bool:
        self._attach_stable_source_id = 0
        controller = self.controller
        if (
            not self._detached
            and controller is not None
            and controller.state is ClientState.CONNECTED
        ):
            handler = getattr(self, "_attach_stable_handler", None)
            if callable(handler):
                try:
                    handler()
                except Exception:
                    # A stable tmux client is already useful; window bookkeeping
                    # must not turn that success into another reconnect cycle.
                    pass
        return False

    def detach(self) -> None:
        if self._detached:
            return
        self._detached = True
        self._cancel_async_history_scroll()
        self._cancel_pending_connect()
        self._cleanup_clipboard_images()
        self._teardown_stream()
        if self._loopback_watch_id:
            self._remove_glib_source(self._loopback_watch_id)
            self._loopback_watch_id = 0
        self._loopback_channel = None
        if self._loopback_fd >= 0:
            try:
                os.close(self._loopback_fd)
            except OSError:
                pass
            self._loopback_fd = -1

    def copy_clipboard(self) -> None:
        self._copy_selection_to_clipboards()

    def paste_clipboard(self) -> None:
        """Paste CLIPBOARD (then PRIMARY / last copy) into the shell as input."""
        self._paste_from_selection(Gdk.SELECTION_CLIPBOARD, prefer_last=True)

    def paste_primary(self) -> None:
        """Paste PRIMARY selection into the shell as input (middle-click)."""
        self._paste_from_selection(Gdk.SELECTION_PRIMARY, prefer_last=True)

    def _history_bottom_value(self) -> float:
        adjustment = self.history_adjustment
        return max(
            float(adjustment.get_lower()),
            float(adjustment.get_upper()) - float(adjustment.get_page_size()),
        )

    def _native_bottom_value(self) -> float:
        adjustment = self.native_adjustment
        return max(
            float(adjustment.get_lower()),
            float(adjustment.get_upper()) - float(adjustment.get_page_size()),
        )

    def _bind_scrollbar_for_selection(self, active: bool) -> None:
        """Expose VTE's adjustment only while a real VTE selection owns review."""

        active = bool(active)
        if active == self._scrollbar_native_bound:
            return
        try:
            self.scrollbar.set_adjustment(
                self.native_adjustment if active else self.history_adjustment
            )
        except Exception:
            return
        self._scrollbar_native_bound = active

    def _set_history_scrollbar_value(self, value: float) -> None:
        adjustment = self.history_adjustment
        lower = float(adjustment.get_lower())
        upper = self._history_bottom_value()
        self._history_scrollbar_syncing = True
        try:
            adjustment.set_value(min(upper, max(lower, float(value))))
            self._last_history_scrollbar_value = float(adjustment.get_value())
            self._history_drag_last_requested = self._last_history_scrollbar_value
        finally:
            self._history_scrollbar_syncing = False

    def _set_history_depth(self, depth: int, *, extent: int | None = None) -> None:
        """Synchronize the proxy from backend/logical history depth evidence."""

        clean_depth = max(0, int(depth))
        adjustment = self.history_adjustment
        page = max(1.0, float(adjustment.get_page_size()))
        current_extent = max(0.0, float(adjustment.get_upper()) - page)
        target_extent = max(
            current_extent,
            float(clean_depth),
            float(max(0, int(extent))) if extent is not None else 0.0,
        )
        self._history_scrollbar_syncing = True
        try:
            if abs(target_extent - current_extent) > 0.5:
                adjustment.set_upper(target_extent + page)
            bottom = max(
                float(adjustment.get_lower()),
                float(adjustment.get_upper()) - float(adjustment.get_page_size()),
            )
            adjustment.set_value(
                min(bottom, max(float(adjustment.get_lower()), bottom - clean_depth))
            )
            self._last_history_scrollbar_value = float(adjustment.get_value())
            self._history_drag_last_requested = self._last_history_scrollbar_value
        finally:
            self._history_scrollbar_syncing = False

    def _sync_history_from_outcome(self, outcome: ScrollOutcome) -> None:
        if outcome.depth is None:
            return
        self._set_history_depth(outcome.depth, extent=outcome.extent)

    def _move_native_history(self, lines: int) -> None:
        """Move only VTE's retained normal-screen buffer for selection review."""

        adjustment = self.native_adjustment
        try:
            current = float(adjustment.get_value())
            lower = float(adjustment.get_lower())
            upper = max(
                lower,
                float(adjustment.get_upper()) - float(adjustment.get_page_size()),
            )
            adjustment.set_value(
                min(upper, max(lower, current + int(lines)))
            )
        except Exception:
            return

    def reset_scrollbar(self) -> None:
        """Reset the application-aware history proxy to its live endpoint."""

        self._set_history_depth(0)

    def scroll_to_bottom(self) -> None:
        """Return both application history and VTE's viewport to live output."""

        self.reset_scrollbar()
        try:
            self.native_adjustment.set_value(self._native_bottom_value())
        except Exception:
            pass

    def _cancel_async_history_scroll(self) -> None:
        """Invalidate queued/in-flight thumb work without killing backend commands."""

        with self._history_request_lock:
            self._history_scroll_generation += 1
            self._pending_history_delta = 0
            self._pending_history_apply = None
            # An already-running worker remains marked active until it observes
            # generation drift. This prevents a second worker from racing the
            # same widget while the old backend call is still returning.
            self._history_drag_last_requested = self._last_history_scrollbar_value

    def _queue_history_scroll_delta(self, delta: int) -> None:
        """Coalesce one thumb movement and keep tmux I/O off the GTK thread."""

        clean_delta = int(delta)
        if not clean_delta or self._detached:
            return
        admitted = False
        with self._history_request_lock:
            self._pending_history_delta += clean_delta
            generation = self._history_scroll_generation
            if self._history_worker_active:
                return
            admitted = _HISTORY_SCROLL_ADMISSION.acquire(blocking=False)
            if admitted:
                self._history_worker_active = True
                self._history_worker_generation = generation
            else:
                # Fail fast under global storage pressure. The proxy is restored
                # to its last applied value on GTK rather than growing an
                # unbounded executor queue.
                self._pending_history_delta = 0
                self._history_drag_last_requested = self._last_history_scrollbar_value
        if not admitted:
            _schedule_glib_callback(self._restore_history_proxy_after_failed_drag)
            return
        try:
            _HISTORY_SCROLL_EXECUTOR.submit(
                self._history_scroll_worker,
                generation,
            )
        except RuntimeError:
            _HISTORY_SCROLL_ADMISSION.release()
            with self._history_request_lock:
                if self._history_worker_generation == generation:
                    self._history_worker_active = False
                    self._history_worker_generation = None
                self._pending_history_delta = 0
            _schedule_glib_callback(self._restore_history_proxy_after_failed_drag)

    def _restore_history_proxy_after_failed_drag(self) -> bool:
        self._set_history_scrollbar_value(self._last_history_scrollbar_value)
        return False

    def _publish_async_history_scroll_outcome(
        self,
        generation: int,
        sequence: int,
        attempted_delta: int,
        outcome,
    ) -> None:
        """Keep at most one pending GTK completion per terminal widget."""

        schedule = False
        with self._history_request_lock:
            if generation != self._history_scroll_generation or self._detached:
                return
            self._pending_history_apply = (
                generation,
                sequence,
                attempted_delta,
                outcome,
            )
            if not self._history_apply_scheduled:
                self._history_apply_scheduled = True
                schedule = True
        if schedule:
            _schedule_glib_callback(self._drain_async_history_scroll_outcome)

    def _drain_async_history_scroll_outcome(self) -> bool:
        with self._history_request_lock:
            pending = self._pending_history_apply
            self._pending_history_apply = None
            self._history_apply_scheduled = False
        if pending is None:
            return False
        generation, sequence, attempted_delta, outcome = pending
        return self._apply_async_history_scroll_outcome(
            generation,
            sequence,
            attempted_delta,
            outcome,
        )

    def _history_scroll_worker(self, generation: int) -> None:
        """Serialize one admitted widget's coalesced backend navigation burst."""

        try:
            while True:
                with self._history_request_lock:
                    if self._detached:
                        self._history_worker_active = False
                        self._history_worker_generation = None
                        return
                    if generation != self._history_scroll_generation:
                        # Reuse the same admitted worker chain for the newest
                        # generation instead of queuing a replacement future.
                        generation = self._history_scroll_generation
                        self._history_worker_generation = generation
                    delta = int(self._pending_history_delta)
                    self._pending_history_delta = 0
                    if not delta:
                        self._history_worker_active = False
                        self._history_worker_generation = None
                        return
                    self._history_scroll_sequence += 1
                    sequence = self._history_scroll_sequence

                handler = getattr(self, "_scroll_handler", None)
                if not callable(handler):
                    outcome = None
                else:
                    try:
                        outcome = handler(delta)
                    except Exception:
                        outcome = None

                with self._history_request_lock:
                    stale = generation != self._history_scroll_generation or self._detached
                    terminal_outcome = (
                        not isinstance(outcome, ScrollOutcome)
                        or outcome.disposition is not ScrollDisposition.HANDLED
                        or outcome.reached_live_edge
                    )
                    if stale:
                        # Do not publish old-generation UI state. Loop once more
                        # so this same admitted worker can absorb any new delta.
                        continue
                    if terminal_outcome:
                        self._pending_history_delta = 0
                        self._history_worker_active = False
                        self._history_worker_generation = None

                self._publish_async_history_scroll_outcome(
                    generation,
                    sequence,
                    delta,
                    outcome,
                )
                if terminal_outcome:
                    return
        finally:
            _HISTORY_SCROLL_ADMISSION.release()

    def _apply_async_history_scroll_outcome(
        self,
        generation: int,
        sequence: int,
        attempted_delta: int,
        outcome,
    ) -> bool:
        """Apply a worker result only if it still belongs to this live widget."""

        with self._history_request_lock:
            if generation != self._history_scroll_generation or self._detached:
                return False
            if sequence <= self._history_last_applied_sequence:
                return False
            self._history_last_applied_sequence = sequence
        if not isinstance(outcome, ScrollOutcome):
            self._set_history_scrollbar_value(self._last_history_scrollbar_value)
            return False
        if outcome.disposition is ScrollDisposition.PASSTHROUGH:
            # A physical wheel can be passed to a TUI; a scrollbar thumb cannot.
            # Revert the proxy without synthesizing child input.
            self._set_history_scrollbar_value(self._last_history_scrollbar_value)
            if self._history_cursor_hidden:
                self._set_history_cursor_hidden(False)
            return False
        if outcome.disposition is not ScrollDisposition.HANDLED:
            if outcome.reached_live_edge:
                self._set_history_depth(0)
            else:
                self._set_history_scrollbar_value(self._last_history_scrollbar_value)
            return False
        self._sync_history_from_outcome(outcome)
        if outcome.reached_live_edge:
            self.prepare_for_live_input()
            return False
        self._set_history_cursor_hidden(True)
        self._arm_scroll_idle_resume()
        return False

    def _on_history_scrollbar_value_changed(self, adjustment) -> None:
        if self._history_scrollbar_syncing or self._scrollbar_native_bound:
            return
        requested = float(adjustment.get_value())
        previous_requested = float(self._history_drag_last_requested)
        delta = int(round(requested - previous_requested))
        self._history_drag_last_requested = requested
        if not delta:
            return
        self._queue_history_scroll_delta(delta)

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

    def _paste_clipboard_image(self, image) -> bool:
        """Save a clipboard pixbuf and paste its shell-safe PNG path."""
        if image is None or getattr(self, "_detached", False):
            return False
        fd = -1
        path = ""
        try:
            fd, path = tempfile.mkstemp(prefix="terminalbrowser-clipboard-", suffix=".png")
            os.close(fd)
            fd = -1
            if not image.savev(path, "png", [], []):
                raise OSError("clipboard image could not be encoded as PNG")
            if not self._feed_text(shlex.quote(path)):
                raise OSError("clipboard image path could not be pasted")
            paths = getattr(self, "_clipboard_image_paths", None)
            if paths is None:
                paths = set()
                self._clipboard_image_paths = paths
            paths.add(path)
            return True
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _cleanup_clipboard_images(self) -> None:
        paths = getattr(self, "_clipboard_image_paths", set())
        for path in tuple(paths):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        paths.clear()

    def _paste_from_selection(self, selection, *, prefer_last: bool = False) -> bool:
        """Request clipboard text or an image without blocking GTK's main loop."""

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

        def _finish_without_payload(*, allow_clipboard_fallback: bool) -> None:
            if getattr(self, "_detached", False):
                return
            if prefer_last and self._last_copied_text:
                self._feed_text(self._last_copied_text)
                return
            if allow_clipboard_fallback and selection != Gdk.SELECTION_CLIPBOARD:
                if _request_text(
                    Gdk.SELECTION_CLIPBOARD,
                    lambda fallback: _finish_text(
                        fallback,
                        requested_selection=Gdk.SELECTION_CLIPBOARD,
                        allow_clipboard_fallback=False,
                    ),
                ):
                    return
            _vte_fallback()

        def _finish_image(image, *, allow_clipboard_fallback: bool) -> None:
            if getattr(self, "_detached", False):
                return
            if image is not None and self._paste_clipboard_image(image):
                return
            _finish_without_payload(allow_clipboard_fallback=allow_clipboard_fallback)

        def _finish_text(
            text,
            *,
            requested_selection,
            allow_clipboard_fallback: bool,
        ) -> None:
            if getattr(self, "_detached", False):
                return
            clean_text = _normalise(text)
            if clean_text:
                self._feed_text(clean_text)
                return
            if requested_selection == Gdk.SELECTION_CLIPBOARD and _request_image(
                requested_selection,
                lambda image: _finish_image(
                    image,
                    allow_clipboard_fallback=allow_clipboard_fallback,
                ),
            ):
                return
            _finish_without_payload(allow_clipboard_fallback=allow_clipboard_fallback)

        def _request_text(requested_selection, callback) -> bool:
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

        def _request_image(requested_selection, callback) -> bool:
            clipboard = self._system_clipboard(requested_selection)
            if clipboard is None or not hasattr(clipboard, "request_image"):
                return False

            def _received(_clipboard, image, *_args) -> None:
                callback(image)

            try:
                clipboard.request_image(_received, None)
                return True
            except TypeError:
                try:
                    clipboard.request_image(_received)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

        if _request_text(
            selection,
            lambda text: _finish_text(
                text,
                requested_selection=selection,
                allow_clipboard_fallback=True,
            ),
        ):
            return True
        _finish_without_payload(allow_clipboard_fallback=True)
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
        # While VTE owns a real selection, expose exactly the native VTE
        # adjustment so wheel/drag extension keeps one continuous selection.
        # Invalidate queued/in-flight thumb results before rebinding: a backend
        # command already inside tmux may finish, but no stale completion or
        # follow-up navigation is allowed to mutate the new selection surface.
        # Clearing the selection atomically rebinds the persistent bar to the
        # application-aware tmux/Codex history proxy.
        if self._selection_pending:
            cancel = getattr(self, "_cancel_async_history_scroll", None)
            if callable(cancel):
                cancel()
        bind_scrollbar = getattr(self, "_bind_scrollbar_for_selection", None)
        if callable(bind_scrollbar):
            bind_scrollbar(self._selection_pending)
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

    def _snapshot_selection_before_history_scroll(self) -> bool:
        """Copy a live range before any history movement can update it."""

        try:
            if not self.terminal.get_has_selection():
                return False
        except Exception:
            return False

        # Do not let the normal 120 ms select-to-copy debounce race either a
        # native held-drag extension or a degraded redraw-based fallback:
        # capture the current payload before the viewport moves.
        #
        # This is deliberately a *read-only* VTE operation. Calling
        # copy_clipboard/copy_primary from inside scroll-event can make VTE
        # consume the same wheel event while it is updating its selection,
        # which makes scrolling appear stuck whenever text is selected. The
        # normal release/idle copy path still uses those VTE ownership calls;
        # the wheel path only snapshots text and publishes CLIPBOARD.
        try:
            text = self._selected_text()
            if not text:
                copied = False
            else:
                self._last_copied_text = text
                self._set_clipboard_text(text, Gdk.SELECTION_CLIPBOARD)
                copied = True
        except Exception:
            # Selection preservation is best effort and must never disable
            # history scrolling if a clipboard backend is unavailable.
            copied = False
        source_id = getattr(self, "_selection_autocopy_source_id", 0)
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            self._selection_autocopy_source_id = 0
        self._selection_pending = False
        return copied

    def _at_native_live_edge(self) -> bool:
        """Return whether VTE's native viewport rests at the live bottom row."""

        try:
            adjustment = self.native_adjustment
            value = float(adjustment.get_value())
            upper = float(adjustment.get_upper()) - float(adjustment.get_page_size())
        except Exception:
            return True
        return value >= upper - 0.5

    def _native_scrollback_available(self) -> bool:
        """Return whether this VTE has one normal-screen history buffer."""

        try:
            adjustment = self.terminal.get_vadjustment()
            lower = float(adjustment.get_lower())
            page = float(adjustment.get_page_size())
            upper = max(lower, float(adjustment.get_upper()) - page)
        except Exception:
            return False
        return upper > lower + 0.5

    def _dispatch_selection_button_event(
        self,
        source_event: Gdk.EventScroll,
        event_type: Gdk.EventType,
        state: Gdk.ModifierType,
    ) -> bool:
        """Send VTE one synthetic left-button edge at the wheel pointer.

        VTE keeps a completed selection stable while its native adjustment
        moves, and it can extend that range with Shift+button-1.  It does not,
        however, retain the original anchor when a wheel event arrives during
        an unfinished button-1 drag.  The selection-scroll bridge below uses
        these two native operations; this helper only builds the matching GDK
        button event and never draws or owns a second selection surface.
        """

        try:
            synthetic = Gdk.Event.new(event_type)
            button_event = synthetic.button
            button_event.window = (
                getattr(source_event, "window", None) or self.terminal.get_window()
            )
            button_event.send_event = True
            button_event.time = int(
                getattr(source_event, "time", Gdk.CURRENT_TIME) or Gdk.CURRENT_TIME
            )
            button_event.x = float(getattr(source_event, "x", 0.0) or 0.0)
            button_event.y = float(getattr(source_event, "y", 0.0) or 0.0)
            button_event.x_root = float(
                getattr(source_event, "x_root", button_event.x) or button_event.x
            )
            button_event.y_root = float(
                getattr(source_event, "y_root", button_event.y) or button_event.y
            )
            button_event.state = state
            button_event.button = 1
            get_device = getattr(source_event, "get_device", None)
            if callable(get_device):
                device = get_device()
                if device is not None:
                    synthetic.set_device(device)
            self.terminal.event(synthetic)
            return True
        except Exception:
            return False

    def _continue_selection_through_native_scroll(
        self,
        event: Gdk.EventScroll,
        line_delta: int,
    ) -> bool:
        """Preserve and extend a held drag while native history is scrolled."""

        state = Gdk.ModifierType(int(getattr(event, "state", 0) or 0))
        if not line_delta or not (state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        if not self._dispatch_selection_button_event(
            event,
            Gdk.EventType.BUTTON_RELEASE,
            state,
        ):
            return False

        # Move only the one VTE adjustment while the range is completed.  A
        # Shift+button-1 press at the same pointer then asks VTE to extend the
        # existing absolute range into the newly exposed rows.  The user's
        # real button release completes this synthetic continuation normally.
        self._move_native_history(line_delta)
        button_masks = (
            Gdk.ModifierType.BUTTON1_MASK
            | Gdk.ModifierType.BUTTON2_MASK
            | Gdk.ModifierType.BUTTON3_MASK
            | Gdk.ModifierType.BUTTON4_MASK
            | Gdk.ModifierType.BUTTON5_MASK
        )
        extend_state = Gdk.ModifierType(
            (int(state) & ~int(button_masks)) | int(Gdk.ModifierType.SHIFT_MASK)
        )
        self._dispatch_selection_button_event(
            event,
            Gdk.EventType.BUTTON_PRESS,
            extend_state,
        )
        return True

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
            # A backend history redraw that is already in flight cannot be
            # cancelled at the tmux boundary. Do not let VTE establish a new
            # selection underneath that redraw: invalidate pending UI work and
            # consume this one press. The next press is accepted after the
            # worker has observed the generation change and retired.
            if bool(getattr(self, "_history_worker_active", False)):
                cancel = getattr(self, "_cancel_async_history_scroll", None)
                if callable(cancel):
                    cancel()
                return True
            # Otherwise let VTE see the complete press/drag/release gesture.
            # Live viewport restoration happens on release only when no
            # selection was created, so ordinary drag-to-copy is preserved.
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
        """Route wheel input through exactly one canonical history owner.

        Normal review belongs to tmux/Codex and never consults VTE's viewport
        for the live-edge decision.  A real VTE text selection is the sole
        exception: it remains inside the retained VTE buffer so redraws cannot
        destroy the selected range.
        """

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
            # Match discrete wheel sensitivity while preserving smooth-event
            # direction and sub-step accumulation semantics.
            delta = 3.0 * float(getattr(event, "delta_y", 0.0) or 0.0)
        line_delta = int(round(delta))
        if not line_delta and delta:
            line_delta = -1 if delta < 0 else 1
        if not line_delta:
            return True

        try:
            has_selection = bool(self.terminal.get_has_selection())
        except Exception:
            has_selection = False
        if has_selection:
            bind_scrollbar = getattr(self, "_bind_scrollbar_for_selection", None)
            if callable(bind_scrollbar):
                bind_scrollbar(True)
            self._snapshot_selection_before_history_scroll()
            if self._native_scrollback_available():
                if self._continue_selection_through_native_scroll(
                    event, line_delta
                ):
                    return True
                # Fallback scrolling is disabled globally so a non-selection
                # wheel can never become synthetic Up/Down input in Codex or
                # another TUI. Move the VTE adjustment explicitly here instead
                # of relying on VTE's default wheel handler. A completed VTE
                # selection remains anchored to buffer coordinates while the
                # viewport moves, so this preserves the highlighted payload and
                # never redraws tmux/Codex history.
                self._move_native_history(line_delta)
                return True
            # In degraded VTE history, preserving the selection is safer than
            # switching surfaces or redrawing an unrelated history owner.
            return True

        bind_scrollbar = getattr(self, "_bind_scrollbar_for_selection", None)
        if callable(bind_scrollbar):
            bind_scrollbar(False)
        handler = getattr(self, "_scroll_handler", None)
        if not callable(handler):
            return True
        try:
            outcome = handler(line_delta)
        except Exception:
            return True
        if not isinstance(outcome, ScrollOutcome):
            return True
        if outcome.disposition is ScrollDisposition.PASSTHROUGH:
            if self._history_cursor_hidden:
                self._set_history_cursor_hidden(False)
            return False
        if outcome.disposition is not ScrollDisposition.HANDLED:
            # Unknown backend state is not permission to navigate VTE's
            # incidental redraw history. Consume the event and stay put.
            return True

        self._sync_history_from_outcome(outcome)
        if outcome.reached_live_edge:
            self.prepare_for_live_input()
        else:
            self._set_history_cursor_hidden(True)
            self._arm_scroll_idle_resume()
        return True

    # ------------------------------------------------------------------ #
    # Scroll-idle auto-resume  (DISABLED)                                 #
    # Auto-resuming after scroll inactivity snaps the viewport back to    #
    # the composer while the user is actively reading history — the       #
    # inactivity timer is the root cause of the "pulled-down" bug.        #
    # Reading mode now persists until the user explicitly returns:        #
    #   • scrolls back down to the live edge                              #
    #   • presses a key while VTE has focus                               #
    #   • left-clicks the terminal surface (without creating a selection) #
    # ------------------------------------------------------------------ #
    SCROLL_IDLE_MS: int = 2000  # retained for reference; timer is not armed

    def _arm_scroll_idle_resume(self) -> None:
        """No-op: the auto-resume-on-idle timer is intentionally disabled.

        Arming this timer caused the viewport to snap back to the live
        terminal bottom whenever the user paused reading for two seconds,
        interrupting any scroll session where focus remained in the composer
        or the user simply stopped to read without typing.  The timer is
        preserved as dead code so the method is safe to call from paths that
        were not updated; it will never schedule a callback.
        """

    def _scroll_idle_resume(self) -> bool:
        """Legacy timer callback — never scheduled; kept for safety."""
        self._scroll_idle_source_id = 0
        return False

    def prepare_for_live_input(self) -> bool:
        """Return to live output only after the canonical owner acknowledges it."""

        self._remove_glib_source(self._scroll_idle_source_id)
        self._scroll_idle_source_id = 0
        # Selection is durable even when GTK reaches this idle before the
        # allocation-delayed controller exists. ``_connect_stream`` consumes
        # the latch before starting that controller.
        self._setup_priority_requested = True
        controller = getattr(self, "controller", None)
        if controller is None:
            # Notebook selection can focus a VTE before its delayed target and
            # stream controller exist. Calling the backend restore path here
            # targets a session that has not been created yet, poisons the
            # bounded retry window, and makes the user's first real key look
            # unsafe. Latch setup priority, but refuse input until a controller
            # owns the target; the next key can then be queued or delivered.
            return False
        try:
            controller.prioritize_setup()
        except Exception:
            pass
        cancel_history = getattr(self, "_cancel_async_history_scroll", None)
        if callable(cancel_history):
            cancel_history()
        handler = self._live_focus_handler
        if callable(handler):
            try:
                restored = handler()
            except Exception:
                return False
            if restored is False:
                # A failed/unknown backend restore is not permission to reset
                # the proxy or deliver input into a possibly-open transcript.
                return False
        bind_scrollbar = getattr(self, "_bind_scrollbar_for_selection", None)
        if callable(bind_scrollbar):
            bind_scrollbar(False)
        self.scroll_to_bottom()
        self._set_history_cursor_hidden(False)
        request_geometry_sync = getattr(self, "request_geometry_sync", None)
        if callable(request_geometry_sync):
            request_geometry_sync(delay_ms=0)
        return True

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
                if self.prepare_for_live_input():
                    self.terminal.grab_focus()
        return False

    def _copy_selection_to_clipboards_idle(self) -> bool:
        self._selection_autocopy_source_id = 0
        self._copy_selection_to_clipboards()
        self._selection_pending = False
        return False



def _transition_duration_ms(settings: object, duration_ms: int) -> int:
    """Honor the reduce-motion accessibility preference for one animation.

    Decorative transitions collapse to instant swaps when the user asked for
    reduced motion; GTK's global gtk-enable-animations still applies on top.
    """

    if getattr(settings, "reduce_motion", False):
        return 0
    return duration_ms


def _refresh_paused(widget: Gtk.Widget) -> bool:
    """True when the widget's toplevel window is minimized.

    Periodic refresh timers skip tmux/proc snapshot work for minimized
    windows; iconified-state tracking is wired lazily on first use so every
    caller stays a single line and unmapped/hidden gating stays unchanged.
    """

    toplevel = widget.get_toplevel()
    if not isinstance(toplevel, Gtk.Window):
        return False
    if not getattr(toplevel, "_termdeck_pause_wired", False):
        toplevel._termdeck_pause_wired = True
        toplevel._termdeck_iconified = False

        def _on_window_state(window, event) -> bool:
            window._termdeck_iconified = bool(
                getattr(event, "new_window_state", 0) & Gdk.WindowState.ICONIFIED
            )
            return False

        try:
            toplevel.connect("window-state-event", _on_window_state)
        except Exception:
            # Tracking is a best-effort optimisation; never break refresh.
            return False
    return bool(getattr(toplevel, "_termdeck_iconified", False))


def _store_sort_active(store) -> bool:
    """Report whether a user-clicked column sort controls the store's order.

    When GTK sorting is active the model reorders rows itself, so positional
    reconciliation must defer to it instead of fighting for row order.
    """

    getter = getattr(store, "get_sort_column_id", None)
    if not callable(getter):
        return False
    try:
        result = getter()
    except Exception:
        return False
    if isinstance(result, tuple) and result:
        return bool(result[0])
    return False


def _reconcile_list_store(store, desired_rows, *, key_column: int = 0) -> bool:
    """Diff ``desired_rows`` into ``store`` in place, keyed by ``key_column``.

    clear()+re-append blanks the tree for a frame, drops the scroll position
    and forces selection restoration on every periodic tick.  Updating only
    the cells that actually changed keeps the widget visually still.  Returns
    True when a full rebuild was unavoidable (duplicate keys or a relative
    reorder) so callers can restore selection state by key.
    """

    desired_keys = [row[key_column] for row in desired_rows]
    diffable = len(set(desired_keys)) == len(desired_keys)
    existing_iters = {}
    existing_order = []
    if diffable:
        tree_iter = store.get_iter_first()
        while tree_iter is not None:
            key = store.get_value(tree_iter, key_column)
            if key in existing_iters:
                diffable = False
                break
            existing_iters[key] = tree_iter
            existing_order.append(key)
            tree_iter = store.iter_next(tree_iter)
    sort_active = _store_sort_active(store)
    if diffable and not sort_active:
        desired_set = set(desired_keys)
        survivors = [key for key in existing_order if key in desired_set]
        if survivors != [key for key in desired_keys if key in existing_iters]:
            # Relative order changed underneath us; a rebuild is cheaper and
            # safer than computing a minimal move sequence for a rare event.
            diffable = False
    if not diffable:
        store.clear()
        for row in desired_rows:
            store.append(list(row))
        return True
    desired_set = set(desired_keys)
    for key in existing_order:
        if key not in desired_set:
            store.remove(existing_iters.pop(key))
    for position, row in enumerate(desired_rows):
        tree_iter = existing_iters.get(row[key_column])
        if tree_iter is None:
            if sort_active:
                store.append(list(row))
            else:
                store.insert(position, list(row))
            continue
        for column, value in enumerate(row):
            if store.get_value(tree_iter, column) != value:
                store.set_value(tree_iter, column, value)
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
        super().__init__(title="T1000 Window Overview")
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
        refresh.connect("clicked", lambda *_: self.refresh(force=True))
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
        if _refresh_paused(self):
            return True
        if self.get_visible():
            self.refresh()
        return True

    def refresh(self, *_args, force: bool = False) -> None:
        # Diff rows in place instead of clear()+rebuild so the table never
        # blinks, keeps its scroll position, and keeps the user's selection
        # across periodic refreshes.
        selection = self.tree.get_selection()
        _model, selected_paths = selection.get_selected_rows()
        selected_titles = {
            self.store.get_value(self.store.get_iter(path), 0)
            for path in selected_paths
        }
        _records, items = self.app_context.shared_task_snapshot(force=force)
        desired_rows = [
            [
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
            ]
            for row in build_window_snapshots(items)
        ]
        if _reconcile_list_store(self.store, desired_rows):
            # Rare full rebuild (duplicate titles or reorder): restore the
            # user's selection by key so focus is never visibly lost.
            row_iter = self.store.get_iter_first()
            while row_iter is not None:
                if self.store.get_value(row_iter, 0) in selected_titles:
                    selection.select_iter(row_iter)
                row_iter = self.store.iter_next(row_iter)


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
        super().__init__(title="T1000 Keyboard Shortcuts")
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
        super().__init__(title="T1000 Search Tabs")
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
        owner_lookup = getattr(self.app_context, "window_for_window_id", None)
        owner = owner_lookup(record.window_id) if callable(owner_lookup) else None
        target = owner or self.app_context.windows[-1]
        open_user = getattr(target, "open_user_session", None)
        if callable(open_user):
            open_user(record)
        else:
            target.open_session(record)

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

        self.hint = Gtk.Label(label="Ctrl+left-click any tab card to select multiple tabs", xalign=0)
        self.hint.set_line_wrap(True)
        _style(self.hint, "td-muted")
        self.pack_start(self.hint, False, False, 0)

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

        self.prompt = Gtk.Entry(placeholder_text="Ask this window's Codex captain")
        self.prompt.set_tooltip_text(
            "Send one task to this window's Codex captain; Pause, Resume, and Kill still use selected tabs"
        )
        self.prompt.connect("activate", lambda *_: self.send_prompt())
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
            ("Make current tab captain", self.make_current_captain),
            ("Clear captain", self.clear_captain),
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
            ("Process monitor", self.window.open_process_monitor_window),
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
        if _refresh_paused(self):
            return True
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
            self.window.open_user_session(record)

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

        # Let GtkListBox's row-activated signal own ordinary navigation. Doing
        # it here as well opens one physical click twice (and used to create
        # duplicate replacement shells for a missing tmux target).
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
        menu.append(self._menu_item("Open", lambda: self.window.open_user_session(record)))
        menu.append(self._menu_item(selected_label, lambda: self._toggle_record_selection(record, check)))
        menu.append(self._menu_item("Rename", lambda: self.window.rename_session(record)))
        captain = self._stored_captain()
        if captain is not None and captain.id == record.id:
            menu.append(self._menu_item("Clear Codex captain", self.clear_captain))
        else:
            menu.append(self._menu_item("Make Codex captain", lambda: self.make_captain(record)))
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
        terminated = 0
        if callable(terminate_records):
            terminated = terminate_records(records)
        else:
            for record in records:
                result = self.window.stop_session(record)
                if result is not False:
                    terminated += 1
                    self.selected_ids.discard(record.id)
            self.refresh()
        if terminated:
            self.window.show_toast(toast_message_for_action("killed", terminated), kind="danger")

    def _update_count_label(self) -> None:
        selected_count = len(self.selected_ids)
        total = self._visible_record_count
        shown = self._shown_record_count
        display = f"{shown} shown · {total} tabs" if shown < total else f"{total} tabs"
        if selected_count:
            display = f"{selected_count} selected · {display}"
        self.count_label.set_text(display)

    def _stored_captain(self) -> SessionRecord | None:
        resolver = getattr(self.window.app_context.store, "captain_for_window", None)
        if not callable(resolver):
            return None
        try:
            return resolver(self.window.window_id)
        except Exception:
            return None

    def _update_captain_hint(self, captain: SessionRecord | None) -> None:
        if captain is None:
            text = "Captain: none · Open a Codex tab, then Send or choose Make current tab captain"
        else:
            text = f"Captain: {captain.name} · Send asks it to coordinate this window"
        text += " · Ctrl+left-click tab cards for Pause, Resume, or Kill"
        self.hint.set_text(text)

    def _captain_error(self, record: SessionRecord) -> str | None:
        if record.window_id != self.window.window_id:
            return "That tab belongs to a different T1000 window"
        if not record.visible or record.id not in self.window.tabs:
            return "The Codex captain must be an open tab in this window"
        status = record.status.casefold()
        if status == "paused":
            return f"Captain {record.name} is paused; resume it before sending"
        if status in {"archived", "stopped", "killed", "terminated", "start-error"}:
            return f"Captain {record.name} is not running"
        probe = getattr(self.window.app_context.backend, "is_codex_running", None)
        if not callable(probe):
            return "The terminal backend cannot verify a live Codex captain"
        try:
            is_codex = bool(probe(record))
        except Exception:
            is_codex = False
        if not is_codex:
            return f"{record.name} is not currently running Codex"
        return None

    def _set_captain(self, record: SessionRecord) -> SessionRecord | None:
        setter = getattr(self.window.app_context.store, "set_window_captain", None)
        if not callable(setter):
            return None
        try:
            return setter(self.window.window_id, record.id)
        except Exception:
            return None

    def resolve_captain(self, *, auto_adopt_current: bool = True) -> tuple[SessionRecord | None, str | None]:
        captain = self._stored_captain()
        if captain is not None:
            error = self._captain_error(captain)
            return (captain, None) if error is None else (None, error)
        if not auto_adopt_current:
            return None, "This window has no Codex captain"
        current = self.window.current_record()
        if current is None:
            return None, "Open a Codex tab in this window, then make it captain"
        error = self._captain_error(current)
        if error is not None:
            return None, error + "; open a Codex tab or choose Make Codex captain"
        captain = self._set_captain(current)
        if captain is None:
            return None, "Could not save this window's Codex captain"
        self.window.refresh_captain_chrome()
        return captain, None

    def make_captain(self, record: SessionRecord) -> None:
        refreshed = self.window.app_context.store.get(record.id) or record
        error = self._captain_error(refreshed)
        if error is not None:
            self.window.show_toast(error, kind="warn")
            return
        captain = self._set_captain(refreshed)
        if captain is None:
            self.window.show_toast("Could not save this window's Codex captain", kind="danger")
            return
        self.window.refresh_captain_chrome()
        self.window.show_toast(f"{captain.name} is now this window's Codex captain", kind="success")

    def make_current_captain(self) -> None:
        current = self.window.current_record()
        if current is None:
            self.window.show_toast("Open a Codex tab before choosing a captain", kind="warn")
            return
        self.make_captain(current)

    def clear_captain(self) -> None:
        clear = getattr(self.window.app_context.store, "clear_window_captain", None)
        changed = False
        if callable(clear):
            try:
                changed = bool(clear(self.window.window_id))
            except Exception:
                changed = False
        self.window.refresh_captain_chrome()
        self.window.show_toast("Codex captain cleared" if changed else "This window has no captain", kind="info")

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
        captain = self._stored_captain()
        self._update_captain_hint(captain)
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
            captain.id if captain is not None else None,
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
        for row_idx, record in enumerate(records):
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
            captain_mark = "♛ " if captain is not None and captain.id == record.id else ""
            title = Gtk.Label(label=f"{kind_glyph(kind)} {captain_mark}{pin}{record.name}", xalign=0)
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
            # ---- Move-up / move-down buttons --------------------------------
            move_up = Gtk.Button(label="▲")
            move_up.set_tooltip_text("Move tab up")
            _style(move_up, "td-ghost-action")
            move_up.set_sensitive(row_idx > 0)
            move_up.connect("clicked", lambda _b, r=record: self._move_tab_up(r))
            actions.pack_start(move_up, False, False, 0)
            move_dn = Gtk.Button(label="▼")
            move_dn.set_tooltip_text("Move tab down")
            _style(move_dn, "td-ghost-action")
            move_dn.set_sensitive(row_idx < len(records) - 1)
            move_dn.connect("clicked", lambda _b, r=record: self._move_tab_down(r))
            actions.pack_start(move_dn, False, False, 0)
            # -----------------------------------------------------------------
            rename = Gtk.Button(label="Rename")
            rename.set_tooltip_text("Rename this tab")
            _style(rename, "td-ghost-action")
            rename.connect("clicked", lambda _b, r=record: self.window.rename_session(r))
            rename.connect("button-press-event", self._on_session_button_press, record, check)
            actions.pack_end(rename, False, False, 0)
            focus = Gtk.Button(label="Open")
            _style(focus, "td-primary-action")
            focus.connect("clicked", lambda _b, r=record: self.window.open_user_session(r))
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

    # -----------------------------------------------------------------------
    # Tab reordering from the side pane
    # -----------------------------------------------------------------------

    def _move_tab_in_notebook(self, record: "SessionRecord", delta: int) -> None:
        """Shift *record*'s notebook page by *delta* positions (+1 down, -1 up).

        The GtkNotebook ``page-reordered`` signal fires automatically, which
        triggers ``_renumber_visible_tabs`` on the window and persists the new
        ``tab_index`` for every affected tab via the store.  The side pane is
        then refreshed by ``_on_page_reordered`` as well.
        """
        tab = self.window.tabs.get(record.id)
        if tab is None:
            return
        notebook = self.window.notebook
        current = notebook.page_num(tab.widget)
        if current < 0:
            return
        target = current + delta
        pages = notebook.get_n_pages()
        if target < 0 or target >= pages:
            return
        notebook.reorder_child(tab.widget, target)

    def _move_tab_up(self, record: "SessionRecord") -> None:
        """Move *record* one position earlier (towards index 0)."""
        self._move_tab_in_notebook(record, -1)

    def _move_tab_down(self, record: "SessionRecord") -> None:
        """Move *record* one position later (towards the last index)."""
        self._move_tab_in_notebook(record, +1)

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
            if record and record.window_id == self.window.window_id:
                records.append(record)
        return records

    def send_prompt(self) -> None:
        text = self.prompt.get_text()
        if not text.strip():
            return
        captain, error = self.resolve_captain()
        if captain is None:
            self.window.show_toast(error or "This window has no live Codex captain", kind="warn")
            return
        records = self.window.app_context.store.list(
            include_archived=False,
            window_id=self.window.window_id,
        )
        envelope = build_captain_envelope(
            window_id=self.window.window_id,
            window_title=self.window.window_title,
            captain=captain,
            records=records,
            user_request=text,
            database_path=self.window.app_context.store.path,
        )
        if not self.window.app_context.confirm_control_plan(
            self.window,
            [captain],
            "send",
            text=envelope,
            enter=True,
            selection="window Codex captain",
        ):
            return
        try:
            self.window.app_context.backend.send_text(captain, envelope, enter=True)
        except (OSError, TmuxError) as exc:
            self.window.show_toast(f"Could not send to captain: {exc}", kind="danger")
            return
        self.prompt.set_text("")
        self.window.show_toast(f"Sent to captain {captain.name}", kind="success")

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
        archivable = []
        for record in records:
            if record.id in self.window.tabs:
                if self.window.hide_tab(record.id) is False:
                    continue
            archivable.append(record)
        if archivable:
            archive_records(self.window.app_context.store, archivable)
        for record in archivable:
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
        terminated = 0
        if callable(terminate_records):
            terminated = terminate_records(records)
        else:
            for record in records:
                result = self.window.stop_session(record)
                if result is not False:
                    terminated += 1
                    self.selected_ids.discard(record.id)
            self.refresh()
        if terminated:
            self.window.show_toast(toast_message_for_action("killed", terminated), kind="danger")


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
        super().__init__(title="T1000 Tab Manager")
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
            ("Refresh", lambda *_: self.refresh(force=True)),
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
        if _refresh_paused(self):
            return True
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

    def refresh(self, *_args, force: bool = False) -> None:
        selected_tabs = set(self._selected_ids_for_tree(self.tree))
        selected_history = set(self._selected_ids_for_tree(self.history_tree))
        records, items = self.app_context.shared_task_snapshot(force=force)
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
            desired_rows = []
            for row in rows[:MASTER_ROW_LIMIT]:
                values = row.as_list()
                desired_rows.append(values[:3] + values[4:])
            if _reconcile_list_store(model, desired_rows) and selected:
                # Rare full rebuild: restore selection by session id (col 0)
                # so multi-select control workflows survive the refresh.
                row_iter = model.get_iter_first()
                while row_iter is not None:
                    if model.get_value(row_iter, 0) in selected:
                        tree.get_selection().select_path(model.get_path(row_iter))
                    row_iter = model.iter_next(row_iter)
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
                capture = f"[T1000 redacted {redacted.replacements} secret-like value(s): {labels}]\n\n{capture}"
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
        open_user = getattr(target, "open_user_session", None)
        if callable(open_user):
            open_user(record)
        else:
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
        archivable = []
        for record in records:
            hidden = True
            for window in self.app_context.windows:
                if record.id in window.tabs:
                    hidden = window.hide_tab(record.id) is not False
                    if not hidden:
                        break
            if hidden:
                archivable.append(record)
        if archivable:
            archive_records(self.app_context.store, archivable)
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
        super().__init__(application=app_context, title="T1000")
        self.app_context = app_context
        self.keymap = KeyMap(
            app_context.theme,
            profile=app_context.settings.keyboard_profile,
            import_terminator=app_context.settings.import_terminator_keybindings,
        )
        self.window_id = window_id or short_id("win")
        self.window_title = window_title or "Main"
        remember_window = getattr(app_context.store, "remember_window", None)
        if callable(remember_window):
            remember_window(self.window_id, self.window_title)
        self._restore_records = restore_records
        self.tabs: dict[str, TerminalTab] = {}
        self.master_window: MasterWindow | None = None
        self.process_monitor_window: ProcessMonitorWindow | None = None
        self.window_overview: WindowOverview | None = None
        self.shortcut_window: ShortcutWindow | None = None
        self.search_window: SessionSearchWindow | None = None
        self.set_default_size(app_context.theme.window_width or 1280, app_context.theme.window_height or 780)
        self.tabs_pane_visible = bool(getattr(app_context.settings, "tabs_pane_visible", False))
        self._toast_source = None
        self._tab_directory_refresh_alive = True
        self._terminal_layout_sync_source_id = 0
        self._side_pane_layout_source_id = 0
        self._auth_combo_updating = False
        self._auth_switch_generation: dict[str, int] = {}
        self._auth_switch_inflight: set[str] = set()
        # A tmux attach client is deliberately allowed to exit while an account
        # transaction replaces its server-side session.  Keep the notebook tab
        # protected until a replacement client has proved stable; an auth action
        # must never be reclassified as a user-visible tab close.
        self._auth_attach_protected: set[str] = set()
        self._auth_fingerprints: dict[str, str] = {}
        self._auth_environment_ready: dict[str, bool] = {}
        self._auth_completion_recovery_lock = threading.Lock()
        self._auth_completion_recoveries: list[tuple[object, ...]] = []
        # Payload replacement is distinct from account-pointer state. It covers
        # exact gateway-model refreshes and automatic dead-pane recovery while
        # sharing the same no-loss attach protection contract.
        self._payload_restart_generation: dict[str, int] = {}
        self._payload_restart_inflight: set[str] = set()
        self._payload_restart_backoff_until: dict[str, float] = {}
        self._payload_restart_completion_lock = threading.Lock()
        self._payload_restart_completions: list[tuple[object, ...]] = []
        self._gateway_model_probe_session_id: str | None = None
        self._gateway_model_probe_at = 0.0
        self._gateway_model_status: GatewayModelStatus | None = None
        self._missing_session_replacements: set[str] = set()
        # A durable row can be visible in the side pane before its VTE client
        # is materialized (for example when one AttachTerminal construction
        # fails while GTK/VTE is under load). Keep those IDs separate from
        # the store state and retry the attach in-place; never recreate a live
        # tmux target merely because the widget was not built yet.
        self._restore_retry_ids: set[str] = set()
        self._restore_retry_source_id = 0
        self._restore_retry_attempts = 0
        # GUI workspace recipes launch hidden/background payloads only after a
        # mapped terminal surface proves one stable canonical grid.  All fields
        # are window-local and are cancelled before destruction.
        self._recipe_geometry_source_id = 0
        self._recipe_geometry_probe = None
        self._recipe_geometry_probe_show_tabs = True
        self._recipe_geometry_probe_window_hidden = False
        self._recipe_geometry_last_grid = (0, 0)
        self._recipe_geometry_stable_samples = 0
        self._recipe_geometry_deadline = 0.0
        self._recipe_hidden_records: tuple[SessionRecord, ...] = ()
        self._recipe_hidden_launch_errors: tuple[tuple[str, str], ...] = ()
        self._recipe_canonical_grid = (0, 0)
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
        # Inline rename: a stack swaps the title label for an entry in place,
        # so renaming happens where the name lives (no modal dialog).
        title_entry = Gtk.Entry()
        title_entry.set_alignment(0.5)
        title_entry.set_width_chars(28)
        title_entry.set_max_width_chars(28)
        _style(title_entry, "td-title-entry")
        self._window_title_entry = title_entry
        title_stack = Gtk.Stack()
        # Crossfade between the title label and its inline rename entry; GTK
        # globally disables this when gtk-enable-animations is off.
        title_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        title_stack.set_transition_duration(_transition_duration_ms(app_context.settings, 120))
        title_stack.add_named(title, "label")
        title_stack.add_named(title_entry, "entry")
        title_stack.set_visible_child(title)
        self._window_title_stack = title_stack
        title_event = Gtk.EventBox()
        title_event.set_visible_window(False)
        title_event.add(title_stack)
        title_event.set_events(title_event.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK)
        title_event.connect("button-press-event", self._on_window_title_press)
        header.set_custom_title(title_event)
        self.set_titlebar(header)
        self.set_title(f"T1000 · {self.window_title}")

        # Keep a permanent per-window New tab control in the header. The
        # browser-style floating + beside the notebook tabs hides when the
        # scrollable strip is full, so it cannot be the only mouse-accessible
        # way to create another tab. New-window creation remains available in
        # the overflow menu without consuming scarce header space.
        header_new_tab = _icon_button(
            "New tab",
            "list-add-symbolic",
            "New tab · Ctrl+T · opens in the home directory",
            self.new_tab,
            suggested=False,
            show_label=False,
        )
        _style(header_new_tab, "td-new-tab", "td-header-icon")
        # Keep the button a centred square instead of stretching to the full
        # headerbar height (noticeable at large UI font scales).
        header_new_tab.set_valign(Gtk.Align.CENTER)
        self._header_new_tab = header_new_tab
        header.pack_start(header_new_tab)

        auth_combo = Gtk.ComboBoxText()
        auth_combo.append("__none__", "Codex: no account")
        auth_combo.set_active_id("__none__")
        auth_combo.set_tooltip_text("Codex account for the current terminal")
        auth_combo.set_valign(Gtk.Align.CENTER)
        auth_combo.connect("changed", self._on_auth_account_changed)
        _style(auth_combo, "td-auth-account")
        self._auth_combo = auth_combo
        header.pack_end(auth_combo)

        gateway_models = _icon_button(
            "Refresh models",
            "view-refresh-symbolic",
            "Gateway model inventory is not used by this tab",
            self._on_gateway_models_refresh,
            suggested=False,
            show_label=False,
        )
        _style(gateway_models, "td-gateway-models", "td-header-icon")
        gateway_models.set_valign(Gtk.Align.CENTER)
        # Parent show_all must not reveal this control on ordinary shells or
        # Codex tabs. _refresh_gateway_model_control owns its visibility.
        gateway_models.set_no_show_all(True)
        gateway_models.set_sensitive(False)
        gateway_models.hide()
        self._gateway_models_button = gateway_models
        header.pack_end(gateway_models)

        overflow_header_actions = [
            ("Rename window", self.rename_window),
            ("New T1000 window", self.new_window),
            ("Search tabs", self.open_search_window),
            ("Command palette", self.open_command_palette),
            ("Reopen closed tab", self.reopen_last_closed_tab),
            ("Show/hide Tabs pane", self.toggle_tabs_pane),
            ("Tab manager · per-tab details", self.open_master_window),
            ("Window overview · window totals", self.open_window_overview),
            ("Keyboard shortcuts", self.open_shortcut_window),
            ("Terminal appearance", self.open_terminal_options),
            ("Kill Window Permanently…", self.kill_window),
            ("Quit T1000", self.quit_termdeck),
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
        self.notebook.connect("switch-page", self._on_notebook_switch_page)
        # GtkNotebook owns this end action independently of its scrollable tab
        # pages. It therefore remains visible when the strip overflows, unlike
        # the previous floating overlay that deliberately hid itself once the
        # last tab scrolled out of view.
        strip_new_tab = _icon_button(
            "New tab",
            "list-add-symbolic",
            "New tab · Ctrl+T · opens in the home directory",
            self.new_tab,
            suggested=False,
            show_label=False,
        )
        _style(strip_new_tab, "td-new-tab", "td-tabstrip-new")
        strip_new_tab.set_valign(Gtk.Align.CENTER)
        self._strip_new_tab = strip_new_tab
        _install_notebook_end_action(self.notebook, strip_new_tab)
        self.notebook.connect("size-allocate", self._on_terminal_layout_size_allocate)
        self.side = SidePane(self)
        self.side_position = app_context.settings.side_pane_position
        self._pack_side_pane()

        root = Gtk.Overlay()
        _style(root, "td-main-root")
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.add(self.paned)
        self._chrome_root = root
        self.toast_revealer = Gtk.Revealer()
        self.toast_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.toast_revealer.set_transition_duration(_transition_duration_ms(app_context.settings, 160))
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
        self.connect("size-allocate", self._on_terminal_layout_size_allocate)
        self.connect("map", self._on_map_recover_offscreen)
        self.show_all()
        self.side.set_visible(self.tabs_pane_visible)
        if self.tabs_pane_visible:
            self._queue_side_pane_layout_repack()
        self.side.refresh()
        self.restore_previous_tabs(records=restore_records)
        self._refresh_auth_selector()
        self._refresh_gateway_model_control(force=True)
        GLib.timeout_add_seconds(2, self._poll_codex_auth_state)
        GLib.timeout_add_seconds(
            int(max(1, round(getattr(app_context.settings, "ui_refresh_interval_seconds", 5.0)))),
            self._refresh_tab_directories_timer,
        )

    def _on_map_recover_offscreen(self, *_args) -> None:
        """Bring a remembered window back when its old monitor disappeared.

        Window managers retain positions outside the current X screen after a
        monitor/layout change.  The terminal clients can be attached and the
        side pane can still be populated in that state, making the window look
        like a restore failure.  Move only windows that do not overlap any
        current monitor; valid multi-monitor placements remain untouched.
        """

        try:
            if self.is_fullscreen():
                return
            was_maximized = bool(self.is_maximized())
            window = self.get_window()
            if window is None:
                return
            # ``get_position`` may report the client-requested origin while a
            # window manager has already applied an obsolete frame position.
            # Frame extents are the actual root-screen coordinates when they
            # are available; fall back to the GTK client geometry otherwise.
            frame = getattr(window, "get_frame_extents", lambda: None)()
            if frame is not None:
                x, y, width, height = frame.x, frame.y, frame.width, frame.height
            else:
                x, y = self.get_position()
                width, height = self.get_size()
            display = self.get_display()
            monitors = []
            for index in range(display.get_n_monitors()):
                geometry = display.get_monitor(index).get_geometry()
                monitors.append((geometry.x, geometry.y, geometry.width, geometry.height))
            if not monitors:
                return
            overlaps = any(
                x < mx + mw
                and x + max(width, 1) > mx
                and y < my + mh
                and y + max(height, 1) > my
                for mx, my, mw, mh in monitors
            )
            if overlaps:
                return
            primary = display.get_primary_monitor() or display.get_monitor(0)
            geometry = primary.get_geometry()
            target_x = geometry.x + 20
            target_y = geometry.y + 20
            if was_maximized:
                # Maximize after moving the normal window so the WM computes
                # the maximized frame on the current monitor, not the removed
                # monitor recorded in its previous session.
                self.unmaximize()
            self.move(target_x, target_y)
            if was_maximized:
                self.maximize()
        except Exception:
            # Placement is a recovery aid only; never make a valid terminal
            # restore fail because a window manager lacks geometry support.
            return

    def on_delete(self, *_args) -> bool:
        if TermDeckWindow._auth_switch_blocks_action(self, action="closing this window"):
            return True
        # Detach VTE clients only. Leave tab rows visible so browser-style
        # multi-window restore can reopen this named window on the next launch.
        for tab in list(self.tabs.values()):
            tab.widget.detach()
        return False

    def _auth_switch_blocks_action(
        self,
        session_id: str | None = None,
        *,
        action: str,
    ) -> bool:
        """Keep a tab/window alive until its account activation is durable."""

        inflight = getattr(self, "_auth_switch_inflight", set())
        blocked = bool(inflight) if session_id is None else session_id in inflight
        if not blocked:
            return False
        name = "this terminal"
        if session_id is not None:
            record = self.app_context.store.get(session_id)
            if record is not None:
                name = record.name
        self.show_toast(
            f"Wait for {name}'s Codex account switch before {action}",
            kind="warning",
        )
        return True

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
        if self._recipe_geometry_source_id:
            try:
                GLib.source_remove(self._recipe_geometry_source_id)
            except Exception:
                pass
            self._recipe_geometry_source_id = 0
        self._recipe_hidden_records = ()
        self._cleanup_recipe_geometry_probe()
        windows = getattr(self.app_context, "windows", None)
        if isinstance(windows, list) and self in windows:
            windows.remove(self)

    def _begin_inline_rename(
        self,
        stack: "Gtk.Stack",
        label: "Gtk.Label",
        entry: "Gtk.Entry",
        current: str,
        commit,
    ) -> None:
        """Swap a label for an in-place entry (Chrome/Claude-style rename).

        ``commit(text)`` is called with the trimmed value on Enter or focus-out;
        Escape cancels. Re-entrancy is guarded so a focus-out fired while we are
        already tearing down does not double-commit.
        """
        if getattr(entry, "_td_editing", False):
            return
        entry._td_editing = True  # type: ignore[attr-defined]
        entry.set_text(current)
        stack.set_visible_child(entry)
        entry.grab_focus()
        entry.select_region(0, -1)

        def _finish(text_value, *, save: bool) -> None:
            if not getattr(entry, "_td_editing", False):
                return
            entry._td_editing = False  # type: ignore[attr-defined]
            for handler_id in getattr(entry, "_td_handlers", []):
                try:
                    entry.disconnect(handler_id)
                except Exception:
                    pass
            entry._td_handlers = []  # type: ignore[attr-defined]
            stack.set_visible_child(label)
            if save:
                cleaned = (text_value or "").strip()
                if cleaned and cleaned != current:
                    commit(cleaned)

        def _on_activate(_e):
            _finish(entry.get_text(), save=True)

        def _on_key(_e, key_event):
            if key_event.keyval == Gdk.KEY_Escape:
                _finish(None, save=False)
                return True
            return False

        def _on_focus_out(_e, _event):
            _finish(entry.get_text(), save=True)
            return False

        entry._td_handlers = [  # type: ignore[attr-defined]
            entry.connect("activate", _on_activate),
            entry.connect("key-press-event", _on_key),
            entry.connect("focus-out-event", _on_focus_out),
        ]

    def _on_window_title_press(self, _widget, event: Gdk.EventButton) -> bool:
        """Double-click the window title in the headerbar to rename it in place."""
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and int(getattr(event, "button", 0) or 0) == 1:
            self._begin_inline_rename(
                self._window_title_stack,
                self.window_title_label,
                self._window_title_entry,
                self.window_title,
                lambda text: self.set_window_name(text),
            )
            return True
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
        # Inline rename: a stack swaps the tab name label for an entry, driven
        # by _begin_inline_rename. A Gtk.Label has no input window, so the
        # double-click never fired before — wrap it in an EventBox that owns a
        # real window and receives the button events.
        name_entry = Gtk.Entry()
        name_entry.set_width_chars(min(20, max(4, len(record.name))))
        name_entry.set_max_width_chars(20)
        _style(name_entry, "td-tab-name-entry")
        name_stack = Gtk.Stack()
        # Match the window-title rename crossfade for tab renames.
        name_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        name_stack.set_transition_duration(_transition_duration_ms(self.app_context.settings, 120))
        name_stack.add_named(name, "label")
        name_stack.add_named(name_entry, "entry")
        name_stack.set_visible_child(name)
        name_event = Gtk.EventBox()
        name_event.set_visible_window(False)
        name_event.add(name_stack)
        name_event.set_events(name_event.get_events() | Gdk.EventMask.BUTTON_PRESS_MASK)
        name_event.connect(
            "button-press-event",
            self._on_tab_label_press,
            record.id,
            name_stack,
            name,
            name_entry,
        )
        # Content-hugging tab: the name must not expand, otherwise the close
        # control drifts away from the label (SOTA tabs keep them adjacent).
        label_box.pack_start(name_event, False, False, 0)
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

    def _move_tab_to_new_window(self, record: SessionRecord) -> None:
        """Open a new TermDeck window and move *record* into it."""
        if TermDeckWindow._auth_switch_blocks_action(self, record.id, action="moving this tab"):
            return
        destination = self.app_context.prompt_for_new_window(
            parent=self,
            open_initial_tab=False,
        )
        if destination is not None:
            self.app_context.move_tab_to_window(record.id, destination)

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
        # --- Move to window submenu -------------------------------------------
        move_item = Gtk.MenuItem(label="Move to window")
        submenu = Gtk.Menu()
        destinations = self.app_context.available_move_windows(record)
        if destinations:
            for destination in destinations:
                item = Gtk.MenuItem(label=destination.window_title)
                item.connect(
                    "activate",
                    lambda _item, target=destination: (
                        self.app_context.move_tab_to_window(record.id, target)
                    ),
                )
                submenu.append(item)
        else:
            unavailable = Gtk.MenuItem(label="No other open windows")
            unavailable.set_sensitive(False)
            submenu.append(unavailable)
        new_win_item = Gtk.MenuItem(label="New window…")
        new_win_item.connect(
            "activate",
            lambda _item: self._move_tab_to_new_window(record),
        )
        submenu.append(new_win_item)
        move_item.set_submenu(submenu)
        menu.append(move_item)
        # ----------------------------------------------------------------------
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._tab_menu_item("Move tabs pane to left", lambda: self.set_side_pane_position("left")))
        menu.append(self._tab_menu_item("Move tabs pane to right", lambda: self.set_side_pane_position("right")))
        menu.append(self._tab_menu_item("Terminal options", self.open_terminal_options))
        menu.append(self._tab_menu_item("Tab manager", self.open_master_window))
        menu.show_all()
        # Keep a reference so the menu widget stays alive during the popup.
        self._tab_context_menu = menu
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, int(getattr(event, "button", 3) or 3), int(getattr(event, "time", 0) or 0))

    def _on_tab_label_press(
        self,
        _widget,
        event: Gdk.EventButton,
        session_id: str,
        name_stack=None,
        name_label=None,
        name_entry=None,
    ) -> bool:
        button = int(getattr(event, "button", 0) or 0)
        record = self.app_context.store.get(session_id)
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and button == 1:
            if record is not None:
                if name_stack is not None and name_label is not None and name_entry is not None:
                    self._begin_inline_rename(
                        name_stack,
                        name_label,
                        name_entry,
                        record.name,
                        lambda text, rid=record.id: self._commit_tab_rename(rid, text),
                    )
                else:
                    self.rename_session(record)
            return True
        if button == 3 and record is not None:
            self._show_tab_context_menu(record, event)
            return True
        return False

    def _commit_tab_rename(self, session_id: str, new_name: str) -> None:
        """Persist an inline tab rename without opening the modal dialog."""
        record = self.app_context.store.get(session_id)
        if record is None or not new_name or new_name == record.name:
            return
        result = rename_record(self.app_context.store, record, new_name)
        if not result.ok:
            self.show_toast(result.detail or "Rename failed", kind="danger")
            return
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

    def refresh_captain_chrome(self) -> None:
        """Refresh only the existing side-pane captain indicator."""

        self.side.refresh()

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

    @staticmethod
    def _should_update_recovery_metadata(
        record: SessionRecord,
        command: str | None,
        recovery_cwd: str | None,
    ) -> bool:
        existing_command = getattr(record, "recovery_command", None)
        existing_cwd = getattr(record, "recovery_cwd", None)
        if not command and existing_command:
            return False
        return existing_command != command or existing_cwd != recovery_cwd

    def refresh_tab_directories(self) -> int:
        records = [tab.record for tab in self.tabs.values()]
        resolve_many = getattr(self.app_context.backend, "current_directories", None)
        observe_recovery = getattr(self.app_context.backend, "recovery_metadata", None)
        if not records or (not callable(resolve_many) and not callable(observe_recovery)):
            return 0
        live_by_id: dict[str, str | None] = {}
        if callable(resolve_many):
            try:
                live_by_id = dict(resolve_many(records))
            except Exception:
                live_by_id = {}
        recovery_by_id: dict[str, tuple[str | None, str | None]] = {}
        if callable(observe_recovery):
            try:
                recovery_by_id = dict(observe_recovery(records))
            except Exception:
                recovery_by_id = {}
        changed_ids: set[str] = set()
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
            changed_ids.add(session_id)
        recovery_updates = []
        for session_id, (command, recovery_cwd) in recovery_by_id.items():
            tab = self.tabs.get(session_id)
            if tab is None:
                continue
            record = self.app_context.store.get(session_id) or tab.record
            if not TermDeckWindow._should_update_recovery_metadata(record, command, recovery_cwd):
                continue
            recovery_updates.append((session_id, command, recovery_cwd))
        if recovery_updates:
            try:
                self.app_context.store.update_recovery_metadata(recovery_updates)
            except Exception:
                recovery_updates = []
            else:
                changed_ids.update(
                    session_id for session_id, _command, _cwd in recovery_updates
                )
                for session_id, _command, _cwd in recovery_updates:
                    updated = self.app_context.store.get(session_id)
                    if updated is not None:
                        self.refresh_tab_label(updated)
        if changed_ids:
            self.side.refresh()
            if self.master_window is not None and self.master_window.get_visible():
                self.master_window.refresh()
        return len(changed_ids)

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
        self.set_title(f"T1000 · {clean_title}")
        self.window_title_label.set_text(clean_title)
        self.app_context.store.rename_window(self.window_id, clean_title)
        for session_id, tab in self.tabs.items():
            updated = self.app_context.store.get(session_id)
            if updated is not None:
                tab.record = updated
        self.side.refresh()
        self.show_toast(f"Window renamed to {clean_title}", kind="success")

    def rename_window(self) -> None:
        title = self._prompt_window_name(title="Rename T1000 window", initial=self.window_title)
        if title is not None:
            self.set_window_name(title)

    def new_window(self) -> None:
        self.app_context.prompt_for_new_window(parent=self)

    def new_window_direct(self) -> None:
        """Open a new window immediately, without the name dialog.

        Used by the headerbar + so the click is instant: the window opens as
        "Window N" with its first shell in the home directory, and it can be
        renamed in place by double-clicking its title.
        """

        ctx = self.app_context
        ctx.create_named_window(
            f"Window {len(ctx.windows) + 1}",
            initial_directory=Path.home(),
            open_initial_tab=True,
        )

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

    def new_shell_tab(self, cwd: str | Path | None = None) -> SessionRecord:
        directory = normalise_directory(cwd or Path.home())
        return self.create_session(directory_tab_name(directory), "terminal", default_shell(), directory)

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
                owner_lookup = getattr(self.app_context, "window_for_window_id", None)
                owner = owner_lookup(record.window_id) if callable(owner_lookup) else None
                target = owner or self
                open_user = getattr(target, "open_user_session", None)
                if callable(open_user):
                    open_user(record)
                else:
                    target.open_session(record)

    def create_session(self, name: str, tag: str, command: str, cwd: str) -> SessionRecord:
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
        try:
            create_at_grid = getattr(
                self.app_context.backend,
                "create_or_resume_at_grid",
                None,
            )
            get_current_page = getattr(self.notebook, "get_current_page", None)
            get_nth_page = getattr(self.notebook, "get_nth_page", None)
            page = (
                get_nth_page(get_current_page())
                if callable(get_current_page) and callable(get_nth_page)
                else None
            )
            visible_terminal = getattr(page, "terminal", None)
            columns = (
                max(0, int(visible_terminal.get_column_count()))
                if visible_terminal is not None
                else 0
            )
            rows = (
                max(0, int(visible_terminal.get_row_count()))
                if visible_terminal is not None
                else 0
            )
            if callable(create_at_grid) and columns >= 2 and rows >= 2:
                # New tabs in an existing window inherit the mapped VTE grid,
                # so their first TUI frame never passes through 80x24.
                create_at_grid(record, columns=columns, rows=rows)
                pid = self.app_context.backend.pane_pid(record)
                record = self.app_context.store.update(
                    record.id,
                    status="running",
                    last_pid=pid,
                )
                self.open_session(
                    record,
                    known_live=True,
                    known_pid=pid,
                    known_pid_resolved=True,
                    refresh_side=False,
                )
            elif callable(create_at_grid):
                # An empty window has no prior terminal from which to inherit a
                # grid. Create its VTE page first and let open_session launch the
                # payload at that page's settled allocation. Async failure owns
                # the same rollback contract as the synchronous path below.
                self.open_session(
                    record,
                    known_live=False,
                    known_pid=None,
                    known_pid_resolved=True,
                    refresh_side=False,
                    rollback_new_record_on_failure=True,
                )
            else:
                # Compatibility for non-production backends that implement only
                # the historical synchronous API.
                self.app_context.backend.create_or_resume(record)
                pid = self.app_context.backend.pane_pid(record)
                record = self.app_context.store.update(
                    record.id,
                    status="running",
                    last_pid=pid,
                )
                self.open_session(
                    record,
                    known_live=True,
                    known_pid=pid,
                    known_pid_resolved=True,
                    refresh_side=False,
                )
        except Exception:
            # A replacement is one atomic user action.  If its tmux/session-row
            # construction only partly succeeds, remove only that newly-created
            # target so retrying cannot accumulate duplicate shells.
            try:
                self._remove_tab_widget(record.id)
            except Exception:
                pass
            try:
                self.app_context.backend.terminate(record)
            except Exception:
                pass
            try:
                self.app_context.store.delete(record.id)
            except Exception:
                pass
            raise
        self.side.refresh()
        return record


    def restore_previous_tabs(self, records: list[SessionRecord] | None = None) -> int:
        """Reattach live tabs and relaunch remembered crash-recovery tabs."""
        retry_ids = getattr(self, "_restore_retry_ids", None)
        if not isinstance(retry_ids, set):
            retry_ids = set()
            self._restore_retry_ids = retry_ids
        restored = 0
        restore_records = sorted(records, key=lambda row: row.tab_index) if records is not None else self.app_context.store.restore_candidates(
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
        failed_ids: set[str] = set()
        for record in restore_records:
            if record.id in self.tabs:
                continue
            before = len(self.tabs)
            try:
                self.open_session(
                    record,
                    restart_missing=True,
                    remember_layout=remember_layout,
                    known_live=live_by_id.get(record.id) if live_by_id is not None else None,
                    known_pid=pids_by_id.get(record.id),
                    known_pid_resolved=pids_resolved,
                    refresh_side=False,
                    retry_start_failure=True,
                )
            except Exception:
                # One unavailable command/socket must not abort construction of
                # the window or hide the rest of its remembered tabs. Keep the
                # failed row visible so a later Restore/startup can retry it.
                # AttachTerminal/VTE can raise ValueError, RuntimeError, or a
                # GI exception in addition to the backend's OSError/TmuxError;
                # all of those are per-row failures, not window failures.
                failed_ids.add(record.id)
                failure_status = (
                    "recovery-pending"
                    if str(getattr(record, "status", "")).casefold() == "recovery-pending"
                    else "start-error"
                )
                self.app_context.store.update(
                    record.id,
                    status=failure_status,
                    last_pid=None,
                    visible=bool(getattr(record, "visible", True)),
                )
                continue
            if len(self.tabs) > before:
                restored += 1
                retry_ids.discard(record.id)
        if failed_ids:
            retry_ids.update(failed_ids)
            schedule_retry = getattr(self, "_schedule_restore_retry", None)
            if callable(schedule_retry):
                schedule_retry()
        elif not retry_ids:
            self._restore_retry_attempts = 0
        self.side.refresh()
        return restored

    def _schedule_restore_retry(self) -> None:
        """Retry only rows whose notebook client was not materialized."""

        if getattr(self, "_restore_retry_source_id", 0):
            return
        # A bounded retry window avoids a permanent loop when a backend is
        # genuinely unavailable while still covering slow VTE/GTK startup.
        attempts = int(getattr(self, "_restore_retry_attempts", 0))
        retry_ids = getattr(self, "_restore_retry_ids", set())
        if attempts >= 30:
            retry_ids.clear()
            self._restore_retry_attempts = 0
            return
        try:
            self._restore_retry_source_id = GLib.timeout_add(
                2000,
                self._retry_restore_tabs,
            )
        except Exception:
            self._restore_retry_source_id = 0

    def _retry_restore_tabs(self) -> bool:
        self._restore_retry_source_id = 0
        self._restore_retry_attempts += 1
        retry_ids = getattr(self, "_restore_retry_ids", set())
        ids = tuple(retry_ids)
        if not ids:
            self._restore_retry_attempts = 0
            return False
        rows = []
        for session_id in ids:
            if session_id in self.tabs:
                retry_ids.discard(session_id)
                continue
            record = self.app_context.store.get(session_id)
            if record is not None and bool(getattr(record, "visible", True)):
                rows.append(record)
            else:
                retry_ids.discard(session_id)
        if rows:
            self.restore_previous_tabs(records=rows)
        if retry_ids and self._restore_retry_attempts < 30:
            self._schedule_restore_retry()
            return False
        retry_ids.clear()
        self._restore_retry_attempts = 0
        return False

    def reopen_last_closed_tab(self) -> int:
        """Browser-style reopen for the most recently hidden terminal tab."""

        records = recently_closed_records(self.app_context.store, limit=1)
        if not records:
            return 0
        record = records[0]
        try:
            live = bool(self.app_context.backend.session_exists(record))
        except (OSError, TmuxError):
            live = True
        if not live:
            reopened = 1 if self.open_user_session(record) else 0
            self.side.refresh()
            if self.master_window is not None and self.master_window.get_visible():
                self.master_window.refresh()
            return reopened
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
            self.open_session(
                record,
                restart_missing=False,
                remember_layout=False,
                known_live=True,
            )
            if record.id in self.tabs or len(self.tabs) > before:
                reopened += 1
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        return reopened

    def open_user_session(self, record: SessionRecord) -> bool:
        """Open a selected tab without replaying or downgrading lost state."""

        current = self.app_context.store.get(record.id)
        if current is None:
            self.show_toast("That terminal is no longer in the session store", kind="error")
            return False
        record = current
        try:
            live = bool(self.app_context.backend.session_exists(record))
        except (OSError, TmuxError) as exc:
            self.show_toast(f"Could not check {record.name}: {exc}", kind="error")
            return False
        if live:
            try:
                self.open_session(record, known_live=True)
            except Exception as exc:
                self.show_toast(f"Could not open {record.name}: {exc}", kind="error")
                return False
            return True

        try:
            durable_recipe = recipe_from_record(record)
        except SessionRecipeError:
            durable_recipe = None
        explicit_recovery = bool((record.recovery_command or "").strip()) or bool(
            durable_recipe is not None
            and durable_recipe.origin in {"legacy-recovery", "native-resume"}
        )
        status = record.status.casefold()
        managed_recovery = (
            not record.external
            and automatic_recovery_allowed(durable_recipe)
            and status != "archived"
            and (status != "stopped" or explicit_recovery)
        )
        if managed_recovery:
            # Use an ephemeral recovery state so a failed attempt cannot mutate
            # durable layout/status. The backend validates and executes only the
            # canonical recipe (or an optional stronger checkpoint/native resume)
            # without knowing which application owns the terminal.
            recovery_record = (
                record
                if record.status.casefold() == "recovery-pending"
                else replace(record, status="recovery-pending")
            )
            try:
                create_at_grid = getattr(
                    self.app_context.backend,
                    "create_or_resume_at_grid",
                    None,
                )
                notebook = getattr(self, "notebook", None)
                get_current_page = getattr(notebook, "get_current_page", None)
                get_nth_page = getattr(notebook, "get_nth_page", None)
                page = (
                    get_nth_page(get_current_page())
                    if callable(get_current_page) and callable(get_nth_page)
                    else None
                )
                terminal = getattr(page, "terminal", None)
                columns = (
                    max(0, int(terminal.get_column_count()))
                    if terminal is not None
                    else 0
                )
                rows = (
                    max(0, int(terminal.get_row_count()))
                    if terminal is not None
                    else 0
                )
                if callable(create_at_grid) and columns >= 2 and rows >= 2:
                    create_at_grid(
                        recovery_record,
                        columns=columns,
                        rows=rows,
                    )
                else:
                    self.app_context.backend.create_or_resume(recovery_record)
                self._remove_tab_widget(record.id)
                self.open_session(recovery_record, known_live=True)
            except Exception as exc:
                self.show_toast(
                    f"Could not recover {record.name}: {exc}",
                    kind="error",
                )
                self.side.refresh()
                return False
            self.show_toast(f"Recovered {record.name}", kind="success")
            return True

        replacements = getattr(self, "_missing_session_replacements", None)
        if replacements is None:
            replacements = set()
            self._missing_session_replacements = replacements
        if record.id in replacements:
            return True

        directory = normalise_directory(record.cwd)
        if not Path(directory).is_dir():
            self.show_toast(
                f"Original session ended and its saved directory is unavailable: {directory}",
                kind="error",
            )
            self.side.refresh()
            return False
        replacements.add(record.id)
        try:
            self.new_shell_tab(directory)
        except Exception as exc:
            replacements.discard(record.id)
            self.show_toast(f"Could not open a replacement shell in {directory}: {exc}", kind="error")
            self.side.refresh()
            return False
        try:
            self.app_context.store.update(
                record.id,
                status="stopped",
                visible=False,
                last_pid=None,
            )
        except KeyError:
            # The replacement is already usable and the stale source was
            # concurrently removed. Keep the completion guard so another stale
            # activation cannot create a second shell.
            pass
        except Exception as exc:
            self.show_toast(
                f"Opened a replacement shell, but could not retire {record.name}: {exc}",
                kind="warning",
            )
            self.side.refresh()
            return True
        self._remove_tab_widget(record.id)
        self.show_toast(
            f"Original session ended; opened a new shell in {directory}",
            kind="info",
        )
        return True

    def _attach_client_should_retry(self, session_id: str) -> bool | None:
        """Classify one exited tmux client without confusing it with its task."""

        # Auth activation intentionally replaces the tmux session. Its attach
        # client may observe the brief gap between terminate and recreate.
        if (
            session_id in self._auth_switch_inflight
            or session_id in getattr(self, "_payload_restart_inflight", set())
        ):
            return None
        record = self.app_context.store.get(session_id)
        if record is None:
            getattr(self, "_auth_attach_protected", set()).discard(session_id)
            return False
        if session_id in getattr(self, "_auth_attach_protected", set()):
            try:
                if self.app_context.backend.session_exists(record):
                    # Keep the protection until the new attach client has
                    # survived its stability interval. A later child exit in
                    # that interval is still part of the same transition.
                    return True
            except (OSError, TmuxError):
                # Liveness is inconclusive: never let a switch-induced gap
                # turn into a destructive missing-tab finalizer.
                return None
            # The server-side replacement has not appeared yet. Keep the VTE
            # deferred and the notebook tab visible; the auth poll retries this
            # exact target without recreating or hiding anything.
            return None
        try:
            if self.app_context.backend.session_exists(record):
                return True
        except (OSError, TmuxError):
            return True
        GLib.idle_add(self._finalize_missing_attach_target, session_id)
        return False

    def _resolve_auth_attach_gap(self, session_id: str) -> None:
        tab = self.tabs.get(session_id)
        if tab is None:
            return
        resolver = getattr(tab.widget, "resolve_deferred_attach", None)
        if callable(resolver):
            resolver()

    def _on_auth_attach_stable(self, session_id: str) -> None:
        """Release a no-loss auth-transition guard after a stable reattach."""

        getattr(self, "_auth_attach_protected", set()).discard(session_id)
        refresh_models = getattr(self, "_refresh_gateway_model_control", None)
        if callable(refresh_models):
            refresh_models(force=True)

    def _finalize_missing_attach_target(self, session_id: str) -> bool:
        record = self.app_context.store.get(session_id)
        if record is None:
            return False
        external = bool(record.external)
        replaceable_missing = bool(
            not external
            and record.status.casefold() in {"missing", "stopped"}
            and not (record.recovery_command or "").strip()
            and not (record.recovery_cwd or "").strip()
        )
        try:
            updated = self.app_context.store.update(
                session_id,
                status="missing" if external or replaceable_missing else "recovery-pending",
                visible=False if external or replaceable_missing else record.visible,
                last_pid=None,
                last_opened_at=utc_now(),
            )
        except KeyError:
            return False
        self._remove_tab_widget(session_id)
        self._renumber_visible_tabs()
        self.side.refresh()
        if self.master_window is not None and self.master_window.get_visible():
            self.master_window.refresh()
        if external or replaceable_missing:
            self.show_toast(
                f"{updated.name} ended; reopen it to start a managed shell in its saved directory",
                kind="warning",
            )
        else:
            self.show_toast(
                f"{updated.name} ended; exact recovery is pending",
                kind="warning",
            )
        return False

    def observed_status(self, record: SessionRecord, pid: int | None) -> str:
        if getattr(self.app_context.backend, "pane_snapshot_stale", False):
            if record.status == "paused":
                return "paused"
            if record.status in {"running", "missing"}:
                return "running"
        snap = self.app_context.sampler.snapshot(pid)
        snap = merge_confirmed_pane_snapshot(
            snap,
            pane_pid=pid,
            pane_confirmed=pid is not None
            and not getattr(self.app_context.backend, "pane_snapshot_stale", False),
        )
        if snap.exists:
            return "paused" if snap.state == "T" or (snap.state == "unknown" and record.status == "paused") else "running"
        if record.status == "paused":
            return "paused"
        return record.status if record.status in {"new", "recovery-pending", "stopped"} else "stopped"

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
        retry_start_failure: bool = False,
        rollback_new_record_on_failure: bool = False,
    ) -> None:
        live_session = (
            self.app_context.backend.session_exists(record)
            if known_live is None
            else known_live
        )
        if not live_session and not restart_missing:
            self.app_context.store.update(
                record.id,
                status="stopped",
                last_pid=None,
                visible=False,
            )
            if refresh_side:
                self.side.refresh()
            return

        # Missing GUI targets are intentionally not started here.  VTE has not
        # received its final allocation yet, and tmux would otherwise launch a
        # full-screen application at its implicit 80x24 before the first GTK
        # resize.  AttachTerminal invokes ``prepare_target`` exactly once after
        # the grid settles, then creates the control-mode attachment.
        launch_after_allocation = not live_session
        pid = None
        if live_session:
            pid = (
                known_pid
                if known_live is not None and known_pid_resolved
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
        configure = getattr(
            self.app_context.backend,
            "configure_interactive_session",
            None,
        )
        if live_session and callable(configure):
            configure(record)

        def prepare_target(columns: int, rows: int, *, session_id=record.id) -> bool:
            current = self.app_context.store.get(session_id)
            if current is None:
                raise TmuxError("terminal record disappeared before startup")
            create_at_grid = getattr(
                self.app_context.backend,
                "create_or_resume_at_grid",
                None,
            )
            if callable(create_at_grid):
                create_at_grid(current, columns=columns, rows=rows)
            else:
                # Third-party test/backends retain their existing API.  The
                # production TmuxBackend always provides the exact-grid method.
                self.app_context.backend.create_or_resume(current)
            if callable(configure):
                configure(current)
            new_pid = self.app_context.backend.pane_pid(current)
            updated = self.app_context.store.update(
                session_id,
                status=self.observed_status(current, new_pid),
                last_pid=new_pid,
                auth_restart_required=False,
            )
            tab = self.tabs.get(session_id)
            if tab is not None:
                tab.record = updated
            if refresh_side:
                self.side.refresh()
            return True

        def prepare_target_failed(exc: Exception, *, session_id=record.id) -> None:
            def finalize() -> bool:
                current = self.app_context.store.get(session_id)
                self._remove_tab_widget(session_id)
                if rollback_new_record_on_failure:
                    # This row/target was created solely for one New Tab action.
                    # Roll back both halves even when tmux creation succeeded but
                    # a later configure/PID step failed asynchronously.
                    if current is not None:
                        try:
                            self.app_context.backend.terminate(current)
                        except Exception:
                            pass
                    try:
                        self.app_context.store.delete(session_id)
                    except Exception:
                        pass
                else:
                    failure_status = (
                        "recovery-pending"
                        if current is not None
                        and str(current.status).casefold() == "recovery-pending"
                        else "start-error"
                    )
                    try:
                        self.app_context.store.update(
                            session_id,
                            status=failure_status,
                            last_pid=None,
                            visible=True,
                        )
                    except KeyError:
                        pass
                if retry_start_failure and not rollback_new_record_on_failure:
                    retry_ids = getattr(self, "_restore_retry_ids", None)
                    if not isinstance(retry_ids, set):
                        retry_ids = set()
                        self._restore_retry_ids = retry_ids
                    retry_ids.add(session_id)
                    schedule_retry = getattr(self, "_schedule_restore_retry", None)
                    if callable(schedule_retry):
                        schedule_retry()
                self.show_toast(
                    f"Could not start {record.name}: {exc}",
                    kind="error",
                )
                if refresh_side:
                    self.side.refresh()
                return False

            GLib.idle_add(finalize)

        def current_callback_record(
            session_id: str = record.id,
            fallback: SessionRecord = record,
        ) -> SessionRecord:
            # Deferred startup replaces the provisional `new` row with its live
            # PID/status after tmux is created. Input, scrolling, and selection
            # callbacks must resolve that current row instead of retaining the
            # pre-launch snapshot; otherwise the first chrome-routed key can be
            # consumed by a false "live restore failed" decision.
            return self.app_context.store.get(session_id) or fallback

        resize_handler = _private_resize_handler(
            self.app_context.backend,
            record,
        )

        terminal = AttachTerminal(
            self.app_context.backend.attach_command(record),
            record.cwd,
            self.app_context.theme,
            key_press_handler=self.on_terminal_key_press,
            live_focus_handler=lambda: self._restore_live_terminal(
                current_callback_record()
            ),
            scroll_handler=lambda lines: self._scroll_terminal(
                current_callback_record(),
                lines,
            ),
            selection_history_handler=lambda: self._selection_history_for_terminal(
                current_callback_record()
            ),
            attach_exit_handler=lambda sid=record.id: self._attach_client_should_retry(sid),
            attach_stable_handler=lambda sid=record.id: self._on_auth_attach_stable(sid),
            pane_dead_handler=lambda sid=record.id: self._on_live_payload_dead(sid),
            scrollback_extent=getattr(
                self.app_context.backend,
                "history_limit",
                self.app_context.theme.scrollback_lines,
            ),
            font_override=self.zoomed_font(),
            prepare_target_handler=(prepare_target if launch_after_allocation else None),
            prepare_target_error_handler=(
                prepare_target_failed if launch_after_allocation else None
            ),
            resize_applied_handler=resize_handler,
            repair_on_setup=live_session,
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

    def _restore_live_terminal(self, record: SessionRecord) -> bool:
        restore = getattr(self.app_context.backend, "restore_live_session", None)
        if not callable(restore):
            return True
        try:
            decision = restore(record)
        except Exception:
            return False
        return decision is not False

    def _scroll_terminal(self, record: SessionRecord, lines: int) -> ScrollOutcome:
        scroll = getattr(self.app_context.backend, "scroll_session", None)
        if not callable(scroll):
            return unhandled_scroll(lines, detail="backend has no history controller")
        outcome = scroll(record, lines)
        return (
            outcome
            if isinstance(outcome, ScrollOutcome)
            else unhandled_scroll(lines, detail="invalid backend scroll outcome")
        )

    def _selection_history_for_terminal(self, record: SessionRecord) -> str:
        """Return a bounded text snapshot for continuous selection review."""

        capture = getattr(self.app_context.backend, "capture", None)
        if not callable(capture):
            return ""
        lines = max(
            100,
            int(
                getattr(
                    self.app_context.backend,
                    "history_limit",
                    self.app_context.theme.scrollback_lines,
                )
            ),
        )
        try:
            return str(capture(record, lines=lines) or "")
        except Exception:
            return ""

    def _cleanup_recipe_geometry_probe(self) -> None:
        probe = getattr(self, "_recipe_geometry_probe", None)
        if probe is None:
            return
        self._recipe_geometry_probe = None
        try:
            page = self.notebook.page_num(probe)
            if page >= 0:
                self.notebook.remove_page(page)
        except Exception:
            pass
        try:
            probe.destroy()
        except Exception:
            try:
                probe.detach()
            except Exception:
                pass
        try:
            self.notebook.set_show_tabs(
                bool(getattr(self, "_recipe_geometry_probe_show_tabs", True))
            )
        except Exception:
            pass
        if bool(getattr(self, "_recipe_geometry_probe_window_hidden", False)):
            self._recipe_geometry_probe_window_hidden = False
            try:
                self.set_opacity(1.0)
            except Exception:
                pass

    def _recipe_geometry_widget(self):
        probe = getattr(self, "_recipe_geometry_probe", None)
        if probe is not None:
            return probe
        try:
            page = self.notebook.get_current_page()
            widget = self.notebook.get_nth_page(page)
        except Exception:
            widget = None
        if widget is not None and getattr(widget, "terminal", None) is not None:
            return widget
        records = getattr(self, "_recipe_hidden_records", ())
        if not records:
            return None
        # An all-hidden recipe still needs the exact VTE + margins + scrollbar
        # allocation that a later visible tab will receive. This transient page
        # has no session row, tmux target, or control stream.
        probe = AttachTerminal(
            [],
            records[0].cwd,
            self.app_context.theme,
            scrollback_extent=getattr(
                self.app_context.backend,
                "history_limit",
                self.app_context.theme.scrollback_lines,
            ),
            font_override=self.zoomed_font(),
            connect_enabled=False,
        )
        self._recipe_geometry_probe = probe
        try:
            self._recipe_geometry_probe_show_tabs = bool(
                self.notebook.get_show_tabs()
            )
        except Exception:
            self._recipe_geometry_probe_show_tabs = True
        # Keep the real tab strip in the layout: hiding it yields two extra rows
        # and would resize a background TUI when the first real tab is opened.
        # Suppress the transient geometry page visually instead of changing the
        # measured allocation.
        try:
            self.set_opacity(0.0)
            self._recipe_geometry_probe_window_hidden = True
        except Exception:
            self._recipe_geometry_probe_window_hidden = False
        label = Gtk.Label(label="")
        page = self.notebook.append_page(probe, label)
        probe.show_all()
        self.notebook.set_current_page(page)
        return probe

    def start_hidden_recipe_records(
        self,
        records: list[SessionRecord] | tuple[SessionRecord, ...],
    ) -> None:
        """Start GUI recipe background rows only at one settled window grid."""

        pending = tuple(records)
        if not pending:
            return
        if self._recipe_geometry_source_id or self._recipe_hidden_records:
            raise RuntimeError("hidden workspace recipe launch is already pending")
        self._recipe_hidden_records = pending
        self._recipe_geometry_last_grid = (0, 0)
        self._recipe_geometry_stable_samples = 0
        self._recipe_geometry_deadline = time.monotonic() + 3.0
        self._recipe_hidden_launch_errors = ()
        self._recipe_geometry_widget()
        self._recipe_geometry_source_id = GLib.timeout_add(
            50,
            self._poll_recipe_geometry,
        )

    def _fail_pending_hidden_recipe_records(self, detail: str) -> None:
        errors: list[tuple[str, str]] = []
        records = self._recipe_hidden_records
        self._recipe_hidden_records = ()
        for record in records:
            current = self.app_context.store.get(record.id)
            if current is None:
                continue
            try:
                self.app_context.store.update(
                    current.id,
                    status="start-error",
                    last_pid=None,
                    visible=False,
                )
            except Exception:
                pass
            errors.append((current.id, detail))
        self._recipe_hidden_launch_errors = tuple(errors)

    def _launch_hidden_recipe_records_at_grid(
        self,
        records: tuple[SessionRecord, ...],
        *,
        columns: int,
        rows: int,
    ) -> None:
        create_at_grid = getattr(
            self.app_context.backend,
            "create_or_resume_at_grid",
            None,
        )
        if not callable(create_at_grid):
            detail = "backend has no exact-grid creation surface"
            errors: list[tuple[str, str]] = []
            for record in records:
                current = self.app_context.store.get(record.id)
                if current is None:
                    continue
                try:
                    self.app_context.store.update(
                        current.id,
                        status="start-error",
                        last_pid=None,
                        visible=False,
                    )
                except Exception:
                    pass
                errors.append((current.id, detail))
            self._recipe_hidden_launch_errors = tuple(errors)
            return
        errors: list[tuple[str, str]] = []
        for record in records:
            current = self.app_context.store.get(record.id)
            if current is None:
                continue
            try:
                create_at_grid(current, columns=columns, rows=rows)
                pid = self.app_context.backend.pane_pid(current)
                self.app_context.store.update(
                    current.id,
                    status="running",
                    last_pid=pid,
                    visible=False,
                    auth_restart_required=False,
                )
            except Exception as exc:
                # A recipe row is durable, but a partially created tmux target is
                # not. Remove only that target and retain the hidden row for an
                # explicit later retry.
                try:
                    self.app_context.backend.terminate(current)
                except Exception:
                    pass
                try:
                    self.app_context.store.update(
                        current.id,
                        status="start-error",
                        last_pid=None,
                        visible=False,
                    )
                except Exception:
                    pass
                errors.append((current.id, str(exc)))
        self._recipe_hidden_launch_errors = tuple(errors)

    def _poll_recipe_geometry(self) -> bool:
        if not getattr(self, "_tab_directory_refresh_alive", False):
            self._recipe_geometry_source_id = 0
            self._recipe_hidden_records = ()
            self._cleanup_recipe_geometry_probe()
            return False
        widget = self._recipe_geometry_widget()
        terminal = getattr(widget, "terminal", None)
        try:
            grid = (
                max(0, int(terminal.get_column_count())),
                max(0, int(terminal.get_row_count())),
            )
        except Exception:
            grid = (0, 0)
        if grid[0] >= 2 and grid[1] >= 2:
            if grid == self._recipe_geometry_last_grid:
                self._recipe_geometry_stable_samples += 1
            else:
                self._recipe_geometry_last_grid = grid
                self._recipe_geometry_stable_samples = 1
            if self._recipe_geometry_stable_samples >= 2:
                self._recipe_geometry_source_id = 0
                self._recipe_canonical_grid = grid
                records = self._recipe_hidden_records
                self._recipe_hidden_records = ()
                self._launch_hidden_recipe_records_at_grid(
                    records,
                    columns=grid[0],
                    rows=grid[1],
                )
                self._cleanup_recipe_geometry_probe()
                self.side.refresh()
                if self._recipe_hidden_launch_errors:
                    self.show_toast(
                        f"Could not start {len(self._recipe_hidden_launch_errors)} hidden workspace tab(s)",
                        kind="warning",
                    )
                return False
        if time.monotonic() >= self._recipe_geometry_deadline:
            self._recipe_geometry_source_id = 0
            self._fail_pending_hidden_recipe_records(
                "canonical terminal geometry did not settle"
            )
            self._cleanup_recipe_geometry_probe()
            self.side.refresh()
            self.show_toast(
                "Hidden workspace tabs were not started because terminal geometry did not settle",
                kind="warning",
            )
            return False
        return True

    def _sync_current_terminal_geometry(self) -> bool:
        self._terminal_layout_sync_source_id = 0
        page_num = self.notebook.get_current_page()
        visible = self.notebook.get_nth_page(page_num)
        if visible is None or bool(getattr(visible, "_detached", False)):
            return False
        terminal = getattr(visible, "terminal", None)
        if terminal is None:
            return False
        try:
            columns = max(0, int(terminal.get_column_count()))
            rows = max(0, int(terminal.get_row_count()))
        except Exception:
            return False
        if columns < 2 or rows < 2:
            return False
        # Gtk.Notebook does not allocate hidden pages when the window settles.
        # Use the mapped page as the canonical grid and explicitly resize every
        # live VTE/PTy pair so tmux never retains a hidden page's stale width.
        for tab in list(self.tabs.values()):
            widget = tab.widget
            if bool(getattr(widget, "_detached", False)):
                continue
            apply_grid = getattr(widget, "apply_canonical_grid", None)
            if callable(apply_grid):
                apply_grid(columns=columns, rows=rows)
        return False

    def _on_terminal_layout_size_allocate(self, *_args) -> None:
        self._queue_current_terminal_geometry_sync()

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

    def _prepare_switched_terminal(self, page) -> bool:
        current_page = self.notebook.get_current_page()
        if self.notebook.get_nth_page(current_page) is not page:
            return False
        prepare = getattr(page, "prepare_for_live_input", None)
        if callable(prepare):
            # This clears stale/untracked tmux copy mode and synchronizes the
            # newly mapped VTE grid after GTK has allocated the selected page.
            prepare()
        return False

    def _on_notebook_switch_page(self, _notebook, page, _page_num) -> None:
        GLib.idle_add(self._refresh_auth_selector)
        refresh_models = getattr(self, "_refresh_gateway_model_control", None)
        if callable(refresh_models):
            GLib.idle_add(refresh_models, True)
        GLib.idle_add(self._prepare_switched_terminal, page)

    def _refresh_gateway_model_control(self, force: bool = False) -> bool:
        """Render model-cache freshness for the current gateway-backed tab."""

        button = getattr(self, "_gateway_models_button", None)
        if button is None:
            return False
        record = self.current_record()
        if record is None or not record_uses_gateway_models(record):
            button.set_sensitive(False)
            button.hide()
            button.get_style_context().remove_class("suggested-action")
            self._gateway_model_probe_session_id = None
            self._gateway_model_status = None
            return False

        button.show()
        now = time.monotonic()
        status = self._gateway_model_status
        if (
            force
            or self._gateway_model_probe_session_id != record.id
            or status is None
            or now - float(getattr(self, "_gateway_model_probe_at", 0.0)) >= 10.0
        ):
            try:
                status = classify_gateway_model_status(
                    record,
                    process_started_at=process_start_epoch(record.last_pid),
                )
            except Exception as exc:
                status = GatewayModelStatus(
                    applicable=True,
                    available=False,
                    stale=False,
                    detail=str(exc),
                )
            self._gateway_model_probe_session_id = record.id
            self._gateway_model_probe_at = now
            self._gateway_model_status = status

        busy = bool(
            record.id in getattr(self, "_payload_restart_inflight", set())
            or record.id in getattr(self, "_auth_switch_inflight", set())
        )
        context = button.get_style_context()
        if busy:
            button.set_sensitive(False)
            button.set_tooltip_text(
                f"{record.name}: model/session refresh is in progress"
            )
            context.remove_class("suggested-action")
            return False
        if status is None or not status.available:
            button.set_sensitive(False)
            detail = status.detail if status is not None else "model inventory unavailable"
            button.set_tooltip_text(f"{record.name}: {detail}")
            context.remove_class("suggested-action")
            return False
        if status.stale:
            button.set_sensitive(True)
            button.set_tooltip_text(
                f"{status.model_count} gateway models are ready. Refresh this exact "
                "conversation in the same tab to load them."
            )
            context.add_class("suggested-action")
        else:
            button.set_sensitive(False)
            button.set_tooltip_text(
                f"{status.model_count} gateway models are current in this tab"
            )
            context.remove_class("suggested-action")
        return False

    def _on_gateway_models_refresh(self, *_args) -> None:
        record = self.current_record()
        if record is None or not record_uses_gateway_models(record):
            return
        self._refresh_gateway_model_control(force=True)
        status = getattr(self, "_gateway_model_status", None)
        if status is None or not status.available:
            self.show_toast(
                f"Could not validate the gateway model inventory for {record.name}",
                kind="error",
            )
            return
        if not status.stale:
            self.show_toast(
                f"{record.name} already has all {status.model_count} gateway models",
                kind="info",
            )
            return
        if self._start_managed_payload_restart(
            record,
            exact_live=True,
            reason="models",
            model_count=status.model_count,
        ):
            self.show_toast(
                f"Refreshing {record.name} in place to load {status.model_count} models…",
                kind="info",
            )
            self._refresh_gateway_model_control(force=True)

    def _on_live_payload_dead(self, session_id: str) -> bool:
        """Immediately recover a crashed managed payload in its existing tab."""

        if time.monotonic() < float(
            getattr(self, "_payload_restart_backoff_until", {}).get(session_id, 0.0)
        ):
            return False
        try:
            record = self.app_context.store.get(session_id)
        except Exception:
            return False
        if record is None or record.external:
            return False
        if session_id in getattr(self, "_auth_switch_inflight", set()):
            return True
        if session_id in getattr(self, "_payload_restart_inflight", set()):
            return True
        if record.status.casefold() in {"archived", "stopped", "terminated", "killed"}:
            return False
        try:
            recipe = recipe_from_record(record)
        except SessionRecipeError:
            return False
        if not automatic_recovery_allowed(recipe):
            return False
        return self._start_managed_payload_restart(
            record,
            exact_live=False,
            reason="dead",
            model_count=0,
        )

    def _start_managed_payload_restart(
        self,
        record: SessionRecord,
        *,
        exact_live: bool,
        reason: str,
        model_count: int,
    ) -> bool:
        """Start one no-loss server-side replacement and protected reattach."""

        session_id = record.id
        if (
            session_id in self._auth_switch_inflight
            or session_id in self._payload_restart_inflight
        ):
            return True
        tab = self.tabs.get(session_id)
        widget = getattr(tab, "widget", None) if tab is not None else None
        defer = getattr(widget, "defer_attach_for_target_replacement", None)
        if not callable(defer):
            return False

        generation = self._payload_restart_generation.get(session_id, 0) + 1
        self._payload_restart_generation[session_id] = generation
        self._payload_restart_inflight.add(session_id)
        self._auth_attach_protected.add(session_id)
        try:
            deferred = bool(defer())
        except Exception:
            deferred = False
        if not deferred:
            self._payload_restart_inflight.discard(session_id)
            self._auth_attach_protected.discard(session_id)
            return False

        def worker() -> None:
            replacement_pid: int | None = None
            replacement_action = "restarted"
            replacement_command: str | None = None
            replacement_cwd: str | None = None
            clear_recovery = False
            error = ""
            try:
                if exact_live:
                    replacement_pid = self.app_context.backend.restart_exact_payload(record)
                else:
                    recovery = self.app_context.backend.recover_dead_payload(record)
                    replacement_pid = recovery.pid
                    replacement_action = recovery.action
                    replacement_command = recovery.command
                    replacement_cwd = recovery.cwd
                    clear_recovery = recovery.clear_recovery
            except Exception as exc:
                error = str(exc)
            payload = (
                session_id,
                generation,
                replacement_pid,
                error,
                reason,
                max(0, int(model_count)),
                replacement_action,
                replacement_command,
                replacement_cwd,
                clear_recovery,
            )
            scheduled, scheduling_error = _schedule_glib_callback(
                self._complete_managed_payload_restart,
                *payload,
            )
            if scheduled:
                return
            with self._payload_restart_completion_lock:
                self._payload_restart_completions.append(payload)
            print(
                f"T1000: payload restart completion for {session_id} will retry "
                f"on the UI poll: {scheduling_error}",
                file=sys.stderr,
            )

        try:
            threading.Thread(
                target=worker,
                name=f"termdeck-payload-{session_id}",
                daemon=False,
            ).start()
        except Exception as exc:
            self._payload_restart_inflight.discard(session_id)
            self._auth_attach_protected.discard(session_id)
            try:
                widget.resolve_deferred_attach()
            except Exception:
                pass
            self.show_toast(
                f"Could not start recovery for {record.name}: {exc}",
                kind="error",
            )
            self._refresh_gateway_model_control(force=True)
            return False
        return True

    def _drain_payload_restart_completions(self) -> None:
        with self._payload_restart_completion_lock:
            pending = self._payload_restart_completions
            self._payload_restart_completions = []
        for payload in pending:
            try:
                self._complete_managed_payload_restart(*payload)
            except Exception as exc:
                print(
                    f"T1000: payload restart completion failed: {exc}",
                    file=sys.stderr,
                )

    def _complete_managed_payload_restart(
        self,
        session_id: str,
        generation: int,
        replacement_pid: int | None,
        error: str,
        reason: str,
        model_count: int,
        replacement_action: str = "restarted",
        replacement_command: str | None = None,
        replacement_cwd: str | None = None,
        clear_recovery: bool = False,
    ) -> bool:
        if generation != self._payload_restart_generation.get(session_id):
            return False
        self._payload_restart_inflight.discard(session_id)
        tab = self.tabs.get(session_id)
        widget = getattr(tab, "widget", None) if tab is not None else None
        record = self.app_context.store.get(session_id)
        if record is None:
            self._auth_attach_protected.discard(session_id)
            return False

        durable_error = ""
        if replacement_pid is not None and not error:
            try:
                changes: dict[str, object] = {
                    "status": "running",
                    "last_pid": replacement_pid,
                    "recovery_last_error": None,
                    "last_opened_at": utc_now(),
                }
                if clear_recovery:
                    if not replacement_command or not replacement_cwd:
                        raise TmuxError(
                            "shell bridge did not provide a durable command and directory"
                        )
                    changes.update(
                        {
                            "command": replacement_command,
                            "cwd": replacement_cwd,
                            "recovery_command": None,
                            "recovery_cwd": None,
                            "restart_policy": "on-failure",
                        }
                    )
                record = self.app_context.store.update(session_id, **changes)
                if tab is not None:
                    tab.record = record
            except Exception as exc:
                durable_error = str(exc)
            self._payload_restart_backoff_until.pop(session_id, None)
            # Keep transition protection until the new control client proves
            # stable, exactly as for an account-driven pane replacement.
            self._auth_attach_protected.add(session_id)
            resolver = getattr(self, "_resolve_auth_attach_gap", None)
            if callable(resolver):
                resolver(session_id)
            else:
                TermDeckWindow._resolve_auth_attach_gap(self, session_id)
            if replacement_action == "shell":
                message = f"{record.name} returned to an interactive shell in the same tab"
            elif reason == "models":
                message = f"{record.name} now has all {model_count} gateway models"
            else:
                message = f"Recovered {record.name} in the same tab"
            if durable_error:
                message += f" · state save warning: {durable_error}"
            self.show_toast(
                message,
                kind="warning" if durable_error else "success",
            )
        else:
            # Avoid a hot dead-pane loop. Reattach once so the VTE can publish
            # truthful exit output; a second pane-dead callback during the
            # bounded backoff declines automatic recovery.
            self._payload_restart_backoff_until[session_id] = time.monotonic() + 15.0
            self._auth_attach_protected.discard(session_id)
            try:
                self.app_context.store.update(
                    session_id,
                    status="recovery-pending",
                    recovery_last_error=error or "payload restart failed",
                    last_opened_at=utc_now(),
                )
            except Exception:
                pass
            resolver = getattr(self, "_resolve_auth_attach_gap", None)
            if callable(resolver):
                resolver(session_id)
            else:
                TermDeckWindow._resolve_auth_attach_gap(self, session_id)
            detail = error or "payload restart failed"
            self.show_toast(
                f"Could not recover {record.name}: {detail}",
                kind="error",
            )
        self._refresh_gateway_model_control(force=True)
        try:
            self.side.refresh()
        except Exception:
            pass
        return False

    def _codex_auth_manager(self) -> CodexAuthManager:
        getter = getattr(self.app_context, "get_codex_auth_manager", None)
        if callable(getter):
            return getter()
        manager = getattr(self.app_context, "codex_auth", None)
        if manager is None:
            raise CodexAuthError("optional Codex account integration is unavailable")
        return manager

    def _sync_observed_auth_state(
        self,
        record: SessionRecord,
        state: CodexAuthState,
    ) -> SessionRecord:
        if state.selected_account == record.selected_account:
            return record
        pending = record.pending_account
        if state.selected_account is not None or record.selected_account is not None:
            pending = None
        updated = self.app_context.store.update(
            record.id,
            selected_account=state.selected_account,
            pending_account=pending,
            auth_restart_required=True,
            auth_changed_at=utc_now(),
        )
        tab = self.tabs.get(record.id)
        if tab is not None:
            tab.record = updated
        return updated

    def _render_auth_selector(
        self,
        record: SessionRecord | None,
        state: CodexAuthState | None,
        *,
        error: str = "",
        environment_ready: bool = True,
    ) -> None:
        combo = self._auth_combo
        self._auth_combo_updating = True
        try:
            combo.remove_all()
            if record is None:
                combo.append("__none__", "Codex: no terminal")
                combo.set_active_id("__none__")
                combo.set_sensitive(False)
                combo.set_tooltip_text("Open a terminal tab to select a Codex account")
                return
            if record.external:
                combo.append("__none__", "Codex: external tmux")
                combo.set_active_id("__none__")
                combo.set_sensitive(False)
                combo.set_tooltip_text(
                    "Recreate this terminal under T1000 before switching accounts; "
                    "a running external tmux environment cannot be retroactively isolated."
                )
                return
            if state is None and (
                not getattr(record, "auth_scope_id", None)
                or not getattr(record, "codex_home", None)
            ):
                combo.append("__none__", "Accounts: optional")
                combo.append("__enable__", "Enable private Codex account integration…")
                combo.set_active_id("__none__")
                combo.set_sensitive(True)
                combo.set_tooltip_text(
                    "This terminal is fully recoverable without Codex. Enable the "
                    "optional private account integration only when you need it."
                )
                return
            if state is None:
                combo.append("__none__", "Codex: unavailable")
                combo.set_active_id("__none__")
                combo.set_sensitive(False)
                combo.set_tooltip_text(error or "Could not read this terminal's private Codex account state")
                return

            pending = record.pending_account
            canonical_binding_ready = bool(
                getattr(state, "canonical_binding_ready", True)
            )
            if pending:
                none_label = f"Codex: {pending} pending"
            elif not canonical_binding_ready and state.selected_account:
                none_label = "Codex: repair saved account binding"
            elif environment_ready:
                none_label = "Codex: no account"
            else:
                none_label = "Codex: choose account · upgrades terminal"
            combo.append("__none__", none_label)
            for account in state.accounts:
                if account.active and not canonical_binding_ready:
                    suffix = " · repair"
                elif account.active and record.auth_restart_required:
                    suffix = " · restart"
                else:
                    suffix = ""
                combo.append(account.account_key, f"{account.label}{suffix}")
            combo.set_active_id(
                (state.selected_account_key or "__none__")
                if environment_ready and canonical_binding_ready
                else "__none__"
            )
            switchable = bool(state.accounts) and state.codex_auth_available and record.id not in self._auth_switch_inflight
            combo.set_sensitive(switchable)
            if not canonical_binding_ready and state.selected_account:
                tooltip = (
                    f"Reselect {state.selected_account} to bind this terminal directly to "
                    "the canonical saved account before Codex starts."
                )
            elif not environment_ready and state.accounts:
                tooltip = (
                    "Choose an account to upgrade this managed terminal to a private Codex environment. "
                    "T1000 will preserve an exact Codex resume or a verified idle shell."
                )
            elif record.auth_restart_required and state.selected_account:
                tooltip = (
                    f"Saved account: {state.selected_account}. Restart the running Codex process "
                    "when its current turn is finished."
                )
            elif state.accounts:
                tooltip = "Switch the saved account only for this terminal's private CODEX_HOME"
            else:
                tooltip = "No saved codex-auth accounts were found"
            combo.set_tooltip_text(tooltip)
        finally:
            self._auth_combo_updating = False

    def _queue_auth_completion_recovery(
        self,
        session_id: str,
        generation: int,
        result: CodexSwitchResult | None,
        activation: AuthActivationResult | None,
        error: str,
        transaction_lock,
        persistence_retry_attempt: int = 0,
    ) -> None:
        payload = (
            session_id,
            generation,
            result,
            activation,
            error,
            transaction_lock,
            persistence_retry_attempt,
        )
        with self._auth_completion_recovery_lock:
            self._auth_completion_recoveries.append(payload)

    def _drain_auth_completion_recoveries(self) -> None:
        """Finish callbacks whose normal GLib handoff could not be registered."""

        with self._auth_completion_recovery_lock:
            pending = self._auth_completion_recoveries
            self._auth_completion_recoveries = []
        for payload in pending:
            try:
                self._complete_auth_switch(*payload)
            except Exception as exc:
                # One malformed/stale callback must never drop the remaining
                # recovery payloads (some may still own scope locks).
                print(f"T1000: auth completion recovery failed: {exc}", file=sys.stderr)

    def _finish_auth_switch_ui(self, session_id: str, *, protect_attach: bool = False) -> None:
        """Release one committed generation's guard and repair its attach/UI."""

        tab = self.tabs.get(session_id)
        tab_widget = getattr(tab, "widget", None) if tab is not None else None
        if protect_attach or bool(
            tab_widget is not None
            and getattr(tab_widget, "_attach_restart_deferred", False)
        ):
            protected = getattr(self, "_auth_attach_protected", None)
            if protected is None:
                protected = set()
                self._auth_attach_protected = protected
            protected.add(session_id)
        self._auth_switch_inflight.discard(session_id)
        try:
            TermDeckWindow._resolve_auth_attach_gap(self, session_id)
        except Exception as exc:
            print(f"T1000: could not resolve auth attach for {session_id}: {exc}", file=sys.stderr)
        try:
            self._refresh_auth_selector()
        except Exception as exc:
            # Durable auth/process state is already committed at this point;
            # a later poll or tab switch can repaint the selector.
            print(f"T1000: could not refresh auth selector for {session_id}: {exc}", file=sys.stderr)

    def _refresh_auth_selector(self) -> bool:
        record = self.current_record()
        if record is None:
            self._render_auth_selector(None, None)
            return False
        if record.external:
            self._render_auth_selector(record, None)
            return False
        if (
            hasattr(record, "auth_scope_id")
            and hasattr(record, "codex_home")
            and (
                not getattr(record, "auth_scope_id", None)
                or not getattr(record, "codex_home", None)
            )
        ):
            self._render_auth_selector(record, None)
            return False
        environment_ready = True
        check_environment = getattr(self.app_context.backend, "private_auth_environment_ready", None)
        if callable(check_environment):
            try:
                environment_ready = bool(check_environment(record))
            except (OSError, TmuxError):
                environment_ready = False
        self._auth_environment_ready[record.id] = environment_ready
        inflight = record.id in getattr(self, "_auth_switch_inflight", set())
        try:
            state = TermDeckWindow._codex_auth_manager(self).state(record)
            # The worker may already have changed the canonical pointer while
            # its matching SessionStore commit is still pending.  Rendering
            # that read-only state is useful, but synchronizing it here would
            # bypass the transaction lock's durable commit boundary.
            if not inflight:
                record = self._sync_observed_auth_state(record, state)
                self._auth_fingerprints[record.id] = state.fingerprint
            self._render_auth_selector(record, state, environment_ready=environment_ready)
        except (CodexAuthError, OSError, ValueError) as exc:
            self._render_auth_selector(
                record,
                None,
                error=str(exc),
                environment_ready=environment_ready,
            )
        return False

    def _poll_codex_auth_state(self) -> bool:
        if not self._tab_directory_refresh_alive:
            return False
        self._drain_auth_completion_recoveries()
        self._drain_payload_restart_completions()
        self._refresh_gateway_model_control()
        # Only managed target transitions receive this non-destructive
        # retry. Ordinary exited tabs retain the normal missing-session path.
        for session_id in tuple(getattr(self, "_auth_attach_protected", set())):
            TermDeckWindow._resolve_auth_attach_gap(self, session_id)
        record = self.current_record()
        if (
            record is None
            or record.external
            or (
                hasattr(record, "auth_scope_id")
                and hasattr(record, "codex_home")
                and (
                    not getattr(record, "auth_scope_id", None)
                    or not getattr(record, "codex_home", None)
                )
            )
            or record.id in self._auth_switch_inflight
        ):
            return True
        try:
            state = TermDeckWindow._codex_auth_manager(self).state(record)
        except (CodexAuthError, OSError, ValueError):
            return True
        environment_ready = True
        check_environment = getattr(self.app_context.backend, "private_auth_environment_ready", None)
        if callable(check_environment):
            try:
                environment_ready = bool(check_environment(record))
            except (OSError, TmuxError):
                environment_ready = False
        if (
            self._auth_fingerprints.get(record.id) != state.fingerprint
            or state.selected_account != record.selected_account
            or self._auth_environment_ready.get(record.id) != environment_ready
        ):
            self._refresh_auth_selector()
        return True

    def _on_auth_account_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._auth_combo_updating:
            return
        record = self.current_record()
        account_key = combo.get_active_id()
        if record is None or record.external or not account_key or account_key == "__none__":
            return
        if record.id in self._auth_switch_inflight:
            return
        if account_key == "__enable__":
            combo.set_sensitive(False)
            self.show_toast(
                f"Enabling optional private account integration for {record.name}…",
                kind="info",
            )
            try:
                record = self.app_context.store.ensure_auth_scope(record.id)
                tab = self.tabs.get(record.id)
                if tab is not None:
                    tab.record = record
                TermDeckWindow._codex_auth_manager(self)
            except (CodexAuthError, OSError, ValueError) as exc:
                self.show_toast(
                    f"Terminal recovery remains available; optional account integration "
                    f"could not be enabled: {exc}",
                    kind="error",
                )
            finally:
                self._refresh_auth_selector()
            return
        check_environment = getattr(self.app_context.backend, "private_auth_environment_ready", None)
        environment_ready = True
        if callable(check_environment):
            try:
                environment_ready = bool(check_environment(record))
            except (OSError, TmuxError):
                environment_ready = False
        try:
            state = TermDeckWindow._codex_auth_manager(self).state(record)
        except (CodexAuthError, OSError, ValueError) as exc:
            self.show_toast(str(exc), kind="error")
            self._refresh_auth_selector()
            return
        account = next((row for row in state.accounts if row.account_key == account_key), None)
        if account is None or (
            environment_ready
            and state.selected_account_key == account_key
            and bool(getattr(state, "canonical_binding_ready", True))
            and not bool(getattr(record, "auth_restart_required", False))
        ):
            return
        previous_pending_account = record.pending_account
        record = self.app_context.store.update(record.id, pending_account=account.label)
        tab = self.tabs.get(record.id)
        if tab is not None:
            tab.record = record
        self._auth_switch_inflight.add(record.id)
        generation = self._auth_switch_generation.get(record.id, 0) + 1
        self._auth_switch_generation[record.id] = generation
        combo.set_sensitive(False)
        attach_deferred = False
        if tab is not None:
            defer = getattr(
                getattr(tab, "widget", None),
                "defer_attach_for_target_replacement",
                None,
            )
            if callable(defer):
                self._auth_attach_protected.add(record.id)
                try:
                    attach_deferred = bool(defer())
                except Exception:
                    attach_deferred = False
                if not attach_deferred:
                    self._auth_attach_protected.discard(record.id)
        self.show_toast(f"Switching {record.name} to {account.label}…", kind="info")

        def _worker() -> None:
            result: CodexSwitchResult | None = None
            activation: AuthActivationResult | None = None
            error = ""
            transaction_lock = None
            try:
                candidate_lock = auth_transaction_lock(record)
                candidate_lock.__enter__()
                transaction_lock = candidate_lock
                # Another CLI or GUI may have completed while this worker was
                # scheduled. Resolve and transact from the fresh canonical
                # binding while holding the cross-process lock.
                locked_state = TermDeckWindow._codex_auth_manager(self).state(record)
                transaction = execute_auth_change(
                    self.app_context.backend,
                    TermDeckWindow._codex_auth_manager(self),
                    record,
                    locked_state,
                    account_key,
                )
                result = transaction.switch_result
                activation = transaction.activation
                error = transaction.error
            except AuthTransactionBusy as exc:
                error = str(exc)
            except Exception as exc:
                error = str(exc)
            scheduled, scheduling_error = _schedule_glib_callback(
                self._complete_auth_switch,
                record.id,
                generation,
                result,
                activation,
                error,
                transaction_lock,
            )
            if scheduled:
                return

            # No GTK callback was registered. The already-running two-second
            # auth poll is the final handoff. Keep the matching scope lock and
            # close/move guard with that payload: releasing either before the
            # SessionStore commit would let a newer transaction race and make
            # this result stale or disappear with a window close.
            recovery_error = (
                f"{error}; " if error else ""
            ) + f"UI completion handoff failed; recovering on the next auth poll · {scheduling_error}"
            self._queue_auth_completion_recovery(
                record.id,
                generation,
                result,
                activation,
                recovery_error,
                transaction_lock,
            )
            print(
                f"T1000: {record.name} account completion remains protected and will "
                f"retry on the auth poll: "
                f"{scheduling_error}",
                file=sys.stderr,
            )

        # Keep Python alive if an unexpected external shutdown bypasses GTK's
        # close/quit guards while the backend is between terminate and recreate.
        try:
            threading.Thread(
                target=_worker,
                name=f"termdeck-auth-{record.id}",
                daemon=False,
            ).start()
        except Exception as exc:
            # No worker ran, so no canonical pointer or process state changed.
            # Restore the pre-click durable/UI state synchronously instead of
            # leaving a permanent pending badge and close/attach guard behind.
            restore_error = ""
            if generation == self._auth_switch_generation.get(record.id):
                try:
                    restored = self.app_context.store.update(
                        record.id,
                        pending_account=previous_pending_account,
                    )
                    tab = self.tabs.get(record.id)
                    if tab is not None:
                        tab.record = restored
                except Exception as restore_exc:
                    restore_error = f"; could not restore pending state: {restore_exc}"
                TermDeckWindow._finish_auth_switch_ui(
                    self,
                    record.id,
                    protect_attach=attach_deferred,
                )
                combo.set_sensitive(True)
            self.show_toast(
                f"Account unchanged: could not start account switch worker: {exc}{restore_error}",
                kind="error",
            )

    def _persist_auth_transaction_result(
        self,
        session_id: str,
        result: CodexSwitchResult,
        activation: AuthActivationResult | None,
    ) -> SessionRecord:
        """Persist pointer/process state before releasing the scope lock."""

        activation_ok = activation is not None and activation.ok
        updates: dict[str, object] = {
            "selected_account": result.state.selected_account,
            "pending_account": None,
            "auth_restart_required": not activation_ok,
            "auth_changed_at": utc_now(),
        }
        if activation is not None:
            replacement_command = getattr(activation, "command", None)
            replacement_cwd = getattr(activation, "cwd", None)
            clear_recovery = bool(getattr(activation, "clear_recovery", False))
            if replacement_command is not None:
                updates["command"] = replacement_command
            if replacement_cwd is not None:
                updates["cwd"] = replacement_cwd
            if clear_recovery:
                updates["recovery_command"] = None
                updates["recovery_cwd"] = None
            elif activation.recovery_command is not None:
                updates["recovery_command"] = activation.recovery_command
                updates["recovery_cwd"] = activation.recovery_cwd
            if activation.status is not None:
                updates["status"] = activation.status
                updates["last_pid"] = activation.pid
        return self.app_context.store.update(session_id, **updates)

    def _complete_auth_switch(
        self,
        session_id: str,
        generation: int,
        result: CodexSwitchResult | None,
        activation: AuthActivationResult | None,
        error: str,
        transaction_lock=None,
        persistence_retry_attempt: int = 0,
    ) -> bool:
        def _release_transaction_lock() -> None:
            if transaction_lock is not None:
                try:
                    transaction_lock.__exit__(None, None, None)
                except Exception as exc:
                    print(
                        f"T1000: could not release auth transaction lock for {session_id}: {exc}",
                        file=sys.stderr,
                    )

        def _retry_persistence(exc: Exception, *, name: str) -> bool:
            if not persistence_retry_attempt:
                try:
                    self.show_toast(
                        f"{name}: could not save Codex account state; "
                        f"retrying while this terminal remains protected · {exc}",
                        kind="error",
                    )
                except Exception as toast_exc:
                    # Status rendering must never prevent registration of the
                    # durable-state retry that owns this lock and guard.
                    print(
                        f"T1000: could not show auth persistence retry status "
                        f"for {session_id}: {toast_exc}",
                        file=sys.stderr,
                    )
            retry_delay_ms = min(5000, 500 * (2 ** min(persistence_retry_attempt, 4)))
            scheduled, scheduling_error = _schedule_glib_callback(
                self._complete_auth_switch,
                session_id,
                generation,
                result,
                activation,
                error,
                transaction_lock,
                persistence_retry_attempt + 1,
                delay_ms=retry_delay_ms,
            )
            if not scheduled:
                # The independently registered two-second auth poll is the
                # final bounded retry path. Keep the lock and guard in its
                # payload so no credential/process state becomes observable
                # as committed before SessionStore catches up.
                self._queue_auth_completion_recovery(
                    session_id,
                    generation,
                    result,
                    activation,
                    (
                        f"{error}; " if error else ""
                    ) + f"durable auth retry scheduling failed · {scheduling_error}",
                    transaction_lock,
                    persistence_retry_attempt + 1,
                )
                if not persistence_retry_attempt:
                    try:
                        self.show_toast(
                            f"{name}: retry timer unavailable; protected state will retry "
                            "on the next auth poll",
                            kind="error",
                        )
                    except Exception as toast_exc:
                        print(
                            f"T1000: could not show auth poll recovery status "
                            f"for {session_id}: {toast_exc}",
                            file=sys.stderr,
                        )
            return False

        # A delayed callback from an older attempt owns only the lock passed in
        # its own callback payload.  It must not tear down the guard or deferred
        # attach owned by a newer generation for the same terminal.
        if generation != self._auth_switch_generation.get(session_id):
            _release_transaction_lock()
            return False

        try:
            record = self.app_context.store.get(session_id)
        except Exception as exc:
            return _retry_persistence(exc, name="Terminal")
        if record is None:
            _release_transaction_lock()
            if generation == self._auth_switch_generation.get(session_id):
                TermDeckWindow._finish_auth_switch_ui(self, session_id)
            return False

        activation_ok = activation is not None and activation.ok
        transaction_applied = result is not None and (result.ok or activation is not None)
        try:
            if transaction_applied:
                record = self._persist_auth_transaction_result(
                    session_id,
                    result,
                    activation,
                )
            else:
                record = self.app_context.store.update(session_id, pending_account=None)
        except Exception as exc:
            # Keep both the cross-process lock and the per-tab safety guard held
            # until this exact generation has a successful durable write.
            return _retry_persistence(exc, name=record.name)

        # The durable state now matches the credential pointer/process result;
        # the scope lock can be released before ordinary GTK bookkeeping.
        _release_transaction_lock()
        try:
            if transaction_applied:
                self._auth_fingerprints[session_id] = result.state.fingerprint
                tab = self.tabs.get(session_id)
                if tab is not None:
                    tab.record = record
                if error:
                    message = f"{record.name}: account change failed · {error}"
                    kind = "error"
                elif (
                    activation_ok
                    and activation is not None
                    and activation.restarted
                    and activation.recovery_command is not None
                ):
                    message = f"{record.name}: {record.selected_account} active · Codex restarted in this tab"
                    kind = "success"
                elif activation_ok and activation is not None and activation.restarted:
                    message = f"{record.name}: {record.selected_account} active · terminal upgraded"
                    kind = "success"
                elif activation_ok:
                    message = f"{record.name}: {record.selected_account} active for the next Codex launch"
                    kind = "success"
                else:
                    detail = activation.detail if activation is not None else (error or "Codex restart did not complete")
                    message = f"{record.name}: account saved, but not active in running Codex · {detail}"
                    kind = "error"
                self.show_toast(message, kind=kind)
            else:
                detail = error or (result.error if result is not None else "Codex account switch failed")
                self.show_toast(f"Account unchanged: {detail}", kind="error")
        finally:
            if generation == self._auth_switch_generation.get(session_id):
                TermDeckWindow._finish_auth_switch_ui(
                    self,
                    session_id,
                    protect_attach=bool(getattr(activation, "restarted", False)),
                )
        return False

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
        hidden = self.hide_tab(session_id, include_pinned=True)
        if hidden and record is not None:
            self.show_toast(f"Closed {record.name} · session still running", kind="info")

    def hide_current_tab(self) -> None:
        # Backward-compatible method name used by older call sites.
        self.close_current_tab()

    def quit_termdeck(self) -> None:
        self.app_context.quit_preserving_sessions()

    def kill_window(self) -> int:
        """Explicitly stop and forget this window; ordinary close stays durable."""

        return self.app_context.kill_window(self)

    def _remove_tab_widget(self, session_id: str) -> bool:
        getattr(self, "_auth_attach_protected", set()).discard(session_id)
        tab = self.tabs.pop(session_id, None)
        if not tab:
            return False
        page = self.notebook.page_num(tab.widget)
        if page >= 0:
            tab.widget.detach()
            self.notebook.remove_page(page)
        return True

    def _persist_live_directory_before_detach(self, record: SessionRecord) -> SessionRecord:
        """Snapshot a last-second ``cd`` before the tmux client is detached."""

        try:
            live_directory = self.app_context.backend.current_directory(record)
        except (OSError, TmuxError, ValueError):
            return record
        if not live_directory:
            return record
        try:
            changes = self._directory_tracking_changes(record, live_directory)
        except (OSError, ValueError):
            return record
        if not changes:
            return record
        try:
            updated = self.app_context.store.update(
                record.id,
                last_opened_at=record.last_opened_at,
                **changes,
            )
        except KeyError:
            return record
        self.refresh_tab_label(updated)
        return updated

    def hide_tab(self, session_id: str, *, include_pinned: bool = False) -> bool:
        if TermDeckWindow._auth_switch_blocks_action(
            self,
            session_id,
            action="closing or hiding this tab",
        ):
            return False
        record = self.app_context.store.get(session_id)
        if record and record.pinned and not include_pinned:
            return False
        if record is not None:
            record = self._persist_live_directory_before_detach(record)
        if not self._remove_tab_widget(session_id):
            return False
        try:
            if record is not None:
                clear_captain = getattr(self.app_context.store, "clear_window_captain", None)
                if callable(clear_captain):
                    clear_captain(record.window_id, session_id=record.id)
            self.app_context.store.update(session_id, visible=False)
        except KeyError:
            pass
        self._renumber_visible_tabs()
        self.side.refresh()
        return True

    def stop_current_session(self) -> int:
        session_id = self.current_session_id()
        if not session_id:
            return 0
        record = self.app_context.store.get(session_id)
        if record:
            return self.stop_session(record)
        return 0

    def stop_session(self, record: SessionRecord) -> int:
        return self.app_context.terminate_records([record])

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

    def open_process_monitor_window(self) -> None:
        if self.process_monitor_window is not None:
            try:
                if self.process_monitor_window.get_realized() and self.process_monitor_window.get_visible():
                    self.process_monitor_window._scan()
                    self.process_monitor_window.present()
                    return
            except Exception:
                pass
            self.process_monitor_window = None
        self.process_monitor_window = ProcessMonitorWindow(self.app_context)
        self.process_monitor_window.connect(
            "destroy", lambda *_: setattr(self, "process_monitor_window", None)
        )
        self.process_monitor_window.show_all()
        self.process_monitor_window.present()

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
                # Font metrics change the VTE grid even for notebook pages that
                # are not currently mapped. Synchronize the terminal whose font
                # changed; a window-level delayed callback can otherwise follow
                # a tab switch and resize only the newly selected page.
                tab.widget.request_geometry_sync(delay_ms=80)
            except Exception:
                continue

    def zoom_font(self, delta: int) -> None:
        _, base_size = parse_font_description(self.base_font())
        requested_delta = float(self.font_zoom_delta) + int(delta)
        _, actual_size = parse_font_description(
            scale_font_description(self.base_font(), requested_delta)
        )
        # Store the effective delta, not an out-of-range request. Otherwise a
        # user who reaches the zoom floor must press Ctrl++ many times before
        # the font starts growing again.
        self.font_zoom_delta = actual_size - base_size
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
    def _is_plain_ctrl_l_event(event: Gdk.EventKey) -> bool:
        """Reserve plain Ctrl+L as a no-op so shell redraws cannot hide terminal contents."""
        state = KeyMap._clean_state(event.state)
        keyval = Gdk.keyval_to_lower(event.keyval)
        name = (Gdk.keyval_name(keyval) or "").lower()
        return state == int(Gdk.ModifierType.CONTROL_MASK) and (
            keyval in {Gdk.KEY_l, Gdk.KEY_L} or name == "l"
        )

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
        if self.keymap.matches(event, "reopen_closed"):
            self.reopen_last_closed_tab()
            return True
        shortcut_index = index_from_key_name(Gdk.keyval_name(event.keyval) or "")
        state = KeyMap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if shortcut_index is not None and ctrl and not alt and not shift:
            self.jump_to_tab(shortcut_index)
            return True
        # Capture zoom / clipboard before VTE treats the chord as input.
        if self.handle_zoom_event(event):
            return True
        # Plain Ctrl+L is normally forwarded as form-feed (0x0c), which shells
        # interpret as clear/redraw. Keep the current terminal contents visible
        # by consuming only that exact chord; modified Ctrl+L bindings still work.
        if TermDeckWindow._is_plain_ctrl_l_event(event):
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
                if not attach.prepare_for_live_input():
                    # Never paste into a transcript/copy-mode surface whose
                    # live restoration could not be acknowledged.
                    return True
            attach.paste_clipboard()
            return True
        record_getter = getattr(self, "current_record", None)
        record = record_getter() if callable(record_getter) else None
        backend = getattr(getattr(self, "app_context", None), "backend", None)
        active = getattr(backend, "scroll_view_active", None)
        if record is not None and callable(active) and active(record):
            # Do NOT snap back to live when the user presses a bare modifier
            # key (Ctrl, Alt, Super/Win, Shift, …). Modifier-only events carry
            # no child input and therefore do not interrupt history review.
            if not _is_modifier_only_keyval(event.keyval):
                if not attach.prepare_for_live_input():
                    # Consume this key rather than leak it into the transcript
                    # pager or tmux copy mode after a failed restore.
                    return True
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
        if not attach.prepare_for_live_input():
            # The event was aimed at chrome; consuming it is safer than
            # forwarding the first character into an unresolved history view.
            return True
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
        if (key_name in ("e", "E")
                and int(event.state & Gdk.ModifierType.CONTROL_MASK)
                and int(event.state & Gdk.ModifierType.SHIFT_MASK)):
            self.open_process_monitor_window()
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
        state = KeyMap._clean_state(event.state)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.MOD1_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if shortcut_index is not None:
            if ctrl and alt:
                self.toggle_pause_tab(shortcut_index)
                return True
            if not shift and (ctrl or alt):
                self.jump_to_tab(shortcut_index)
                return True
        return self._route_unhandled_key_to_terminal(event)


def canonical_database_path(db_path: str | Path | None = None) -> Path:
    """Resolve the exact file a GUI instance will open and identify."""

    raw_path = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    if raw_path == Path(":memory:"):
        raise ValueError("the TermDeck GUI requires a file-backed session database")
    canonical = raw_path.resolve(strict=False)
    if canonical.exists() and not canonical.is_file():
        raise ValueError(f"TermDeck database path is not a regular file: {canonical}")
    return canonical


def _application_id_for_canonical_database(db_path: Path) -> str:
    default_path = DEFAULT_DB_PATH.expanduser().resolve(strict=False)
    if db_path == default_path:
        return "dev.t1000.T1000"
    digest = hashlib.sha256(os.fsencode(str(db_path))).hexdigest()[:32]
    return f"dev.t1000.T1000.db_{digest}"


def application_id_for_database(db_path: str | Path | None = None) -> str:
    """Return one stable GApplication identity per canonical registry file.

    GTK forwards a second launch to an existing application with the same ID.
    A global ID therefore makes ``termdeck gui --db other.sqlite3`` disappear
    into an already-running default-registry process.  File-backed registries
    get distinct stable IDs while the historical default keeps its original ID.
    """

    return _application_id_for_canonical_database(canonical_database_path(db_path))


def payload_revival_candidates(records) -> "list[SessionRecord]":
    """Order managed sessions whose durable payload may need relaunching.

    A canonical automatic recipe is the only restart authority. The backend
    distinguishes an alive pane (no-op), a dead pane (in-place respawn), a
    missing managed session (recreate), and optional stronger checkpoint/native
    resume data. External, manual, never, stopped, archived, and corrupt rows
    remain excluded.
    """

    rows = []
    for record in records:
        if bool(getattr(record, "external", False)):
            continue
        if str(getattr(record, "status", "")).casefold() not in {
            "running",
            "paused",
            "missing",
            "recovery-pending",
            "start-error",
        }:
            continue
        try:
            recipe = recipe_from_record(record)
        except SessionRecipeError:
            continue
        if automatic_recovery_allowed(recipe):
            rows.append(record)
    rows.sort(
        key=lambda record: (
            not bool(getattr(record, "visible", True)),
            str(getattr(record, "window_id", "")),
            int(getattr(record, "tab_index", 0)),
            str(getattr(record, "name", "")),
        )
    )
    return rows


def revive_dead_payload_sessions(backend, records, store=None) -> "tuple[int, int]":
    """Relaunch dead or missing payloads through the generic durable contract.

    ``create_or_resume`` is a no-op for a live payload, respawns a proven dead
    pane in place, recreates a missing managed session from its digest-bound
    recipe, and preserves optional native resume semantics. Returns
    ``(revived, failed)``; no command is guessed here.
    """

    revived = 0
    failed = 0
    for record in records:
        probe = (
            record
            if str(getattr(record, "status", "")).casefold() == "recovery-pending"
            else replace(record, status="recovery-pending")
        )
        try:
            before_pid = backend.pane_pid(record)
        except (OSError, TmuxError):
            before_pid = None
        try:
            backend.create_or_resume(probe)
        except Exception:
            # The backend refused an invalid/corrupt recipe, unavailable
            # optional native integration, or unsafe external replacement. Leave
            # the tab exactly as it is; never guess with send-keys.
            failed += 1
            continue
        try:
            after_pid = backend.pane_pid(record)
        except (OSError, TmuxError):
            after_pid = None
        if after_pid is None or after_pid == before_pid:
            # The payload was already alive; nothing was replaced.
            continue
        revived += 1
        if store is not None:
            try:
                store.update(
                    record.id,
                    status="running",
                    last_pid=after_pid,
                    last_opened_at=utc_now(),
                )
            except Exception:
                pass
    return revived, failed


class TermDeckApplication(Gtk.Application):
    def __init__(self, db_path: str | Path | None = None, workspace_path: str | Path | None = None):
        canonical_db_path = canonical_database_path(db_path)
        super().__init__(
            application_id=_application_id_for_canonical_database(canonical_db_path),
            flags=0,
        )
        quit_action = Gio.SimpleAction.new("quit-preserving-sessions", None)
        quit_action.connect("activate", self._activate_quit_preserving_sessions)
        self.add_action(quit_action)
        self.store = SessionStore(canonical_db_path)
        self.backend = TmuxBackend()
        self._tmux_server_isolation = None
        try:
            self._tmux_server_isolation = self.backend.ensure_server_lifecycle_isolated()
            if not self._tmux_server_isolation.ok:
                print(
                    f"T1000: {self._tmux_server_isolation.detail}",
                    file=sys.stderr,
                )
        except TmuxError as exc:
            print(
                f"T1000: tmux server lifecycle isolation failed: {exc}",
                file=sys.stderr,
            )
        # Optional account isolation is loaded only after the operator opts in
        # for a terminal. Generic create/open/reconcile/recover paths neither
        # provision CODEX_HOME nor require Codex/codex-auth to be installed.
        self.codex_auth: CodexAuthManager | None = None
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
        self._shutdown_persistence_started = False
        self._last_persistence_result: PersistenceResult | None = None
        self._legacy_recovery_checked = False
        self._last_legacy_recovery_result: LegacyRecoveryResult | None = None
        self._payload_revival_started = False
        # Shared snapshot memo: multiple periodic table refreshers (window
        # overview, tab manager) reuse one tmux/proc sampling pass per tick
        # instead of each running their own identical pass.
        self._task_snapshot_cache = None
        # Event-driven refresh: one persistent tmux control-mode client turns
        # session lifecycle changes into a coalesced refresh well before the
        # next polling tick; the periodic timers stay on as the reconcile net.
        self._tmux_events_client: ControlModeClient | None = None
        self._tmux_event_refresh_source: int | None = None

    def get_codex_auth_manager(self) -> CodexAuthManager:
        manager = self.codex_auth
        if manager is None:
            manager = CodexAuthManager(account_source=self.store.account_source)
            self.codex_auth = manager
        return manager

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
        # Control clients orphaned by a crashed or killed instance stay
        # attached to the shared tmux server, and every restart stacks another
        # generation on top of them; reap ours before this instance attaches
        # its own.  Only tmux -C attach-session processes whose parent is gone
        # (PID 1) and whose target carries the termdeck_ prefix are touched,
        # so live instances and user processes are never at risk.
        try:
            terminate_orphan_control_clients()
        except Exception:
            pass
        self._frame_budget_monitor = None
        if hud_enabled():
            # Opt-in (T1000_PERF_HUD=1) stall evidence: a lightweight GLib tick
            # probe plus a periodic one-line report keeps UI jank measurable in
            # the field before and after each optimisation, at zero cost when
            # the HUD is off.
            self._frame_budget_monitor = FrameBudgetMonitor()
            install_glib_probe(self._frame_budget_monitor)
            GLib.timeout_add_seconds(10, self._log_frame_budget_report)
        if os.environ.get("T1000_DISABLE_TMUX_EVENTS") != "1":
            self._start_tmux_event_watcher()

    def _log_frame_budget_report(self) -> bool:
        """Emit one-line stall summaries so field reports are copy-pasteable."""

        monitor = self._frame_budget_monitor
        if monitor is None:
            return False
        report = monitor.report()
        print(
            f"[frame-budget] ticks={report.ticks} stalls={report.stalls} "
            f"p95={report.p95_ms:.2f}ms worst={report.worst_ms:.2f}ms",
            file=sys.stderr,
        )
        return True

    # Notification kinds that change what the task tables and panes display.
    # High-frequency stream kinds (output, pane-offset, focus, resize) are
    # excluded on purpose; "unknown" is a forward-compatible catch-all and is
    # safe because refreshes are coalesced.
    _EVENT_REFRESH_KINDS = frozenset(
        {
            "client-detached",
            "client-session-changed",
            "exit",
            "layout-change",
            "pane-add",
            "pane-exited",
            "session-changed",
            "sessions-changed",
            "unknown",
            "unlinked-window-add",
            "unlinked-window-close",
            "unlinked-window-renamed",
            "window-add",
            "window-close",
            "window-pane-changed",
            "window-renamed",
        }
    )

    def _start_tmux_event_watcher(self) -> None:
        """Start push-based refresh from tmux control-mode notifications.

        The polling timers remain as the reconcile fallback; this only makes
        session create/close/rename visible immediately.  ``attach-session``
        without a target never creates sessions, so the watcher is free of
        side effects: while no tmux session exists the client retries with a
        capped backoff until one appears, then stays attached.
        """

        if self._tmux_events_client is not None or not self.backend.available:
            return
        executable = self.backend.executable

        def spawn_transport() -> "subprocess.Popen[bytes]":
            # The parent-death signal keeps this watcher from surviving as
            # a zombie client when the app dies without running its cleanup.
            return subprocess.Popen(
                [executable, "-C", "attach-session"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=bind_transport_to_parent_death,
            )

        self._tmux_events_client = ControlModeClient(
            spawn_transport=spawn_transport,
            on_event=self._on_tmux_control_event,
            reconnect_initial_seconds=1.0,
            reconnect_max_seconds=10.0,
        )
        self._tmux_events_client.start()

    def _on_tmux_control_event(self, event: ControlEvent) -> None:
        """Reader-thread callback: only schedule main-loop work, never touch GTK."""

        if event.kind not in self._EVENT_REFRESH_KINDS:
            return
        GLib.idle_add(self._queue_event_refresh)

    def _queue_event_refresh(self) -> bool:
        """Coalesce notification bursts (one spawn emits several) into one refresh."""

        if self._tmux_event_refresh_source is None:
            self._tmux_event_refresh_source = GLib.timeout_add(
                150, self._run_event_refresh
            )
        return False

    def _run_event_refresh(self) -> bool:
        self._tmux_event_refresh_source = None
        # The world changed outside this app: drop the snapshot memo so the
        # refresh below (and any imminent timer tick) samples fresh state.
        self._task_snapshot_cache = None
        for toplevel in self.get_windows():
            if isinstance(toplevel, (WindowOverview, MasterWindow)) and toplevel.get_visible():
                toplevel.refresh(force=True)
        for window in self.windows:
            side = getattr(window, "side", None)
            if side is not None:
                side.refresh()
        return False

    def persist_for_shutdown(self) -> PersistenceResult | None:
        """Persist exact recoverable state once before the application exits."""

        if self._shutdown_persistence_started:
            return self._last_persistence_result
        self._shutdown_persistence_started = True
        self._last_persistence_result = persist_live_sessions(self.store, self.backend)
        return self._last_persistence_result

    def shared_task_snapshot(self, *, force: bool = False):
        """Return (records, task snapshot items), memoized briefly.

        The window overview and the tab manager historically each ran their
        own tmux list/capture and /proc sampling pass on every refresh tick
        even though they request identical data.  One short-lived memo keeps
        a single sampling pass per tick without changing what any consumer
        renders: the memo key covers record identity and state, so any store
        change (open/close/rename/visibility) invalidates it immediately,
        and ``force=True`` (manual Refresh buttons) always rebuilds.
        """

        records = self.store.list()
        key = tuple(
            (
                record.id,
                getattr(record, "status", None),
                getattr(record, "last_pid", None),
                getattr(record, "name", None),
                getattr(record, "window_id", None),
                getattr(record, "visible", None),
            )
            for record in records
        )
        now = time.monotonic()
        ttl = min(2.0, max(0.5, float(self.settings.ui_refresh_interval_seconds) / 2.0))
        cached = self._task_snapshot_cache
        if (
            not force
            and cached is not None
            and cached[0] == key
            and (now - cached[1]) < ttl
        ):
            return records, cached[2]
        items = build_task_snapshot(
            records,
            backend=self.backend,
            sampler=self.sampler,
            policy=self.budget,
            snapshot_refresh_limit=UI_RESOURCE_REFRESH_BUDGET,
        )
        self._task_snapshot_cache = (key, now, items)
        return records, items

    def do_shutdown(self) -> None:
        client = self._tmux_events_client
        if client is not None:
            # Stop the reader before persistence so a wedged transport cannot
            # interleave callbacks with teardown; stop() joins with a bound.
            self._tmux_events_client = None
            client.stop()
        if self._tmux_event_refresh_source is not None:
            GLib.source_remove(self._tmux_event_refresh_source)
            self._tmux_event_refresh_source = None
        try:
            self.persist_for_shutdown()
        except Exception:
            # Persistence failures are reported by the explicit CLI/service path;
            # never trap the desktop application in a failed shutdown sequence.
            pass
        finally:
            Gtk.Application.do_shutdown(self)

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

    def window_for_window_id(self, window_id: str) -> TermDeckWindow | None:
        """Return the open GTK window that owns one logical window id."""

        return next(
            (window for window in self.windows if window.window_id == window_id),
            None,
        )

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
        if source is not None and session_id in (
            getattr(source, "_auth_switch_inflight", set())
            | getattr(source, "_payload_restart_inflight", set())
            | getattr(source, "_auth_attach_protected", set())
        ):
            source.show_toast(
                f"Wait for {record.name}'s Codex account switch before moving this tab",
                kind="warning",
            )
            return None

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
                selection_history_handler=lambda r=updated: destination._selection_history_for_terminal(r),
                attach_exit_handler=lambda sid=session_id: destination._attach_client_should_retry(sid),
                attach_stable_handler=lambda sid=session_id: destination._on_auth_attach_stable(sid),
                pane_dead_handler=lambda sid=session_id: destination._on_live_payload_dead(sid),
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
        terminated_ids: set[str] = set()
        for record in unique.values():
            blocking_window = next(
                (
                    window
                    for window in getattr(self, "windows", [])
                    if record.id in getattr(window, "_auth_switch_inflight", set())
                ),
                None,
            )
            if blocking_window is not None:
                blocker = getattr(blocking_window, "_auth_switch_blocks_action", None)
                if callable(blocker):
                    blocker(record.id, action="stopping this tab")
                else:
                    show_toast = getattr(blocking_window, "show_toast", None)
                    if callable(show_toast):
                        show_toast(
                            f"Wait for {record.name}'s Codex account switch before stopping this tab",
                            kind="warning",
                        )
                continue
            self.backend.terminate(record)
            clear_captain = getattr(self.store, "clear_window_captain", None)
            if callable(clear_captain):
                clear_captain(record.window_id, session_id=record.id)
            self.store.update(
                record.id,
                status="stopped",
                visible=False,
                last_pid=None,
                recovery_command=None,
                recovery_cwd=None,
            )
            terminated_ids.add(record.id)
        if terminated_ids:
            self.close_session_tabs(terminated_ids)
        return len(terminated_ids)

    def confirm_kill_window(
        self,
        parent: TermDeckWindow,
        records: list[SessionRecord],
    ) -> bool:
        """Always confirm the irreversible window-level lifecycle action."""

        managed_count = sum(not bool(record.external) for record in records)
        external_count = len(records) - managed_count
        tab_word = "tab" if len(records) == 1 else "tabs"
        managed_word = "session" if managed_count == 1 else "sessions"
        external_word = "reference" if external_count == 1 else "references"
        details = [
            f"This permanently removes the saved window and its {len(records)} {tab_word}, "
            "so it cannot return after restart."
        ]
        if managed_count:
            details.append(
                f"{managed_count} T1000-managed {managed_word} will be stopped."
            )
        if external_count:
            details.append(
                f"{external_count} external tmux tab {external_word} will be detached and "
                "forgotten without stopping the external tmux session."
            )
        details.append(
            "Closing the window instead keeps it for the next launch. This cannot be undone."
        )
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Kill “{parent.window_title}” permanently?",
        )
        dialog.format_secondary_text("\n\n".join(details))
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def kill_window(self, window: TermDeckWindow) -> int:
        """Stop, delete, and forget one logical window after explicit consent.

        Managed rows go through :meth:`terminate_records`, which owns the auth
        transition guard. External rows are registry references only: detach
        their clients, but never signal their independently-owned tmux sessions.
        Any guarded or failed row keeps the logical window remembered so startup
        can recover it and the user can retry safely.
        """

        records = list(
            self.store.list(
                include_archived=True,
                window_id=window.window_id,
            )
        )
        if not self.confirm_kill_window(window, records):
            return 0

        managed = [record for record in records if not bool(record.external)]
        external = [record for record in records if bool(record.external)]
        termination_error: Exception | None = None
        terminated = 0
        if managed:
            try:
                terminated = self.terminate_records(managed)
            except Exception as exc:
                # Some earlier rows may already be stopped, but the central
                # batch does not expose their IDs. Preserve every managed row
                # in this failure case so a retry cannot lose provenance.
                termination_error = exc

        protected_ids = {
            session_id
            for owner in getattr(self, "windows", [])
            for session_id in getattr(owner, "_auth_switch_inflight", set())
        }
        expected_managed = [
            record for record in managed if record.id not in protected_ids
        ]
        removable_managed_ids: set[str] = set()
        if termination_error is None and terminated == len(expected_managed):
            for record in expected_managed:
                refreshed = self.store.get(record.id)
                if (
                    refreshed is not None
                    and refreshed.status.casefold() == "stopped"
                    and not refreshed.visible
                    and refreshed.last_pid is None
                    and refreshed.recovery_command is None
                    and refreshed.recovery_cwd is None
                ):
                    removable_managed_ids.add(record.id)

        removable_external_ids = {
            record.id for record in external if record.id not in protected_ids
        }
        cleanup_errors: list[str] = []
        if removable_external_ids:
            try:
                self.close_session_tabs(removable_external_ids)
            except Exception as exc:
                cleanup_errors.append(str(exc))
                removable_external_ids.clear()

        removable_ids = removable_managed_ids | removable_external_ids
        deleted_ids: set[str] = set()
        for record in records:
            if record.id not in removable_ids:
                continue
            try:
                self.store.delete(record.id)
                deleted = self.store.get(record.id) is None
            except Exception as exc:
                cleanup_errors.append(str(exc))
                continue
            if deleted:
                deleted_ids.add(record.id)

        try:
            remaining = self.store.list(
                include_archived=True,
                window_id=window.window_id,
            )
        except Exception as exc:
            cleanup_errors.append(str(exc))
            remaining = records

        forgotten = False
        if not remaining:
            forget_window = getattr(self.store, "forget_window", None)
            if callable(forget_window):
                try:
                    forgotten = bool(forget_window(window.window_id))
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if not forgotten:
                cleanup_errors.append("saved window could not be forgotten")

        title = window.window_title
        deleted_external = len(deleted_ids & {record.id for record in external})
        other_window = next(
            (
                candidate
                for candidate in getattr(self, "windows", [])
                if candidate is not window
            ),
            None,
        )
        toast_target = other_window or window
        show_toast = getattr(toast_target, "show_toast", None)

        if forgotten:
            managed_word = "session" if terminated == 1 else "sessions"
            tab_word = "tab" if len(deleted_ids) == 1 else "tabs"
            details = [f"stopped {terminated} managed {managed_word}"]
            if deleted_external:
                reference_word = "reference" if deleted_external == 1 else "references"
                details.append(
                    f"forgot {deleted_external} external {reference_word} without stopping tmux"
                )
            details.append(f"removed {len(deleted_ids)} saved {tab_word}")
            if callable(show_toast):
                show_toast(
                    f"Killed “{title}” permanently · " + " · ".join(details),
                    kind="danger",
                )
            try:
                window.destroy()
            except Exception as exc:
                cleanup_errors.append(str(exc))
            return len(deleted_ids)

        remaining_count = len(remaining)
        blocked_count = len(
            protected_ids & {record.id for record in remaining}
        )
        tab_word = "tab" if len(records) == 1 else "tabs"
        remaining_word = "tab remains" if remaining_count == 1 else "tabs remain"
        managed_word = "session" if terminated == 1 else "sessions"
        detail = (
            f"Window kill incomplete · stopped {terminated} managed {managed_word} · "
            f"removed {len(deleted_ids)} of {len(records)} {tab_word} · "
            f"{remaining_count} {remaining_word}"
        )
        if blocked_count:
            switch_word = "switch is" if blocked_count == 1 else "switches are"
            detail += f" · {blocked_count} Codex account {switch_word} still in progress"
        if callable(show_toast):
            show_toast(
                detail,
                kind="danger" if termination_error is not None or cleanup_errors else "warning",
            )
        return len(deleted_ids)

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
            title="Create your first T1000 window" if first_run else "New T1000 window",
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
        hidden_records: list[SessionRecord] = []
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
            if tab.visible:
                # Let the VTE page settle before creating tmux. The callback in
                # ``open_session`` passes that exact grid to ``new-session -x/-y``.
                win.open_session(
                    record,
                    restart_missing=True,
                    remember_layout=False,
                    known_live=False,
                    known_pid=None,
                    known_pid_resolved=True,
                    refresh_side=False,
                )
            else:
                hidden_records.append(record)
        if hidden_records:
            win.start_hidden_recipe_records(hidden_records)
        win.side.refresh()
        return win

    def open_workspace_recipe(self, path: str | Path) -> int:
        recipe = load_recipe(path)
        opened = 0
        for index, recipe_window in enumerate(recipe.windows, start=1):
            self._open_recipe_window(recipe_window, recipe.name, index)
            opened += 1
        return opened

    def _activate_quit_preserving_sessions(self, _action, _parameter) -> None:
        """Expose a D-Bus GApplication action for GUI-only release handoff."""

        self.quit_preserving_sessions()

    def quit_preserving_sessions(self) -> bool:
        """Close every TermDeck window while leaving tmux sessions alive."""

        busy_windows = [
            window
            for window in self.windows
            if getattr(window, "_auth_switch_inflight", set())
        ]
        if busy_windows:
            for window in busy_windows:
                window.show_toast(
                    "T1000 will stay open until the Codex account switch finishes",
                    kind="warning",
                )
            return False

        requires_isolation = process_exit_requires_tmux_isolation()
        try:
            isolation = self.backend.ensure_server_lifecycle_isolated()
        except TmuxError as exc:
            if requires_isolation:
                detail = f"T1000 cannot close safely: {exc}"
                print(detail, file=sys.stderr)
                for window in self.windows:
                    window.show_toast(detail, kind="error")
                return False
            isolation = None
        if isolation is not None and isolation.ok:
            self._tmux_server_isolation = isolation
        elif requires_isolation:
            detail = (
                "T1000 cannot close safely: "
                + (
                    isolation.detail
                    if isolation is not None
                    else "tmux server isolation could not be verified"
                )
            )
            print(detail, file=sys.stderr)
            for window in self.windows:
                window.show_toast(detail, kind="error")
            return False

        for window in list(self.windows):
            for tab in list(window.tabs.values()):
                tab.widget.detach()
            try:
                window.destroy()
            except Exception:
                pass
        self.windows.clear()
        self.quit()
        return True

    def do_activate(self) -> None:
        if not self.backend.available:
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.CLOSE,
                text="tmux is required",
            )
            dialog.format_secondary_text("Install tmux, then run T1000 again. On Debian/Ubuntu: sudo apt install tmux")
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
            recover_missing=True,
            resource_sample_limit=UI_RESOURCE_REFRESH_BUDGET,
        )

        # Older TermDeck versions sometimes persisted only ``codex --yolo``
        # instead of the primary UUID.  The archive pass can inspect tens of
        # gigabytes of rollout history, so never make notebook construction
        # wait for it.  Rows are repaired in the background and the side pane
        # is refreshed after the durable result is published.
        schedule_legacy_recovery = getattr(self, "_schedule_legacy_recovery", None)
        if callable(schedule_legacy_recovery):
            schedule_legacy_recovery()

        # Reconcile cannot detect a crashed Codex/Claude payload: the tmux
        # session and its trailing fallback shell survive, so the row stays
        # ``running`` while its tab replays a dead conversation.  Give the
        # notebooks a moment to attach, then relaunch those payloads in place
        # through the backend's fail-closed placeholder recovery.
        payload_revival_timeout = getattr(self, "_payload_revival_timeout", None)
        if callable(payload_revival_timeout):
            GLib.timeout_add_seconds(2, payload_revival_timeout)

        # Drop destroyed or unrealized windows so a single-instance re-activate
        # does not skip restoration while holding stale ApplicationWindow
        # references.  An unrealized window can still own VTE tmux clients; it
        # must be detached before its records are rebuilt or reactivation will
        # create a second client for every tab (and duplicate terminal input).
        live_windows = []
        stale_windows = []
        for window in self.windows:
            try:
                if window.get_realized():
                    live_windows.append(window)
                    continue
            except Exception:
                pass
            stale_windows.append(window)
        for window in stale_windows:
            for tab in list(getattr(window, "tabs", {}).values()):
                try:
                    tab.widget.detach()
                except Exception:
                    pass
            try:
                window.destroy()
            except Exception:
                pass
        self.windows = live_windows

        if not self.windows and self.workspace_path is not None:
            try:
                self.open_workspace_recipe(self.workspace_path)
            except WorkspaceRecipeError as exc:
                self._show_error("Could not open saved window layout", str(exc))
            if self.windows:
                self._present_all_windows()
                return

        restore_groups = getattr(self.store, "windows_for_restore", None)
        groups = (
            restore_groups()
            if callable(restore_groups)
            else self.store.windows_with_visible_sessions()
        )
        windows_by_id = {
            getattr(window, "window_id", None): window
            for window in self.windows
            if getattr(window, "window_id", None)
        }
        restore_errors: list[tuple[str, str, str]] = []
        for window_id, window_title, records in groups:
            existing = windows_by_id.get(window_id)
            if existing is not None:
                open_tabs = getattr(existing, "tabs", {})
                missing = [record for record in records if record.id not in open_tabs]
                restore = getattr(existing, "restore_previous_tabs", None)
                if missing and callable(restore):
                    try:
                        restore(records=missing)
                    except Exception as exc:
                        restore_errors.append((window_id, window_title, str(exc)))
                continue
            get_application_windows = getattr(self, "get_windows", None)
            windows_before = {
                id(window)
                for window in (
                    get_application_windows()
                    if callable(get_application_windows)
                    else ()
                )
            }
            try:
                win = TermDeckWindow(
                    self,
                    window_id=window_id,
                    window_title=window_title,
                    restore_records=records,
                )
            except Exception as exc:
                # One malformed window or unavailable terminal must not prevent
                # every later remembered window from being reconstructed.
                # Gtk.ApplicationWindow registers with the application early in
                # construction, so explicitly destroy any new partial window
                # before a later activation retries the same catalog ID.
                if callable(get_application_windows):
                    for partial in get_application_windows():
                        if id(partial) in windows_before or partial in self.windows:
                            continue
                        for tab in list(getattr(partial, "tabs", {}).values()):
                            try:
                                tab.widget.detach()
                            except Exception:
                                pass
                        try:
                            partial.destroy()
                        except Exception:
                            pass
                restore_errors.append((window_id, window_title, str(exc)))
                continue
            self.windows.append(win)
            windows_by_id[window_id] = win

        self._last_window_restore_errors = tuple(restore_errors)
        if not self.windows:
            if groups:
                show_error = getattr(self, "_show_error", None)
                if callable(show_error):
                    detail = "\n".join(
                        f"{title} ({window_id}): {error}"
                        for window_id, title, error in restore_errors
                    )
                    show_error("Could not restore saved T1000 windows", detail)
                return
            self.prompt_for_new_window(first_run=True)
            return
        self._present_all_windows()
        if restore_errors:
            show_toast = getattr(self.windows[0], "show_toast", None)
            if callable(show_toast):
                show_toast(
                    f"Could not restore {len(restore_errors)} saved window(s); activate T1000 to retry",
                    kind="danger",
                )

    def _present_all_windows(self) -> None:
        """Raise every restored named window, not only the last one created."""

        for window in self.windows:
            try:
                window.present()
            except Exception:
                continue
            recover_position = getattr(window, "_on_map_recover_offscreen", None)
            if not callable(recover_position):
                continue
            # Some WMs apply their saved placement after ``present``.  A
            # second pass after that placement prevents a disconnected
            # monitor from leaving the restored window outside the screen.
            for delay_ms in (250, 1500):
                try:
                    GLib.timeout_add(
                        delay_ms,
                        lambda recover=recover_position: (
                            recover() or False
                        ),
                    )
                except Exception:
                    pass

    def _schedule_legacy_recovery(self) -> None:
        """Run the archive scan away from GTK's startup/main thread.

        ``repair_legacy_recovery`` walks the shared Codex rollout archive.  On
        a large home that can take minutes, and doing it synchronously in
        ``do_activate`` leaves a window with its side pane constructed but no
        notebook pages attached.  Use a separate store connection because the
        GUI connection is thread-affine, then publish only the small result
        and refresh already-created windows on the GTK context.
        """

        if getattr(self, "_legacy_recovery_checked", False):
            return
        self._legacy_recovery_checked = True
        try:
            store_path = self.store.path
            auth_root = self.store.auth_root
            account_source = self.store.account_source
            provision_auth_scopes = self.store.provision_auth_scopes
        except Exception:
            self._last_legacy_recovery_result = None
            return

        def worker() -> None:
            worker_store = None
            result = None
            try:
                # Archive inspection is deliberately low priority: terminal
                # attachment should win over historical metadata repair.
                try:
                    os.nice(10)
                except (AttributeError, OSError):
                    pass
                worker_store = SessionStore(
                    store_path,
                    auth_root=auth_root,
                    account_source=account_source,
                    provision_auth_scopes=provision_auth_scopes,
                )
                result = repair_legacy_recovery(worker_store)
            except Exception:
                result = None
            finally:
                if worker_store is not None:
                    try:
                        worker_store.close()
                    except Exception:
                        pass

            def publish() -> bool:
                self._last_legacy_recovery_result = result
                for window in list(self.windows):
                    try:
                        window.side.refresh()
                    except Exception:
                        pass
                return False

            try:
                GLib.idle_add(publish)
            except Exception:
                # The app may be shutting down; the durable store mutation is
                # already complete and no UI callback is required then.
                pass

        threading.Thread(
            target=worker,
            name="termdeck-legacy-recovery",
            daemon=True,
        ).start()

    def _payload_revival_timeout(self) -> bool:
        """One-shot GLib callback: start the payload revival sweep once."""

        self._schedule_payload_revival()
        return False

    def _schedule_payload_revival(self) -> None:
        """Relaunch dead or missing managed payloads from durable recipes.

        Reconcile cannot see a payload crash: the session and its fallback
        shell are alive, so the registry says ``running`` while the notebook
        still exists after its payload exited. Probe every qualifying row
        through the backend's generic recovery contract on one
        low-priority worker thread — serial on purpose, so a machine-wide
        restart cannot fan out dozens of heavyweight resume processes at
        once — and publish only a small summary back on the GTK context.
        """

        if getattr(self, "_payload_revival_started", False):
            return
        self._payload_revival_started = True
        try:
            records = self.store.list()
        except Exception:
            return
        candidates = payload_revival_candidates(records)
        if not candidates:
            return
        try:
            store_path = self.store.path
            auth_root = self.store.auth_root
            account_source = self.store.account_source
            provision_auth_scopes = self.store.provision_auth_scopes
        except Exception:
            store_path = None
            auth_root = None
            account_source = None
            provision_auth_scopes = False

        def worker() -> None:
            try:
                os.nice(10)
            except (AttributeError, OSError):
                pass
            worker_store = None
            if store_path is not None:
                # The GUI store connection is thread-affine; use a private one.
                try:
                    worker_store = SessionStore(
                        store_path,
                        auth_root=auth_root,
                        account_source=account_source,
                        provision_auth_scopes=provision_auth_scopes,
                    )
                except Exception:
                    worker_store = None
            try:
                revived, failed = revive_dead_payload_sessions(
                    self.backend,
                    candidates,
                    store=worker_store,
                )
            finally:
                if worker_store is not None:
                    try:
                        worker_store.close()
                    except Exception:
                        pass
            if not revived and not failed:
                return

            def publish() -> bool:
                for window in list(self.windows):
                    try:
                        window.side.refresh()
                    except Exception:
                        pass
                show_toast = (
                    getattr(self.windows[0], "show_toast", None)
                    if self.windows
                    else None
                )
                if callable(show_toast):
                    if revived:
                        message = (
                            f"Relaunched {revived} coding session"
                            f"{'s' if revived != 1 else ''} in place"
                        )
                        if failed:
                            message += f"; {failed} could not be replaced safely"
                        show_toast(message, kind="warning" if failed else "success")
                    else:
                        show_toast(
                            f"{failed} remembered session"
                            f"{'s' if failed != 1 else ''} could not be relaunched safely",
                            kind="warning",
                        )
                return False

            try:
                GLib.idle_add(publish)
            except Exception:
                # The app may be shutting down; respawned payloads keep
                # running inside tmux and need no UI callback.
                pass

        threading.Thread(
            target=worker,
            name="termdeck-payload-revival",
            daemon=True,
        ).start()



# ---------------------------------------------------------------------------
# Process Monitor — floating dashboard for aborted / errored tabs
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = re.compile(
    r"(?i)(traceback|error:|exception:|killed|oom|segfault|abort|panic|"
    r"failed|fatal|command not found|permission denied|no such file|killed by signal)"
)


def _classify_tail(tail: str) -> str:
    """Return a short label describing what the tail output looks like."""
    if not tail.strip():
        return "(no output)"
    m = _ERROR_PATTERNS.search(tail)
    if m:
        return m.group(0)[:40]
    return "(no error keywords)"


class ProcessMonitorWindow(Gtk.Window):
    """Auto-refreshing dashboard that surfaces tabs whose process died or
    produced error output.  Opens from More-actions menu or Ctrl+Shift+E.

    A tab is considered *flagged* when:
      - its stored status is 'stopped' or 'missing', OR
      - its PID is gone while status is still 'running', OR
      - the last 60 lines of its pane output match _ERROR_PATTERNS.
    """

    _POLL_SECONDS = 5
    _CAPTURE_LINES = 60

    def __init__(self, app_context) -> None:
        super().__init__(title="Process Monitor")
        self.app_context = app_context
        self.set_default_size(1100, 600)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)

        # --- toolbar ---
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        self._status_label = Gtk.Label(label="Scanning…")
        self._status_label.set_halign(Gtk.Align.START)
        bar.pack_start(self._status_label, True, True, 0)

        refresh_btn = Gtk.Button(label="↺ Refresh")
        refresh_btn.connect("clicked", lambda *_: self._scan())
        bar.pack_end(refresh_btn, False, False, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda *_: self.destroy())
        bar.pack_end(close_btn, False, False, 0)

        # --- scrolled list ---
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._store = Gtk.ListStore(
            str,   # 0 id
            str,   # 1 name
            str,   # 2 window
            str,   # 3 status
            str,   # 4 health
            str,   # 5 flag reason
            str,   # 6 tail preview (last non-empty line)
        )
        self._tree = Gtk.TreeView(model=self._store)
        self._tree.set_rules_hint(True)
        self._tree.set_activate_on_single_click(False)

        cols = [
            ("Name",    1, 160),
            ("Window",  2, 120),
            ("Status",  3,  80),
            ("Health",  4,  80),
            ("Reason",  5, 180),
            ("Last output", 6, 380),
        ]
        for title, col_idx, width in cols:
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
            column = Gtk.TreeViewColumn(title, renderer, text=col_idx)
            column.set_min_width(width)
            column.set_resizable(True)
            column.set_sort_column_id(col_idx)
            self._tree.append_column(column)

        scroll.add(self._tree)

        # --- tail detail panel ---
        self._detail_buf = Gtk.TextBuffer()
        detail_view = Gtk.TextView(buffer=self._detail_buf)
        detail_view.set_editable(False)
        detail_view.set_monospace(True)
        detail_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        detail_scroll = Gtk.ScrolledWindow()
        detail_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        detail_scroll.set_min_content_height(160)
        detail_scroll.add(detail_view)

        self._tree.connect("cursor-changed", self._on_row_selected)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.pack1(scroll, resize=True, shrink=False)
        paned.pack2(detail_scroll, resize=True, shrink=False)
        paned.set_position(340)

        hint = Gtk.Label(
            label="Shows tabs that have stopped unexpectedly or produced error output. "
                  "Select a row to see the full tail. Auto-refreshes every 5 s."
        )
        hint.set_halign(Gtk.Align.START)
        hint.set_margin_start(8)
        hint.set_margin_bottom(4)
        hint.set_line_wrap(True)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.pack_start(bar, False, False, 0)
        vbox.pack_start(hint, False, False, 0)
        vbox.pack_start(paned, True, True, 0)
        self.add(vbox)

        # per-row raw tail cache for the detail panel
        self._tail_cache: dict[str, str] = {}

        self._timer_id: int | None = None
        self.connect("destroy", self._on_destroy)
        self._scan()
        self._start_timer()

    # ------------------------------------------------------------------
    def _start_timer(self) -> None:
        self._timer_id = GLib.timeout_add_seconds(self._POLL_SECONDS, self._timer_tick)

    def _timer_tick(self) -> bool:
        if not self.get_realized() or not self.get_visible():
            return True
        self._scan()
        return True

    def _on_destroy(self, *_) -> None:
        if self._timer_id is not None:
            try:
                GLib.source_remove(self._timer_id)
            except Exception:
                pass
            self._timer_id = None

    # ------------------------------------------------------------------
    def _scan(self) -> None:
        backend = self.app_context.backend
        sampler = self.app_context.sampler
        records = self.app_context.store.list()

        flagged: list[tuple] = []
        for record in records:
            flag_reason = self._check_record(record, backend, sampler)
            if flag_reason is None:
                continue
            tail_raw = backend.capture(record, lines=self._CAPTURE_LINES)
            last_line = ""
            for ln in reversed(tail_raw.splitlines()):
                if ln.strip():
                    last_line = ln.strip()[:120]
                    break
            self._tail_cache[record.id] = tail_raw
            from .health import health_badge, evaluate_health
            from .models import ResourceSnapshot
            pid = record.last_pid
            snap = sampler.snapshot(pid) if pid else ResourceSnapshot(pid=pid, exists=False)
            health = health_badge(evaluate_health(record, snap, self.app_context.budget))
            flagged.append((
                record.id,
                record.name,
                record.window_title,
                record.status,
                health,
                flag_reason,
                last_line,
            ))

        # preserve selection
        sel_id: str | None = None
        sel_model, sel_iter = self._tree.get_selection().get_selected()
        if sel_iter:
            sel_id = self._store.get_value(sel_iter, 0)

        self._store.clear()
        new_sel_iter = None
        for row in flagged:
            it = self._store.append(row)
            if row[0] == sel_id:
                new_sel_iter = it

        if new_sel_iter:
            self._tree.get_selection().select_iter(new_sel_iter)

        ts = GLib.DateTime.new_now_local().format("%H:%M:%S")
        self._status_label.set_text(
            f"{len(flagged)} flagged tab(s) — last scan {ts}"
        )

    def _check_record(self, record, backend, sampler) -> str | None:
        """Return a short reason string if the tab should be flagged, else None."""
        status = record.status
        if status in ("stopped", "missing"):
            return f"status={status}"
        # PID gone while still marked running
        if status == "running" and record.last_pid:
            pid = backend.pane_pid(record)
            if pid is None:
                return "PID gone (unexpected exit)"
        # Scan output for error patterns
        tail = backend.capture(record, lines=self._CAPTURE_LINES)
        if tail and _ERROR_PATTERNS.search(tail):
            return "error output: " + _classify_tail(tail)
        return None

    def _on_row_selected(self, tree_view) -> None:
        model, it = tree_view.get_selection().get_selected()
        if it is None:
            return
        session_id = model.get_value(it, 0)
        tail = self._tail_cache.get(session_id, "")
        self._detail_buf.set_text(tail)


def run(db_path: str | Path | None = None, workspace_path: str | Path | None = None) -> int:
    app = TermDeckApplication(db_path=db_path, workspace_path=workspace_path)
    # The argparse CLI has already consumed TermDeck-specific flags. Passing
    # them to Gtk.Application a second time makes GTK reject options such as
    # --db and --workspace, so expose only the executable name here.
    return app.run([sys.argv[0]])
