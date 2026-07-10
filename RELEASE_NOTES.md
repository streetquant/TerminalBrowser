# Release notes

## 0.1.30

This build validates the modern UI through the real GTK runtime and fixes two defects that source-level tests did not expose.

### Runtime fixes

- Replaced unsupported PyGObject `set_data` / `get_data` calls in Sessions-card selection styling with normal Python widget attributes. The previous path crashed when active session cards were rendered.
- Stopped forwarding argparse-consumed `--db` and `--workspace` options into `Gtk.Application`, which previously made the real `termdeck gui` entry point exit with `Unknown option --db`.

### Visual verification

- Added `scripts/render_ui_smoke.py`, which launches the public GUI command inside isolated Xvfb, D-Bus, HOME, SQLite, and tmux environments.
- The harness captures the mapped TermDeck window, reports its dimensions and image size, separates application errors from expected headless portal warnings, and cleans up every temporary process.
- Added a dedicated GitHub Actions `ui-smoke` job that provisions system GTK/VTE bindings, runs the harness, and uploads the PNG frame for review.
- Split CI into Python 3.10–3.12 compatibility checks and a system-Python GTK test job, ensuring PyGObject-dependent tests run with the bindings they require.
- Updated the workflow to `actions/checkout@v7`, `actions/setup-python@v6`, and `actions/upload-artifact@v7`.
- Verified a nonblank `1290x856` PNG frame from the real CLI path with zero TermDeck application error lines.

### Verified

- Full unit test suite: 318 passed, 11 subtests passed.
- Python syntax compilation for `termdeck`, `tests`, and `scripts`.
- Real-window visual smoke render through `termdeck gui --db ... --workspace ...`.
- GitHub repository visibility: private.

## 0.1.29

This release gives TermDeck a cohesive modern visual system while preserving its existing terminal, session, and safety behaviour.

### Visual and interaction redesign

- Replaced the flat chrome with layered dark, noir, and system surfaces, softer borders, modern rounded geometry, restrained shadows, and consistent hover/focus states.
- Reduced header clutter to the most frequent actions and moved secondary workspace, window, shortcut, and appearance controls into a single overflow menu.
- Redesigned the Sessions pane with clearer hierarchy, active/selected counts, stronger search and prompt surfaces, semantic Send/Pause/Resume/Kill actions, and actionable empty-state guidance.
- Added persistent visual emphasis for selected sessions and refined cards, badges, tabs, close buttons, tables, menus, dialogs, tooltips, scrollbars, and toasts.
- Split the generated GTK stylesheet into a dedicated chrome module to keep future visual changes isolated and testable.

### Verified

- Full unit test suite: 316 passed, 11 subtests passed.
- GTK CSS parser accepted dark, noir, and system themes.
- Python syntax compilation for `termdeck` and `tests`.
- GitHub repository visibility: private.

## 0.1.28

This build fixes two direct interaction failures in the Sessions pane and detected-project launcher.

### Added or stabilised

- Ctrl-right-click now adds a session to the current multi-selection from the card body, checkbox, tag, Rename button, or Open button before showing context actions.
- Existing selected sessions remain selected, allowing ergonomic multi-session context actions.
- GTK project-pane creation now materialises `{shell}` and other supported placeholders at launch time.
- Detected project shells no longer execute the literal `{shell}` token or produce a dead tmux pane with exit status 127.

### Verified

- Focused pointer-selection and project-pane runtime regressions.
- Full unit test suite: 312 passed, 11 subtests passed.
- Python syntax compilation for `termdeck` and `tests`.
- GitHub repository visibility: private.

## 0.1.27

This build fixes clipboard ownership, session lifecycle, auxiliary-window closing, and responsiveness at their integration boundaries.

### Added or stabilised

- Installed durable GTK clipboard text after VTE updates its internal selections.
- Reused one cached tmux all-pane snapshot for repeated single-session PID lookups.
- Aligned tmux and process-resource cache lifetimes with the configured UI refresh interval.
- Reduced unchanged Sessions-pane resource rebuilds while preserving periodic health updates.
- Removed killed session tabs from every open TermDeck window and cleared stale selections.
- Explicitly enabled window-manager close controls for auxiliary windows.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Full unit test suite: 309 passed, 11 subtests passed.
- GitHub repository visibility: private.

## 0.1.26

