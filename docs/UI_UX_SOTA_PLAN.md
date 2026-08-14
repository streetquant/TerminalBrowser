# TermDeck UI/UX SOTA Plan

Status: proposed · Scope: UI smoothness, latency, and visual polish · Basis: code audit of
`termdeck/gtk_app.py` (8,400 lines), `tmux_backend.py`, `history_control.py`, `chrome_css.py`,
`ui_theme.py`, `resources.py`, `settings.py`, and the smoke/regression test suite.

## 1. Executive summary

TermDeck's UX promise — "browser mental model for terminals, never loses work" — is architecturally
sound, and the design language in `chrome_css.py` is already disciplined (flat surfaces, hairlines,
single accent, 130 ms color transitions). What makes the app feel less smooth than Warp, Ghostty,
Kitty, or VS Code's terminal is not the visual styling; it is that **interactive paths block on, or
round-trip through, the tmux control plane**, and that **the UI is timer-polled and rebuilt wholesale
instead of event-driven and diffed**. GTK3 also caps what motion and rendering quality is reachable.

The plan below is ordered by user-perceived impact per unit of risk. Phases 1–4 make the current
GTK3 app feel dramatically smoother. Phase 6 (GTK4/libadwaita) is what closes the final gap to SOTA.

## 2. Evidence: why it feels un-smooth today

### 2.1 Scrollback is a network-style round trip (largest single issue)
- Wheel scrolling in history is deliberately not VTE-native: each gesture becomes a tmux copy-mode
  command executed on a worker thread, gated by `_HISTORY_SCROLL_ADMISSION = threading.BoundedSemaphore(8)`
  (`gtk_app.py:100`), sequenced by generation counters (`gtk_app.py:977–1162`), with the scrollbar
  value synced back afterwards.
- Consequence: every wheel tick pays a subprocess round trip (tens of ms), ticks queue behind the
  semaphore under fast scrolling, and the scrollbar visibly "catches up". Kinetic/pixel-smooth
  scrolling is impossible in this model.
- The design intent is legitimate — `history_control.py` documents that the live/history boundary
  must be backend evidence, and `test_terminal_history_ownership_cannot_regress_to_native_vte_scrollbar`
  guards it. The fix must preserve that invariant while removing the per-tick round trip (§4, Phase 2).

### 2.2 Synchronous tmux subprocess calls reachable from the GTK main thread
- `TmuxBackend._run` (`tmux_backend.py:~593`) is a blocking `subprocess.run` per query, spawning a
  new `tmux` process each time, with timeouts and a backoff latch as damage control.
- Refresh paths call it from the main loop: `refresh()` (`gtk_app.py:2162`) does `self.store.clear()`
  then `build_task_snapshot(records, backend=...)` — synchronous backend queries while a frame is
  pending. A slow/busy tmux server stalls paint; the existing query-backoff and freeze diagnostics
  (`freeze_diagnostics.py`) exist precisely because this happens.

### 2.3 Poll-everything control plane
- Three independent 5 s full-refresh timers (`gtk_app.py:2152, 3010, 3821` — task pane, side pane,
  master window), a 2 s Codex auth poll (`gtk_app.py:4448–4449`), and a process-monitor poll
  (`gtk_app.py:8300`).
- Each tick rebuilds `Gtk.TreeStore`/`ListStore` from scratch (`store.clear()` + re-append), which
  resets row state, forces full TreeView relayout/repaint, and produces the periodic "blink"/stutter
  cadence users feel even when idle.
- `resources.py` falls back to `_children_map_full_scan()` (line 504) — a full `/proc` walk — on
  every poll when `/proc/<pid>/task/children` is unavailable, burning CPU with many sessions.

### 2.4 Fixed-delay attach choreography (tab open/switch latency)
- `AttachTerminal` spawns only after a 60 ms "stable allocation" timer (`gtk_app.py:685`), with a
  500 ms spawn fallback (`:566`), a 45 ms geometry-sync debounce (`:706`), and a 2,000 ms
  "attach stable" latch (`:857`). Opening or restoring a tab therefore has a floor of ~60–500 ms
  before the shell even starts, plus visible reflow while the grid settles.

### 2.5 Motion and chrome are GTK3-capped and partly disabled
- GTK3 + `Gtk.Notebook` tabs, `Gtk.TreeView` panes, `Gtk.Menu` context menus, `Gtk.Dialog`-based
  palette (`ActionPaletteDialog`, `gtk_app.py:2786`). No animated tab open/close/reorder.
