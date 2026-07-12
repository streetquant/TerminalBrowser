import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from termdeck.gtk_app import MasterWindow, TermDeckApplication, TermDeckWindow
from termdeck.session_store import SessionStore


class MenuItemStub:
    def __init__(self, *, label: str = "") -> None:
        self.label = label
        self.handler = None
        self.submenu = None
        self.sensitive = True

    def connect(self, signal: str, handler) -> None:
        if signal != "activate":
            raise AssertionError(signal)
        self.handler = handler

    def activate(self) -> None:
        if self.handler is not None:
            self.handler(self)

    def set_submenu(self, submenu) -> None:
        self.submenu = submenu

    def set_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive


class MenuStub:
    created = []

    def __init__(self) -> None:
        self.items = []
        self.pointer_event = None
        self.showed = False
        MenuStub.created.append(self)

    def append(self, item) -> None:
        self.items.append(item)

    def show_all(self) -> None:
        self.showed = True

    def popup_at_pointer(self, event) -> None:
        self.pointer_event = event

    def popup(self, *_args) -> None:
        raise AssertionError("unexpected legacy popup")


class SelectionStub:
    def __init__(self) -> None:
        self.selected = []
        self.unselect_count = 0

    def unselect_all(self) -> None:
        self.unselect_count += 1
        self.selected.clear()

    def select_path(self, path) -> None:
        self.selected.append(path)


class TreeStub:
    def __init__(self, target, model=None) -> None:
        self.target = target
        self.model = model
        self.selection = SelectionStub()
        self.lookup = None
        self.cursor = None

    def get_path_at_pos(self, x: int, y: int):
        self.lookup = (x, y)
        return self.target

    def get_selection(self):
        return self.selection

    def get_model(self):
        return self.model

    def set_cursor(self, path, column, start_editing: bool) -> None:
        self.cursor = (path, column, start_editing)


class NotebookStub:
    def __init__(self, pages=None) -> None:
        self.pages = list(pages or [])
        self.current = -1
        self.reorderable = []

    def get_n_pages(self) -> int:
        return len(self.pages)

    def get_nth_page(self, index: int):
        return self.pages[index]

    def page_num(self, widget) -> int:
        try:
            return self.pages.index(widget)
        except ValueError:
            return -1

    def remove_page(self, index: int) -> None:
        self.pages.pop(index)

    def insert_page(self, widget, _label, position: int) -> int:
        self.pages.insert(position, widget)
        return position

    def set_tab_reorderable(self, widget, reorderable: bool) -> None:
        self.reorderable.append((widget, reorderable))

    def set_current_page(self, page: int) -> None:
        self.current = page


class WidgetStub:
    def __init__(self) -> None:
        self.detach_count = 0
        self.show_count = 0
        self.prepare_count = 0
        self.rebound_handlers = None
        self.applied_theme = None
        self.terminal = SimpleNamespace(grab_focus=lambda: None)

    def detach(self) -> None:
        self.detach_count += 1

    def show_all(self) -> None:
        self.show_count += 1

    def prepare_for_live_input(self) -> None:
        self.prepare_count += 1

    def rebind_window_handlers(self, **handlers) -> None:
        self.rebound_handlers = handlers

    def apply_window_theme(self, theme, *, font_override: str) -> None:
        self.applied_theme = (theme, font_override)