This build focuses on browser-like tab ergonomics and verified UI responsiveness. Main tabs now have a full right-click action menu, pinned tabs have an explicit close path, Sessions row menus understand multi-selection, and expensive refresh/capture paths have behavioural regression tests rather than only static guards.

### Added or stabilised

- Added a main-tab context menu with rename, retag, pin/unpin, hide, kill, Terminal options, Master tasks, hide-other-tabs, and hide-tabs-to-the-right actions.
- Added explicit pinned-tab close guidance and **Unpin and hide tab** so pinned tabs no longer look uncloseable.
- Made Sessions row right-click actions bulk-aware when Ctrl-click selection is active.
- Debounced heavy search/filter refresh callbacks to avoid per-keystroke row rebuilds.
- Added short-lived Master captured-output preview caching to reduce repeated tmux capture work during selection and refresh events.
- Added behavioural tests for debounce coalescing, Master preview caching, and pinned-tab close handling.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Focused UI regression tests, debounce tests, Master preview cache tests, and pinned-tab close behaviour tests.
- Full unit test suite in the actual repository checkout.

## 0.1.25

This build makes refresh timing configurable. Users on slower machines can now reduce background refresh churn without changing source code, while the default behaviour remains the same.

### Added or stabilised

- Added `appearance.refresh_interval_seconds` with aliases `ui_refresh_interval_seconds` and `refresh_seconds`.
- Clamped configured refresh intervals to 1–30 seconds.
- Applied the setting to the side pane, master task window, and window overview timers.
- Reused the same interval for the side-pane resource refresh bucket, keeping CPU/RAM/health updates periodic but configurable.
- Added settings and static UI coverage for configurable refresh timing.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted settings and UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.24

This build tightens the responsiveness work by cleaning up refresh timers. Helper windows and side-pane widgets now mark their refresh timers dead on destroy, allowing GLib to stop calling those timers after the UI surface has closed.

### Added or stabilised

- Added `_refresh_alive` flags to overview, master, and side-pane refresh surfaces.
- Wired destroy handlers that set `_refresh_alive` to `False`.
- Updated refresh timer callbacks to return `False` after teardown so GLib self-cancels them.
- Added static regression coverage for timer cleanup behaviour.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.23

This build completes the side-pane refresh fix by avoiding stale resource badges. The unchanged-signature fast path still prevents redundant row rebuilds, but the signature now includes a timed refresh bucket so CPU, RAM, and health cues are refreshed periodically even when session metadata has not changed.

### Added or stabilised

- Added a side-pane `_refresh_bucket_seconds` value for timed dynamic refreshes.
- Included a `time.monotonic()` bucket in the side-pane refresh signature.
- Kept the previous blank-pane fix: rows are cleared only after the cache check decides a rebuild is needed.
- Extended static tests to guard against both blanking and stale-resource regressions.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.22

This build fixes a regression introduced by the responsiveness cache. The Sessions pane now checks its refresh signature before clearing/rebuilding widgets; if nothing changed, it leaves the existing visible rows in place instead of briefly or permanently blanking the pane.

### Added or stabilised

- Moved side-pane widget clearing after the unchanged-signature fast path.
- Preserved stopped-session filtering and selected-id pruning while avoiding unnecessary row rebuilds.
- Added a static regression test proving the cache check happens before `session_box.remove(...)`.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.21

This build continues the responsiveness work by caching resource snapshots. After tmux pane metadata is cached, the next expensive repeated operation is `/proc` sampling: walking child process trees, reading process stat data, and summing RSS. TermDeck now reuses a short-lived per-PID resource snapshot across adjacent UI refreshes.

### Added or stabilised

- Added `ResourceSampler(..., snapshot_cache_ttl=...)` with a default short-lived snapshot cache.
- Cached per-PID resource snapshots so side pane, master task view, and overview refreshes can reuse recent process-tree/RSS samples.
- Added `ResourceSampler.invalidate_cache()` for full or per-PID cache invalidation.
- Added tests proving snapshot reuse and explicit invalidation resampling.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted resource sampler tests.
- Unit test suite in the actual repository checkout.

## 0.1.20

This build targets responsiveness. The hidden tmux backend now caches the expensive `list-panes -a` pane/PID snapshot for a short interval, allowing the side pane, master task view, and window overview to share one tmux query instead of spawning repeated tmux processes during adjacent refreshes.

### Added or stabilised