- `Gtk.Stack` transitions are explicitly `NONE` (`gtk_app.py:4300, 4702`) — title/rename switches
  hard-cut.
- Icons are emoji/ASCII glyphs (`KIND_GLYPHS`, `ui_theme.py`) — inconsistent metrics and color across
  fonts; reads as unpolished next to symbolic icon sets.
- GTK3's CPU/cairo renderer drops frames on large-window resize with several VTE widgets; GTK4's GPU
  renderer is the structural fix.

### 2.6 Structural drag
- `gtk_app.py` is an 8,400-line monolith mixing widgets, controllers, timers, clipboard, auth UI, and
  diagnostics. It makes every UI change risky, which in practice means polish doesn't happen.

## 3. SOTA definition (targets)

| Metric | Current (audit estimate) | Target |
|---|---|---|
| Wheel tick → viewport update, p95 | 1 tmux round trip (≈30–100 ms), queued | ≤ 16 ms (one frame), zero round trips |
| Keypress → echo latency, p95 | good (VTE direct) — keep | ≤ 20 ms, regression-gated |
| New tab → first paint | 60–500 ms timer floor | ≤ 50 ms (optimistic shell) |
| New tab → live prompt | variable | ≤ 300 ms p95 |
| Tab switch (no flash, focus correct) | hard cut + occasional reflow | ≤ 100 ms with 120–150 ms crossfade |
| Idle CPU, 20 tabs | periodic poll spikes | < 2 % average, no periodic repaint |
| Main-thread blocking subprocess calls | present in refresh paths | **zero** (CI-enforced) |
| Sustained scroll/resize frame rate | uneven | 60 fps on baseline hardware |

Reference bar: Ghostty/Kitty for latency, Warp for chrome polish and palette UX, Firefox/AdwTabBar
for tab ergonomics, VS Code for panes/virtualized lists.

## 4. The plan

### Phase 0 — Instrument before optimizing (≈3–4 days)
1. Extend `freeze_diagnostics.py` into a frame-budget monitor: record main-loop stalls > 8 ms with
   stack capture; surface a debug HUD toggle (`t1000 --perf-hud`).
2. Add probe scripts alongside the existing smoke harness (`scripts/render_ui_smoke.py`,
   `check_terminal_scroll_smoke.py`): input-to-echo probe, wheel-to-viewport probe, tab-open-to-paint
   probe. Emit JSON; store baselines in `docs/perf-baselines/`.
3. Wire the probes into pytest markers so every later phase lands with before/after numbers.
Exit criteria: baseline numbers for every metric in §3 committed.

### Phase 1 — Event-driven control plane (≈2–3 weeks, biggest systemic win)
1. **Persistent tmux control-mode client.** Replace per-query `subprocess.run` with one long-lived
   `tmux -C` client per server, owned by a dedicated thread in `tmux_backend.py`. Commands are
   written to its stdin and matched to `%begin/%end` replies; notifications (`%output`,
   `%session-changed`, `%window-renamed`, `%unlinked-window-add`, `%exit`) are pushed to the UI via
   `GLib.idle_add` as typed events. Keep the current subprocess path as a fallback behind a setting.
2. **Async facade with a state cache.** The UI reads only an in-memory `TmuxModel` (sessions,
   windows, panes, activity) that the client thread keeps current. All existing call sites that
   query the backend on the main thread switch to cache reads; mutations become fire-and-confirm
   with optimistic UI and rollback on error.
3. **Delete the polling timers.** The three 5 s `_refresh_timer`s and the 2 s auth poll become
   subscriptions on the event bus with a 100–150 ms coalescing debounce (the debounce machinery
   already exists — `test_debounced_refresh.py`). Keep one 30 s reconcile sweep
   (`session_reconcile.py`) as a safety net. Auth state moves to a `GFileMonitor`/inotify watch on
   the auth scope files with the 2 s poll as fallback.
4. **Resource sampling off the hot path.** Sample only rows currently visible, on a worker thread,
   reusing `/proc/<pid>/task/children` (`resources.py:465`) and never the full-scan path during
   steady state; publish deltas to the UI.
Exit criteria: zero main-thread subprocess calls (assert via a lint/pytest guard that greps the call
graph); idle CPU target met; snapshot correctness covered by existing reconcile/liveness tests.