class TabManagerMoveTest(TestCase):
    def setUp(self) -> None:
        MenuStub.created = []

    def test_focus_selected_presents_existing_owner_without_relocating(self) -> None:
        record = SimpleNamespace(id="tab-1", window_id="source")
        calls = []
        source = SimpleNamespace(
            window_id="source",
            open_session=lambda candidate, **kwargs: calls.append(
                ("open", candidate.id, kwargs)
            ),
            present=lambda: calls.append(("present",)),
        )
        destination = SimpleNamespace(window_id="destination")
        master = SimpleNamespace(
            selected_records=lambda: [record],
            app_context=SimpleNamespace(
                windows=[source, destination],
                window_for_tab=lambda session_id: source if session_id == record.id else None,
            ),
        )

        MasterWindow.focus_selected(master)

        self.assertEqual(
            calls,
            [
                (
                    "open",
                    record.id,
                    {"restart_missing": False, "remember_layout": False},
                ),
                ("present",),
            ],
        )

    def test_focus_selected_falls_back_to_remembered_window(self) -> None:
        record = SimpleNamespace(id="tab-1", window_id="source")
        calls = []
        source = SimpleNamespace(
            window_id="source",
            open_session=lambda candidate, **kwargs: calls.append(candidate.id),
            present=lambda: calls.append("present"),
        )
        master = SimpleNamespace(
            selected_records=lambda: [record],
            app_context=SimpleNamespace(
                windows=[SimpleNamespace(window_id="other"), source],
                window_for_tab=lambda _session_id: None,
            ),
        )

        MasterWindow.focus_selected(master)

        self.assertEqual(calls, [record.id, "present"])

    def test_right_click_selects_the_exact_row_before_opening_menu(self) -> None:
        record = SimpleNamespace(id="tab-2")
        path = SimpleNamespace(name="row-2")
        column = SimpleNamespace(name="Name")
        store = SimpleNamespace(
            get_iter=lambda candidate: candidate,
            get_value=lambda _iter, index: "tab-2" if index == 0 else "",
        )
        tree = TreeStub((path, column, 0, 0), model=store)
        shown = []
        master = SimpleNamespace(
            tree=tree,
            store=store,
            app_context=SimpleNamespace(store=SimpleNamespace(get=lambda session_id: record if session_id == "tab-2" else None)),
            _show_row_context_menu=lambda candidate, event: shown.append((candidate, event)),
        )
        event = SimpleNamespace(button=3, x=31.8, y=62.4)

        handled = MasterWindow._on_tree_button_press(master, tree, event)

        self.assertTrue(handled)
        self.assertEqual(tree.lookup, (31, 62))
        self.assertEqual(tree.selection.unselect_count, 1)
        self.assertEqual(tree.selection.selected, [path])
        self.assertEqual(tree.cursor, (path, column, False))
        self.assertEqual(shown, [(record, event)])

    def test_row_context_menu_lists_other_windows_and_moves_to_selected_target(self) -> None:
        record = SimpleNamespace(id="tab-1", window_id="source")
        source = SimpleNamespace(window_id="source", window_title="Source")
        beta = SimpleNamespace(window_id="beta", window_title="Beta")
        alpha = SimpleNamespace(window_id="alpha", window_title="Alpha")
        moves = []
        master = SimpleNamespace(
            app_context=SimpleNamespace(available_move_windows=lambda _record: [alpha, beta]),
            _move_record_to_window=lambda candidate, target: moves.append((candidate.id, target.window_id)),
        )
        event = SimpleNamespace(button=3, time=44)

        with patch("termdeck.gtk_app.Gtk.Menu", MenuStub), patch(
            "termdeck.gtk_app.Gtk.MenuItem", MenuItemStub
        ):
            MasterWindow._show_row_context_menu(master, record, event)

        menu = MenuStub.created[0]
        move_item = menu.items[0]
        self.assertEqual(move_item.label, "Move to window")
        self.assertEqual(
            [item.label for item in move_item.submenu.items],
            ["Alpha", "Beta", "New window…"],
        )
        self.assertTrue(menu.showed)
        self.assertIs(menu.pointer_event, event)

        move_item.submenu.items[1].activate()
        self.assertEqual(moves, [("tab-1", "beta")])

    def test_row_context_menu_disables_move_when_no_other_window_is_open(self) -> None:
        record = SimpleNamespace(id="tab-1", window_id="only")
        master = SimpleNamespace(
            app_context=SimpleNamespace(available_move_windows=lambda _record: []),
        )

        with patch("termdeck.gtk_app.Gtk.Menu", MenuStub), patch(
            "termdeck.gtk_app.Gtk.MenuItem", MenuItemStub
        ):
            MasterWindow._show_row_context_menu(master, record, SimpleNamespace(button=3, time=1))

        unavailable = MenuStub.created[0].items[0].submenu.items[0]
        self.assertEqual(unavailable.label, "No other open windows")
        self.assertFalse(unavailable.sensitive)
        self.assertEqual(
            MenuStub.created[0].items[0].submenu.items[1].label,
            "New window…",
        )

    def test_new_window_move_prompts_without_opening_an_extra_shell(self) -> None:
        record = SimpleNamespace(id="tab-1")
        destination = SimpleNamespace(window_id="new-window")
        calls = []
        master = SimpleNamespace(
            app_context=SimpleNamespace(
                prompt_for_new_window=lambda **kwargs: calls.append(("prompt", kwargs))
                or destination,
            ),
            _move_record_to_window=lambda candidate, target: calls.append(
                ("move", candidate.id, target.window_id)
            ),
        )

        MasterWindow._move_record_to_new_window(master, record)

        self.assertEqual(
            calls,
            [
                ("prompt", {"parent": master, "open_initial_tab": False}),
                ("move", "tab-1", "new-window"),
            ],
        )

    def test_cancelled_new_window_move_keeps_the_tab_in_place(self) -> None:
        record = SimpleNamespace(id="tab-1")
        moves = []
        master = SimpleNamespace(
            app_context=SimpleNamespace(
                prompt_for_new_window=lambda **_kwargs: None,
            ),
            _move_record_to_window=lambda candidate, target: moves.append(
                (candidate, target)
            ),
        )

        MasterWindow._move_record_to_new_window(master, record)

        self.assertEqual(moves, [])

    def test_live_tab_move_reuses_widget_and_compacts_both_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                moved = store.create(
                    "agent",
                    "work",
                    "bash",
                    tmp,
                    window_id="source",
                    window_title="Source",
                    tab_index=0,
                )
                remaining = store.create(
                    "shell",
                    "work",
                    "bash",
                    tmp,
                    window_id="source",
                    window_title="Source",
                    tab_index=1,
                )
                existing = store.create(
                    "tests",
                    "work",
                    "bash",
                    tmp,
                    window_id="destination",
                    window_title="Destination",
                    tab_index=0,
                )
                moved_widget = WidgetStub()
                remaining_widget = WidgetStub()
                existing_widget = WidgetStub()
                moved_tab = SimpleNamespace(record=moved, widget=moved_widget)
                source = SimpleNamespace(
                    window_id="source",
                    window_title="Source",
                    tabs={
                        moved.id: moved_tab,
                        remaining.id: SimpleNamespace(record=remaining, widget=remaining_widget),
                    },
                    notebook=NotebookStub([moved_widget, remaining_widget]),
                    side=SimpleNamespace(refresh=lambda: None),
                    master_window=None,
                    window_overview=None,
                )
                destination = SimpleNamespace(
                    window_id="destination",
                    window_title="Destination",
                    tabs={existing.id: SimpleNamespace(record=existing, widget=existing_widget)},
                    notebook=NotebookStub([existing_widget]),
                    side=SimpleNamespace(refresh=lambda: None),
                    master_window=None,
                    window_overview=None,
                    _build_tab_label=lambda record: record.name,
                    on_terminal_key_press=lambda *_args: False,
                    _restore_live_terminal=lambda _record: None,
                    zoomed_font=lambda: "Monospace 13",
                    open_session=lambda *_args, **_kwargs: self.fail("visible tab was reattached instead of reparented"),
                    present=lambda: None,
                    _focus_session=lambda _session_id: None,
                    _queue_current_terminal_geometry_sync=lambda: None,
                    show_toast=lambda *_args, **_kwargs: None,
                )
                theme = object()
                app = SimpleNamespace(store=store, windows=[source, destination], theme=theme)
                app.window_for_tab = lambda session_id: TermDeckApplication.window_for_tab(app, session_id)
                app._refresh_tab_management_views = lambda: None
                source.app_context = app
                destination.app_context = app
                source._renumber_visible_tabs = lambda: TermDeckWindow._renumber_visible_tabs(source)
                destination._renumber_visible_tabs = lambda: TermDeckWindow._renumber_visible_tabs(destination)

                result = TermDeckApplication.move_tab_to_window(app, moved.id, destination)

                self.assertIsNotNone(result)
                self.assertNotIn(moved.id, source.tabs)
                self.assertIs(destination.tabs[moved.id], moved_tab)
                self.assertEqual(source.notebook.pages, [remaining_widget])
                self.assertEqual(destination.notebook.pages, [existing_widget, moved_widget])
                self.assertEqual(moved_widget.detach_count, 0)
                self.assertGreaterEqual(moved_widget.prepare_count, 1)
                self.assertIs(moved_widget.applied_theme[0], theme)
                self.assertEqual(moved_widget.applied_theme[1], "Monospace 13")
                self.assertIs(
                    moved_widget.rebound_handlers["key_press_handler"],
                    destination.on_terminal_key_press,
                )
                refreshed = store.get(moved.id)
                self.assertEqual(refreshed.window_id, "destination")  # type: ignore[union-attr]
                self.assertEqual(refreshed.window_title, "Destination")  # type: ignore[union-attr]
                self.assertEqual(refreshed.tab_index, 1)  # type: ignore[union-attr]
                self.assertEqual(store.get(remaining.id).tab_index, 0)  # type: ignore[union-attr]
            finally:
                store.close()

    def test_hidden_row_reattaches_without_restarting_a_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            try:
                record = store.create(
                    "hidden",
                    "work",
                    "bash",
                    tmp,
                    window_id="source",
                    window_title="Source",
                    visible=False,
                )
                source = SimpleNamespace(
                    window_id="source",
                    window_title="Source",
                    tabs={},
                    master_window=None,
                    window_overview=None,
                )
                open_calls = []
                destination = SimpleNamespace(
                    window_id="destination",
                    window_title="Destination",
                    tabs={},
                    notebook=NotebookStub(),
                    side=SimpleNamespace(refresh=lambda: None),
                    master_window=None,
                    window_overview=None,
                    open_session=lambda candidate, **kwargs: open_calls.append((candidate, kwargs)),
                    _renumber_visible_tabs=lambda: None,
                    present=lambda: None,
                    _focus_session=lambda _session_id: None,
                    _queue_current_terminal_geometry_sync=lambda: None,
                    show_toast=lambda *_args, **_kwargs: None,
                )
                app = SimpleNamespace(store=store, windows=[source, destination])
                app.window_for_tab = lambda session_id: TermDeckApplication.window_for_tab(app, session_id)
                app._refresh_tab_management_views = lambda: None

                TermDeckApplication.move_tab_to_window(app, record.id, destination)

                self.assertEqual(len(open_calls), 1)
                self.assertFalse(open_calls[0][1]["restart_missing"])
                self.assertFalse(open_calls[0][1]["remember_layout"])
                refreshed = store.get(record.id)
                self.assertEqual(refreshed.window_id, "destination")  # type: ignore[union-attr]
                self.assertEqual(refreshed.window_title, "Destination")  # type: ignore[union-attr]
            finally:
                store.close()

    def test_destination_list_excludes_actual_widget_owner_and_is_sorted(self) -> None:
        record = SimpleNamespace(id="tab", window_id="stale")
        current = SimpleNamespace(window_id="current", window_title="Current", tabs={"tab": object()})
        zulu = SimpleNamespace(window_id="zulu", window_title="Zulu", tabs={})
        alpha = SimpleNamespace(window_id="alpha", window_title="Alpha", tabs={})
        app = SimpleNamespace(windows=[current, zulu, alpha])
        app.window_for_tab = lambda session_id: TermDeckApplication.window_for_tab(app, session_id)

        destinations = TermDeckApplication.available_move_windows(app, record)

        self.assertEqual([window.window_id for window in destinations], ["alpha", "zulu"])


if __name__ == "__main__":
    import unittest

    unittest.main()
