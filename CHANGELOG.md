# Changelog

## 0.2.5

- Added conservative typo-tolerant matching to Search Tabs and the `termdeck search` CLI.
- Terms of four or more characters now support bounded Damerau-Levenshtein matching for insertion, deletion, substitution, and adjacent transposition; short terms remain literal to suppress noisy results.
- Applied the same fuzzy matching to opt-in recent terminal-output search after redaction.
- Kept exact/literal metadata matches ranked above fuzzy alternatives and preserved the all-query-terms-required rule.
- Updated Search Tabs labels, placeholder, preview guidance, and CLI help to describe typo tolerance and the latest-120-lines output boundary.
- Added unit and real-GTK coverage for `pytes`, `pyetst`, literal-over-fuzzy ranking, short-query suppression, and `compilaton` matching captured `compilation` output.
- Verified 336 tests and 15 subtests, all four GTK lifecycle smokes, and desktop/compact renders with zero application or layout errors.

## 0.2.4

- Simplified the visible notebook tab to exactly the user-defined tab name and one close control.
- Removed type/category glyphs, pin emoji, and live health/attention markers from the main tab strip.
- Added end ellipsis and a 24-character width cap so long directory-derived names remain compact without changing their stored value.
- Kept pinned-state guidance in tooltips/context actions and retained health/resource detail in the optional Tabs pane and Tab Manager.
- Added static and real-GTK coverage for exact pinned and renamed tab labels, close-control stability, and absence of visible decoration.
- Added the minimal tab-strip smoke to the GitHub Actions UI job before desktop and compact rendering.
- Verified 330 tests and 11 subtests, the tab-strip smoke, and desktop/compact renders with zero application or layout errors.

## 0.2.3

- Reduced the main HeaderBar to exactly three concepts: the user-defined window name, New tab, and More.
- Removed the decorative status dot, the `Directory tabs` subtitle, and the duplicated `TermDeck ·` prefix from the in-app title.
- Moved Search tabs and Command palette into More while retaining `Ctrl+Shift+P` and all existing keyboard access.
- Kept the native window-manager title as `TermDeck · <window name>` and fixed its initial assignment order after the custom HeaderBar is installed.
- Added a dedicated real-GTK header smoke check for exact visible actions, accessible names, native title, and ordered More-menu contents.
- Verified 330 tests and 11 subtests, the reduced-header lifecycle smoke, and desktop/compact renders with zero application or layout errors.

## 0.2.2

- Changed the default main layout to terminal-first: the tab strip and active terminal are shown, while the advanced Tabs pane starts hidden.
- Replaced the legacy `focus_mode` preference with canonical `appearance.tabs_pane_visible`, defaulting to `false`, while preserving inverse compatibility for existing focus-mode configs.
- Consolidated duplicate pane controls into one persisted **Show/hide Tabs pane** action available from More, the command palette, and `F9`.
- Synchronised the Tabs-pane preference across every open window and ensured newly created windows inherit the current state.
- Removed redundant post-construction `show_all()` calls that remapped hidden panes during saved-layout and restored-window creation.
- Added unit and real-Xvfb smoke coverage for default hiding, `F9`, persistence, legacy migration, cross-window inheritance, and synchronisation.
- Verified 330 tests and 11 subtests, plus desktop and compact terminal-first GTK renders with zero application or layout errors.

## 0.2.1

- Replaced the fresh-launch `Main` window and implicit current-directory shell with an explicit first-run flow: name the window, then choose its first tab directory.
- Centralised first-run and additional-window creation through one application-level path so both flows use the same naming and directory-selection behavior.
- Removed the `open_initial_shell` constructor switch and all automatic current-working-directory tab creation.
- Cancelling the first-run naming dialog now leaves no generic window or silently launched process; cancelling the directory chooser leaves a valid named empty window with a visible New tab action.
- Added behavioral coverage for first-run success, cancellation, additional-window reuse, noninteractive window creation, and activation routing.
- Verified 325 tests and 11 subtests, plus a real Xvfb first-run flow that persisted `Client work` and created a `client-api` tab from the selected directory.

## 0.2.0

