# Changelog

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