- Added `TmuxBackend(..., pane_cache_ttl=...)` with a default one-second pane metadata cache.
- Reused cached pane maps across `pane_pids()` calls while keeping direct `pane_pid()` behaviour unchanged for explicit process controls.
- Invalidated cached pane metadata after session creation, rename, and termination.
- Added tests for cache reuse and explicit invalidation refresh.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.19

This build fixes the most visible interaction papercuts: selected terminal text now goes to the real system clipboard for pasting outside TermDeck, cursor blinking can be disabled, the Sessions pane can move left/right, killed tabs disappear from the Sessions pane, Ctrl-clicking tags multi-selects sessions, helper windows have explicit Close/Esc exits, and periodic UI refresh work is reduced.

### Added or stabilised

- System clipboard writes now use `Gtk.Clipboard.get_for_display` first, with PRIMARY and VTE copy fallbacks retained.
- Added terminal right-click menu actions for copy, paste, paste primary, select all, and clear selection.
- Added `appearance.terminal.cursor_blink` and a Terminal options checkbox to stop cursor blinking.
- Added persistent `appearance.side_pane_position` plus right-click Sessions header actions to move the pane left/right.
- Sessions pane filters out stopped/killed sessions and drops them from current multi-selection.
- Ctrl-click on a session tag toggles that session in the multi-selection.
- Master, search, and overview helper windows now expose explicit Close buttons and Esc close behaviour.
- Side-pane refresh now skips unchanged rebuilds and background timers run less aggressively.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.18

This build strengthens terminal select-to-copy behaviour. Text selected with the cursor is copied into the system clipboard and PRIMARY selection so it can be pasted into other applications immediately, while Ctrl+Shift+C remains available as an explicit fallback.

### Added or stabilised

- Debounced VTE selection-change handling so double-click, triple-click, keyboard selection, and drag selection all synchronise selected text after VTE settles its selection buffer.
- Immediate left-button-release copying remains in place for browser-like mouse selection.
- System clipboard and PRIMARY selection are both updated, with VTE clipboard APIs retained as a fallback for different GTK/VTE backends.
- Static GUI tests now cover the delayed select-to-copy path and cleanup of scheduled clipboard syncs.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.17

This build completes the next visible browser-usability step: global remembered-tab search now has a GTK surface. The feature was already available headlessly in `termdeck search`; it is now reachable from the header and the command palette so users can find terminal tasks without remembering commands.

### Added or stabilised

- Added a GTK `SessionSearchWindow` for remembered terminal tabs.
- Added header and command-palette access to search all tabs.
- Kept captured-output search opt-in and redacted by default.
- Fixed GTK pin/unpin import wiring discovered during static verification.
- Expanded static GUI tests so imported session lifecycle/search helpers are checked without loading GI/VTE.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 169 unit tests covering CLI controls, session search, output search, activity classification, redaction, reconcile, recently closed/reopen flows, pinned sessions, GUI search/static wiring, GUI safety-plan wiring, browser snapshots, diagnostics, desktop integration, settings, resource budgets, window snapshots, master task rows, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `search`, `palette`, and `recently-closed` with an in-memory session database.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.16

This build completes the next browser-control step: remembered terminal tabs can now be searched, inspected for activity, reconciled against the hidden tmux backend, pinned, and reopened from a recently closed list without making users type tmux commands.

### Added or stabilised

- Added `termdeck search` for browser-style lookup across remembered tab metadata, with optional redacted captured-output matching.
- Added `termdeck search-output` for cross-session terminal-output search with redacted snippets by default and `--allow-empty` for scripts.
- Added `termdeck activity` to classify captured output as needing input, error, warning, ready/success, active, silent, not started, unavailable, stopped, or archived.
- Added `termdeck reconcile` to repair stale remembered sessions before restore; missing running sessions are marked stopped/hidden while never-started tabs remain visible as new tabs.
- Added `recently-closed` and `reopen-closed` for browser-like hidden-tab recovery without restarting the process.
- Added `pin` and `unpin` lifecycle controls, with pinned sessions surfaced in task/search rows and protected from hot-session cleanup flows.
- Routed GTK bulk controls through the same `ControlPlan` text renderer used by the CLI, so GUI Send+Enter, pause-hot, and kill prompts share the confirmed safety model.
- Fixed schema/model drift around pinned state and aligned the master task row, task snapshot, search, activity, lifecycle, and store migration paths.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 167 unit tests covering CLI controls, search, output search, activity classification, redaction, reconcile, recently closed/reopen flows, pinned sessions, GUI safety-plan wiring, browser snapshots, diagnostics, desktop integration, settings, resource budgets, window snapshots, master task rows, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `search`, `activity`, `recently-closed`, `reconcile`, and `search-output --allow-empty` using an in-memory session database.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.15