- Simplified the product to one visible hierarchy: named TermDeck windows containing directory-backed tabs.
- Replaced task creation with a directory chooser; each new tab opens the user's default shell and uses the directory basename as its initial title.
- Added persistent window naming and a New TermDeck window flow that asks for a group name before choosing the first tab directory.
- Removed the task launcher, task-template modules, template CLI command, custom-task GUI path, and launcher shortcuts.
- Removed project detection from the GUI, command palette, quickstart report, CLI, and executable package.
- Simplified `termdeck new`: removed `--tag`, changed the JSON result from `sessions` to `tabs`, and stopped exposing internal tag/tmux fields in its normal creation response.
- Removed visible tag chips, Retag actions, and Tag columns from the Tabs pane, Search, and Tab Manager while retaining internal metadata for backward compatibility.
- Reworded the primary GUI around Window, Tabs, New tab, Rename window, Tab manager, and directory-based search.
- Preserved restoration of existing remembered tabs, saved window layouts, Terminator imports, snapshots, and hidden tmux processes.
- Added tests for directory chooser behavior, directory-derived titles, scoped window renaming, timestamp preservation, and removal of obsolete launcher/project surfaces.
- Verified 319 tests and 11 subtests, plus desktop and compact GTK renders with zero application or layout errors.

## 0.1.32

- Replaced Ctrl+right-click session collection with standard Ctrl+left-click multi-selection across the complete Sessions card.
- Ctrl+left-click now toggles a session from the card body, checkbox, tag, Rename button, or Open button while preserving peer selections.
- Right-click is now menu-only and no longer changes selection, even when Ctrl is held.
- Updated Sessions-pane guidance, tag tooltip, README documentation, and pointer regression coverage.
- Verified 323 tests and 11 subtests, plus desktop and compact GTK renders with zero application or layout errors.

## 0.1.31

- Reduced titlebar minimum width by converting New, Shell, Search, Palette, and More to compact icon controls with preserved tooltips and accessible names.
- Added ellipsis-aware title and subtitle bounds so long workspace names no longer force the window wider.
- Marked both `Gtk.Paned` children shrinkable, preserving the preferred desktop split while allowing constrained terminal and Sessions-pane layouts.
- Extended the real GTK visual-smoke harness with explicit window dimensions, requested-versus-captured reporting, and failure on window-size clamping.
- Added a `640x520` compact render to CI alongside the desktop frame and upload both screenshots as review artifacts.
- Added responsive-layout and visual-harness regression coverage; the full suite now passes 321 tests and 11 subtests.

## 0.1.30

- Added an isolated Xvfb visual-smoke harness that launches the real `termdeck gui` entry point, captures the mapped GTK window, and cleans up its temporary HOME, database, D-Bus session, X server, and tmux server.
- Fixed a runtime Sessions-pane crash caused by unsupported PyGObject `set_data` / `get_data` calls; card references now use normal Python widget attributes.
- Fixed `termdeck gui --db ... --workspace ...` by preventing argparse-consumed options from being passed to `Gtk.Application` a second time.
- Added regression coverage for the GTK card-reference path and the CLI-to-GTK argument boundary.
- Added a dedicated GitHub Actions `ui-smoke` job that installs GTK/VTE dependencies, renders the real application, and uploads the captured frame as a CI artifact.
- Repaired CI by separating Python 3.10–3.12 syntax/CLI compatibility checks from the full GTK-enabled test suite, so PyGObject-dependent tests run against system GTK bindings instead of failing during import.
- Updated GitHub Actions to the current `checkout` v7, `setup-python` v6, and `upload-artifact` v7 major releases.
- Updated development documentation for the modern overflow header and repeatable real-window visual verification.

## 0.1.29

- Rebuilt the GTK chrome around layered dark, noir, and system palettes with modern spacing, rounded geometry, elevation, and consistent interactive states.
- Simplified the main header to high-frequency New, Shell, Search, and Palette actions with secondary workspace controls in a responsive overflow menu.
- Redesigned the Sessions pane with a workspace hierarchy, live active/selected counts, stronger search and prompt fields, semantic bulk actions, and a guided empty state.
- Added persistent selected-card styling, clearer health/tag badges, modern tab treatment, refined menus, tooltips, scrollbars, tables, dialogs, and toast feedback.
- Extracted the chrome stylesheet into a dedicated module and added visual-system regression coverage plus GTK parser verification for every theme.

## 0.1.28

- Added Ctrl-right-click selection across the complete Sessions card, including its checkbox and action buttons, while preserving existing multi-selection.
- Updated Sessions-pane guidance and added behavioural coverage for card-wide context selection.
- Materialised portable workspace placeholders such as `{shell}` immediately before GTK project panes create their sessions.
- Prevented detected project shells from launching the literal `{shell}` command and dying with status 127.
- Added a runtime regression test for project-pane command materialisation.

## 0.1.27