### Phase 2 — Scrollback that never waits (≈2–3 weeks, biggest felt win)
Goal: preserve the backend-evidence invariant of `history_control.py` while removing tmux from the
per-tick path.
1. **Local history mirror.** Stream pane output continuously into a per-tab local ring buffer
   (control-mode `%output` from Phase 1; `pipe-pane` as fallback), seeded on attach with a one-shot
   `capture-pane -S` backfill. Feed VTE's native scrollback from this mirror so the emulator always
   holds the recent N thousand lines locally.
2. **Hybrid scroll ownership.** Wheel/drag within the mirrored region is handled entirely by VTE —
   pixel-smooth, kinetic, zero IPC. Crossing the mirror's upper edge triggers *page-sized* background
   prefetch (e.g. 2,000-line chunks) ahead of the viewport; the user rides the mirror while the next
   page loads. tmux copy-mode round trips remain only for exact live-edge validation and for the
   Codex-transcript / child-TUI owners, which keep today's semantics (`HistoryOwner.CODEX`,
   `CHILD_TUI`).
3. **Boundary correctness.** The live-edge remains backend evidence: the mirror tracks byte/line
   provenance, and `BackendHistoryState`/`ScrollOutcome` stay the source of truth for "where am I".
   Update — don't delete — the two history-ownership regression tests to assert the new invariant:
   *native VTE scroll is allowed only within backend-mirrored content*.
4. **Scrollbar honesty.** One `Gtk.Adjustment` mapped to mirror extent + known backend extent, so the
   thumb no longer jumps when depth reports arrive.
Exit criteria: wheel-tick p95 ≤ 16 ms; fast flick through 50k lines with no queueing; existing
copy/selection pipeline tests still green.

### Phase 3 — Incremental UI updates, no more rebuild-the-world (≈1–1.5 weeks)
1. Replace every `store.clear()` + re-append refresh with keyed diff reconciliation: stable row IDs
   (session/tab id), in-place `set_value` for changed cells, insert/remove only deltas. Preserves
   selection, scroll position, expanded state — kills the 5 s blink.
2. Coalesce redundant snapshot builds: one `build_task_snapshot` per event burst, shared by all
   consumer views (task pane, side pane, master window) instead of three independent builders.
3. Extend the master-preview short-lived cache (`test_master_preview_cache.py`) with
   visibility-gated refresh: hidden previews never refresh.
Exit criteria: no visible flicker on refresh; selection survives updates (add regression test);
steady-state repaint count ≈ 0 when nothing changed.

### Phase 4 — Perceived performance and interaction polish (≈2 weeks)
1. **Optimistic tab open.** Create the tab + terminal widget immediately (< 50 ms) with a quiet
   "connecting" skeleton state; run spawn/attach in the background. Replace the 60 ms/500 ms spawn
   timers with allocation-settled + first-`%output` events; keep timers only as fallbacks.
2. **Motion.** Enable `Gtk.Stack` crossfades (120–150 ms ease-out) for title/name stacks and terminal
   switching; animate side-pane show/hide with `Gtk.Revealer`; add a `reduce_motion` setting honored
   by all transitions. Keep durations in `ChromeTokens` as motion tokens.
3. **Popovers over menus.** Replace `Gtk.Menu` context menus with `Gtk.Popover` (pointing, themed,
   keyboard-navigable); toast show/hide via revealer animation instead of instant swap
   (`gtk_app.py:5100`).
4. **Command palette as overlay.** Move `ActionPaletteDialog` and session search into a `Gtk.Overlay`
   sheet on the main window (Warp/VS Code style): single Ctrl+K entry point, fuzzy match (reuse
   `session_search.py`/`capture_search.py`), recent-first ranking, inline action hints. Dialogs feel
   like interruptions; overlays feel instant.
5. **Focus discipline.** After any navigation (tab switch, pane close, palette dismiss), focus lands
   in the active terminal — extend `test_terminal_focus_routing.py` to cover palette and popover
   paths.
Exit criteria: tab-open-to-paint ≤ 50 ms; all switches animated at 60 fps; palette open ≤ 1 frame.

### Phase 5 — Visual design system pass (≈1–1.5 weeks, parallelizable with Phase 4)
1. Replace emoji `KIND_GLYPHS` with bundled symbolic SVG icons (recolorable via CSS `-gtk-icon-*`),
   one optical size, aligned to a 16 px grid.
2. Formalize the token system in `ui_theme.py`: 4 px spacing grid, type scale (11/12/13/15/20),
   radius scale, elevation (3 shadow levels), motion tokens — then sweep `chrome_css.py` to consume
   tokens only.
