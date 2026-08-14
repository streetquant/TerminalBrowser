"""Regression guards for UI smoothness behaviours.

These pin three production behaviours introduced by the UI/UX SOTA plan
(docs/UI_UX_SOTA_PLAN.md, quick wins):

1. Periodic table refreshers share one tmux/proc snapshot pass per tick via
   ``TermDeckApplication.shared_task_snapshot`` instead of each running an
   identical ``build_task_snapshot`` pass.
2. Periodic refresh timers pause while their toplevel window is minimized.
3. Inline rename stacks (window title, tab name) animate with a short
   crossfade instead of hard-swapping widgets.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

GTK_APP = Path(__file__).resolve().parents[1] / "termdeck" / "gtk_app.py"


class UiSmoothnessSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = GTK_APP.read_text(encoding="utf-8")

    def test_periodic_tables_share_one_snapshot_pass(self) -> None:
        self.assertIn("def shared_task_snapshot(self, *, force: bool = False):", self.source)
        # Window overview + tab manager both consume the shared memo.
        self.assertEqual(self.source.count("self.app_context.shared_task_snapshot("), 2)
        # No periodic window refresher may regress to its own private
        # build_task_snapshot pass; the application memo and the bounded
        # side-pane path are the only allowed call sites.
        self.assertEqual(self.source.count("items = build_task_snapshot("), 2)

    def test_manual_refresh_buttons_bypass_the_memo(self) -> None:
        self.assertGreaterEqual(self.source.count("self.refresh(force=True)"), 2)
        self.assertIn("if (\n            not force", self.source)

    def test_refresh_timers_pause_while_minimized(self) -> None:
        self.assertIn("def _refresh_paused(", self.source)
        self.assertEqual(self.source.count("if _refresh_paused(self):"), 3)
        self.assertIn("Gdk.WindowState.ICONIFIED", self.source)

    def test_overview_refresh_preserves_selection(self) -> None:
        overview = self.source.split("class WindowOverview", 1)[1].split("class ShortcutWindow", 1)[0]
        self.assertIn("selected_titles", overview)
        self.assertIn("selection.select_iter(row_iter)", overview)

    def test_periodic_tables_diff_rows_in_place(self) -> None:
        # clear()+re-append blanks tables for a frame and drops scroll state;
        # the keyed reconciler must stay the only periodic refresh path for
        # the overview and tab manager stores (Phase 3 of the SOTA plan).
        self.assertIn("def _reconcile_list_store(", self.source)
        self.assertIn("_reconcile_list_store(self.store, desired_rows)", self.source)
        self.assertIn("_reconcile_list_store(model, desired_rows)", self.source)
        overview = self.source.split("class WindowOverview", 1)[1].split("class ShortcutWindow", 1)[0]
        self.assertNotIn("self.store.clear()", overview)
        master = self.source.split("class MasterWindow", 1)[1].split("class TermDeckWindow", 1)[0]
        self.assertNotIn("self.store.clear()", master)
        self.assertNotIn("self.history_store.clear()", master)

    def test_tmux_events_drive_coalesced_refresh(self) -> None:
        # Phase 1 of the SOTA plan: a persistent control-mode client turns
        # tmux lifecycle notifications into one coalesced refresh instead of
        # waiting for the next polling tick.  The reader-thread callback must
        # marshal via idle_add, bursts must coalesce through a single timeout
        # source, and shutdown must stop the client.
        self.assertIn("from .tmux_control import ControlEvent, ControlModeClient", self.source)
        self.assertIn("def _start_tmux_event_watcher(self) -> None:", self.source)
        self.assertIn("GLib.idle_add(self._queue_event_refresh)", self.source)
        self.assertIn("if self._tmux_event_refresh_source is None:", self.source)
        self.assertIn("self._task_snapshot_cache = None\n        for toplevel in self.get_windows():", self.source)
        shutdown = self.source.split("def do_shutdown", 1)[1].split("def rebuild_theme", 1)[0]
        self.assertIn("client.stop()", shutdown)
        # The watcher must never create tmux sessions on the user's server.
        self.assertIn('[executable, "-C", "attach-session"]', self.source)
        self.assertNotIn('"-C", "new-session"', self.source)

    def test_rename_stacks_crossfade(self) -> None:
        self.assertNotIn("Gtk.StackTransitionType.NONE", self.source)
        self.assertEqual(self.source.count("Gtk.StackTransitionType.CROSSFADE"), 2)
        # Keep the rename animation subtle: both stacks use a short 120ms fade.
        crossfade_durations = re.findall(
            r"StackTransitionType\.CROSSFADE\)\n\s*\w+\.set_transition_duration\((\d+)\)",
            self.source,
        )
        self.assertEqual([int(value) for value in crossfade_durations], [120, 120])


if __name__ == "__main__":
    unittest.main()