- Made select-to-copy durable across focus and selection changes by installing GTK clipboard ownership after VTE updates its internal selections.
- Added a regression test that verifies durable clipboard ownership is the final step in the copy pipeline.
- Shared one cached tmux all-pane snapshot across repeated single-session PID lookups instead of spawning tmux for each UI action.
- Matched tmux and `/proc` cache lifetimes to the configured UI refresh cadence and reduced unchanged Sessions-pane metric rebuild frequency.
- Removed killed session tabs from every open TermDeck window, not only the window that initiated the action.
- Explicitly marked auxiliary windows deletable in addition to their existing Close and Escape paths.

## 0.1.26

- Added browser-style main-tab context menus for rename, retag, pin/unpin, hide, kill, Terminal options, Master tasks, hide-other-tabs, and hide-tabs-to-the-right.
- Made pinned tabs explicitly closeable through a guided **Unpin and hide tab** path instead of silent close-button no-ops.
- Made Sessions-pane row menus bulk-aware for Ctrl-click multi-selection, including retag, pin, unpin, hide, and kill actions.
- Debounced heavy GUI search/filter refreshes and added behavioural tests for refresh coalescing and timer cleanup.
- Cached Master captured-output previews briefly and added behavioural tests proving repeated previews avoid repeated backend capture calls.
- Added behavioural tests for pinned-tab close handling and expanded user-reported UI regression coverage.

## 0.1.25

- Added configurable UI refresh intervals through `appearance.refresh_interval_seconds` / `appearance.ui_refresh_interval_seconds` / `appearance.refresh_seconds`.
- Wired the setting into the side pane, master task window, and window overview timers.
- Reused the configured interval for side-pane dynamic resource refresh buckets, so slow machines can reduce background churn without code edits.
- Clamped refresh intervals to 1–30 seconds and added settings/static UI coverage.

## 0.1.24

- Fixed periodic refresh timer leaks after helper windows or side-pane widgets are destroyed.
- Added `_refresh_alive` destroy hooks so overview, master, and side-pane timers return `False` and self-cancel after teardown.
- Added static regression coverage for timer cleanup paths.

## 0.1.23

- Fixed side-pane resource staleness introduced by the refresh signature cache.
- Added a timed resource-refresh bucket so unchanged session metadata preserves rows briefly but still rebuilds CPU/RAM/health cues periodically.
- Extended the static side-pane regression test to require both pre-clear cache checks and timed dynamic refreshes.

## 0.1.22

- Fixed a side-pane refresh regression where the unchanged-signature fast path could return after clearing all visible session rows.
- Moved row removal/rebuild work after the cache-signature check, preserving the current Sessions pane when nothing changed.
- Added a static regression guard ensuring the side pane checks its refresh signature before clearing widgets.

## 0.1.21

- Added short-lived per-PID resource snapshot caching so adjacent side pane, master, and overview refreshes do not repeatedly walk `/proc` trees and RSS data for the same process.
- Added explicit `ResourceSampler.invalidate_cache()` for process-state changes and tests for cache reuse/invalidation.
- Kept the cache TTL short by default so UI responsiveness improves without stale resource data persisting across interactions.

## 0.1.20

- Added a short-lived tmux pane/PID cache so multiple UI surfaces can share one `list-panes -a` snapshot instead of repeatedly spawning tmux during refresh.
- Invalidated the pane cache after session create, rename, and terminate operations so process controls stay fresh.
- Added backend tests proving repeated `pane_pids` calls reuse the cached snapshot and `invalidate_cache()` forces a refresh.

## 0.1.19

- Fixed select-to-copy to write through the real GTK system clipboard for external paste targets, while keeping PRIMARY and VTE fallbacks.
- Added a terminal right-click menu for copy, paste, paste primary, select all, and clear selection.
- Added cursor blink configuration and a Terminal options checkbox to disable blinking.
- Added persistent left/right Sessions pane placement with right-click header controls.
- Hid stopped/killed sessions from the Sessions pane and added Ctrl-click tag multi-selection.
- Added explicit Close/Esc behaviour to master/search/overview helper windows.
- Reduced periodic heavy UI refreshes and skipped unchanged side-pane rebuilds for better responsiveness.

## 0.1.18

- Strengthened terminal select-to-copy so selected text is synchronised to both the system clipboard and PRIMARY selection.
- Added delayed selection-change copying to cover double-click, triple-click, keyboard selection, and drag-selection timing edge cases.
- Kept immediate left-button-release copying for browser-like mouse selection.
- Expanded static GTK tests to cover the debounced clipboard sync path.

## 0.1.17