This build completes the next usability step: first-run guidance and keyboard predictability. TermDeck now gives new users a browser-like setup report before they launch the GUI, and the shortcut layer explains which keys come from TermDeck defaults, Terminator imports, generated tab controls, and protected terminal/editing behaviour.

### Added or stabilised

- Added `termdeck quickstart`, a JSON readiness report covering tmux, GTK, VTE, Terminator theme import, desktop launcher status, detected project tabs, and recommended next actions.
- Added shortcut profiles: `browser` for familiar tab controls and `emacs-safe` for users who do not want app shortcuts to steal common readline/Emacs-style terminal keys.
- Added `termdeck shortcuts` and `termdeck shortcuts --json` for discoverable keyboard controls, duplicate accelerator reporting, Terminator keybinding import reporting, and terminal-conflict reporting.
- Added GTK shortcut help and command-palette discovery for the shortcut report.
- Cleaned the shortcut model so the CLI, GTK help window, tests, and Terminator-import reporting use one stable shortcut backend.
- Added static GUI coverage for the shortcut help surface to prevent fresh-process shortcut integration regressions.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 139 unit tests covering browser snapshots, CLI commands, quickstart, shortcuts, diagnostics, desktop integration, settings, project detection, resource budgets, window snapshots, master task rows, health badges, control plans, redaction, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `quickstart`, `shortcuts --json`, `shortcuts --profile emacs-safe --json`, `project-detect --recipe`, `budget-plan`, `pause-over-budget`, `tasks --db :memory: --json`, `diagnostics --db :memory:`, `windows --db :memory: --json`, `workspace-plan --sample`, and no-tmux `adopt-tmux --list` JSON error handling.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.14

This build stabilised the supportability layer and carried the browser-tab lifecycle model into the visible UI.

- Added local settings support for global and per-tag health thresholds.
- Added `termdeck settings-sample` and `examples/sample.settings.json`.
- Fixed the fresh-process diagnostics import path by restoring the settings backend.
- Exposed side-pane and master-window Hide/Archive actions without stopping the underlying process.
- Added command-palette discovery for hide/archive lifecycle actions.

## 0.1.13

This build added a migration path for users who already have tmux sessions running outside TermDeck.

- Added `termdeck adopt-tmux --list`, `--dry-run`, and adoption into external browser-like TermDeck tabs.
- Added active-pane targeting for adopted external tmux sessions.
- Added `pane_target` and `external` session metadata with SQLite migration support.

## 0.1.12

This build completed browser-tab lifecycle controls in the headless/controller layer.

- Added `hide`, `show`, `move`, `rename`, `retag`, and `archive`.
- Added redaction-safe `diagnostics` and Terminator layout conversion through `terminator-workspace`.
- Kept tab organisation separate from process control, so hiding or moving a tab does not kill its process.

## 0.1.11

This build completed configurable per-tag resource budgets for browser-like terminal task sessions.

- Added `termdeck budget-plan --sample` and `examples/sample.budgets.json`.
- Added `termdeck budget-plan` for CPU, RAM, process-count, and runtime budget evaluation.
- Added confirmation-gated `termdeck pause-over-budget` for budgets whose action is `pause`.

## 0.1.10

This build improved onboarding and multi-window visibility.

- Added dependency-free `project-detect` and `project-init`.
- Added `termdeck windows` and GTK window overview.
- Added project-to-workspace detection for Node, Python, tests, Makefile, Docker, and logs.

## 0.1.9

This build added user-local desktop/app-menu integration.

- Added `install-desktop`, `install-desktop --dry-run`, and `uninstall-desktop`.
- Added a small SVG icon and freedesktop launcher generation.

## 0.1.8

This build finished the selected-session safety and redaction step.

- Added structured control plans for bulk send, Send+Enter, kill, pause-hot, and destructive-looking text.
- Added default redaction for captured terminal output and master previews.
- Added headless `new`, `workspace-open`, and `tasks` flows so browser-like behaviour can be tested without GTK.

## 0.1.5

This build added scriptable pause/resume/kill/send/capture controls that hide tmux behind browser-like selectors.

## 0.1.0

Initial GTK/VTE MVP with hidden tmux sessions, Terminator theme import, browser-like tabs, side pane, restore model, and master task window skeleton.