3. Add a first-class light theme and a "follow system" mode that also syncs the VTE palette;
   verify contrast ≥ 4.5:1 for all fg/bg pairs (extend `check_visual_polish_smoke.py`).
4. Empty states and zero-data views for panes (no bare white rectangles), hover affordances on tab
   close/new, consistent 1 px hairline usage.
Exit criteria: visual smoke renders reviewed; contrast checks in CI.

### Phase 6 — GTK4 + libadwaita migration (strategic, ≈4–6 weeks; the true SOTA unlock)
Why: GPU-accelerated rendering (smooth resize with many VTE widgets), `AdwTabBar`/`AdwTabView`
(animated, reorderable, overflowing browser-grade tabs — exactly TermDeck's model, replacing the
custom `Gtk.Notebook` machinery), `Gtk.ListView` virtualization for large session lists,
`AdwToastOverlay`, modern popovers/shortcuts window for free. VTE ships GTK4 bindings (`vte4`,
same 2.91 API line).
1. **Prerequisite: split the monolith.** Extract from `gtk_app.py`: `views/window.py`,
   `views/tab_strip.py`, `views/side_pane.py`, `views/terminal_view.py`, `views/palette.py`,
   `controllers/*.py`. Pure mechanical moves first, behavior-preserving, guarded by the existing
   ~90-file test suite. This alone pays for itself even if the port slips.
2. Introduce a thin widget-facade layer for the GTK3-isms (menus→popovers already done in Phase 4,
   event boxes, `show_all`), then port module-by-module behind a `TERMDECK_GTK4=1` flag.
3. Keep GTK3 as the fallback build for one release cycle; drop after parity smoke passes.
Exit criteria: parity on all smoke/regression suites; resize/scroll at 60+ fps; tab DnD animated.

### Phase 7 — Hold the line (ongoing)
1. CI perf gates from Phase 0 probes: fail on p95 regressions > 20 % vs. committed baselines.
2. A `no-main-thread-subprocess` static guard (AST check in tests) and a frame-budget assertion in
   the visual smoke harness.
3. UX regression inbox: keep growing `test_user_reported_ui_regressions.py` — it is already the
   project's best UX asset.

## 5. Quick wins (can ship this week, before Phase 1)
1. Pause all refresh timers while the window is unfocused/minimized; resume with one refresh.
2. Share one snapshot build across the three refresh timers per tick.
3. Preserve TreeView selection/scroll across refresh (cheap subset of Phase 3.1).
4. Enable Stack crossfade at `gtk_app.py:4300/4702` (one-line each) behind `reduce_motion`.
5. Lower the attach fallback from 500 ms to 150 ms and cut the 60 ms allocation timer to 16 ms —
   measurable snappier tab opens with zero architectural risk.

## 6. Sequencing and effort

| Phase | Effort | Depends on | User-visible payoff |
|---|---|---|---|
| 0 Instrument | 3–4 d | — | none (enables everything) |
| 1 Event-driven control plane | 2–3 w | 0 | idle smoothness, no stutter cadence |
| 2 Scrollback mirror | 2–3 w | 1 | *the* smoothness win |
| 3 Incremental updates | 1–1.5 w | 1 | no blink, stable selection |
| 4 Interaction polish | 2 w | 1 (partial) | instant-feeling tabs, palette, motion |
| 5 Design system | 1–1.5 w | — (parallel) | perceived quality |
| 6 GTK4/libadwaita | 4–6 w | 3, 4 | browser-grade tabs, GPU rendering |
| 7 CI gates | ongoing | 0 | never regress |

## 7. Risks

| Risk | Mitigation |
|---|---|
| Control-mode client protocol edge cases (`%begin/%end` interleaving, server restarts) | keep subprocess backend as runtime fallback; reconcile sweep stays; extensive replay tests against recorded control-mode transcripts |
| History mirror divergence from tmux truth (dropped output, resize reflow) | provenance tracking per chunk; checksum spot-audits against `capture-pane`; fall back to copy-mode ownership on divergence |
| Test suite couples to current timers/behaviors | update tests with the invariant they protect, not the mechanism (esp. the two history-ownership tests) |
| GTK4 port scope creep | monolith split first; port behind flag; GTK3 fallback for one release |
| macOS partial support drifts | keep `/proc`-dependent paths isolated behind `resources.py` capability checks |