- Added `SessionSearchWindow` so remembered-tab search is available from the GTK header and command palette, not only from the CLI.
- Routed the new `search tabs` palette action to the GUI search surface.
- Fixed GTK pin/unpin static import wiring so side-pane and master-window pin buttons resolve cleanly at runtime.
- Expanded static GTK import coverage to include session lifecycle and session search modules.
- Verified 169 tests plus targeted CLI smoke checks before packaging.

## 0.1.16

- Added `termdeck search` for remembered-tab metadata search, with optional redacted captured-output matching.
- Added `termdeck search-output` for cross-session terminal-output search with safe snippets and `--allow-empty` scripting support.
- Added `termdeck activity` to classify captured output into attention-ready browser states such as needs input, error, warning, ready, active, silent, not started, and unavailable.
- Added `termdeck reconcile` to repair stale remembered sessions before restore without starting processes.
- Added `recently-closed` and `reopen-closed` for browser-like hidden-tab recovery.
- Added `pin` and `unpin` lifecycle controls and pinned-state support in storage, search, and task rows.
- Routed GTK selected-session controls through the shared `ControlPlan` safety renderer.
- Fixed pinned-state schema/model drift and verified 167 tests plus CLI smoke checks.

## 0.1.15

- Added `termdeck quickstart` as a first-run readiness report covering dependency checks, Terminator import status, desktop launcher state, detected project tabs, and recommended next actions.
- Added shortcut profiles: `browser` and `emacs-safe`.
- Added `termdeck shortcuts` and `termdeck shortcuts --json` for Terminator keybinding imports, duplicate accelerator reporting, and terminal/editing conflict reporting.
- Added GTK shortcut help and command-palette discovery for keyboard controls.
- Stabilised the shortcut backend used by CLI, GTK, tests, and Terminator-import reporting.
- Added static GUI coverage for the shortcut help surface.
- Verified 139 tests plus CLI smoke checks for quickstart, shortcuts, resource budgets, diagnostics, windows, tasks, workspace samples, and no-tmux adoption error handling.

## 0.1.14

- Added local settings support for global and per-tag health thresholds.
- Added `termdeck settings-sample` and `examples/sample.settings.json`.
- Fixed diagnostics/fresh-process CLI import regression caused by the missing settings backend.
- Exposed browser-tab lifecycle actions in the GTK side pane and master task window.
- Added command-palette actions for hiding and archiving selected sessions.

## 0.1.13

- Added `termdeck adopt-tmux` to list, dry-run, and adopt already-running tmux sessions as browser-like TermDeck tabs.
- Added active-pane targeting for adopted external tmux sessions.
- Added `pane_target` and `external` session metadata with SQLite schema migration.

## 0.1.12

- Added browser-tab lifecycle commands: `hide`, `show`, `move`, `rename`, `retag`, and `archive`.
- Added `session_lifecycle.py` so tab visibility, window placement, naming, tags, and archiving are handled without exposing tmux commands.
- Added `diagnostics` for redaction-safe support bundles.
- Added `terminator-workspace` for converting Terminator layout information into workspace recipes.

## 0.1.11

- Added configurable per-tag resource budgets through `termdeck budget-plan`.
- Added confirmation-gated `termdeck pause-over-budget` for budgets whose action is `pause`.
- Added `examples/sample.budgets.json`.

## 0.1.10

- Added `termdeck project-detect` and `termdeck project-init` for browser-like workspace suggestions from project directories.
- Added detection for Node scripts, package-manager lockfiles, Python markers, tests, Django, Makefile targets, Docker, and logs.
- Added `termdeck windows` and GTK `WindowOverview` for aggregate multi-window resource visibility.

## 0.1.9

- Added `termdeck install-desktop` and `termdeck uninstall-desktop` for user-local desktop/app-menu integration.
- Added dry-run JSON output for desktop launcher installation.
- Added a small embedded SVG icon and freedesktop `.desktop` entry generation.

## 0.1.8

- Added headless browser-style task creation through `termdeck new`, including template resolution, custom commands, remembered window/tab placement, and `--no-start` support.
- Added `workspace-open` to materialise JSON workspace recipes into remembered process capsules without launching the GTK UI.
- Added `tasks` as a headless master-task snapshot.
- Added selected-session safety planning and default capture redaction.

## 0.1.5

- Added scriptable pause/resume/kill/send/capture controls that hide tmux behind browser-like selectors.
- Added JSON outputs for side-pane/controller integration.

## 0.1.0

- Initial GTK/VTE MVP with hidden tmux sessions, Terminator theme import, browser-like tabs, side pane, restore model, and master task window skeleton.
