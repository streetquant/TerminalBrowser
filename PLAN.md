# TermDeck implementation plan and refinement rounds

## Initial plan

Build a lightweight native terminal application with one visible hierarchy: named windows containing directory-backed tabs. The product should not expose tmux, projects, tags, task templates, or terminal-multiplexer language as peer concepts. Each tab runs the user's shell or a remembered command in its directory.

Initial stack: custom app shell, embedded terminal widget, hidden persistent session backend, Terminator visual configuration importer, local SQLite registry, Linux resource sampling, and JSON workspace recipes.

## Refinement round 1: avoid heavy UI stacks

Electron would make browser-style UI easy, but it would violate the lightweight goal. The plan therefore uses GTK + VTE, which is close to Terminator's own ecosystem and avoids shipping a browser runtime.

Final choice after round 1:

- GTK 3 for application UI.
- VTE 2.91 for terminal rendering.
- No Python package dependency for the core.
- System packages only: `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-vte-2.91`, and `tmux`.

## Refinement round 2: hide tmux, but use it for persistence

A direct PTY model would be simpler, but active processes would die when the UI closes unless a separate daemon is built. A daemon is a better long-term architecture, but too large for the first implementation. The MVP therefore uses tmux as a hidden persistence layer.

Final choice after round 2:

- One hidden tmux session per TermDeck tab.
- Each visible tab attaches to the hidden session.
- Closing a visible tab detaches; it does not kill the hidden task.
- Stopping a session is a separate destructive action.
- GUI and CLI control sessions through browser-like actions, not tmux commands.

## Refinement round 3: make task control the product centre

The project should not compete as another generic terminal. The differentiator is task control: resource usage, session IDs, health labels, pause/resume, selected-session prompting, captured output preview, command palette, and browser-like task management.

Final choice after round 3:

- Right-side Tabs pane for filtering, selection, prompts, status, pause, resume, stop, restore, and the Tab manager.
- Tab manager showing ID, window, name, PID, health, state, CPU, RAM, process count, runtime, shortcuts, command, directory, and captured output.
- Resource monitoring implemented through Linux `/proc` without psutil.
- Terminator config importer implemented as a custom parser for nested Terminator config sections.
- Workspace recipes implemented as dependency-free JSON so project layouts can be shared.

## Fixed MVP scope

Deliver a working Linux-first MVP named TermDeck with:

1. GTK/VTE native terminal UI.
2. Browser-like top tabs and multiple windows.
3. Hidden tmux sessions for persistence.
4. SQLite-backed session registry.
5. Terminator font, colour, palette, cursor, scrollback, window-size, and common keybinding import.
6. Right-side session control pane.
7. Universal command palette.
8. Tab manager with resource usage, health, capture preview, and pause/resume/kill controls.
9. Support for any command, not only Codex.
10. JSON workspace recipe import/export.
11. CLI smoke commands for doctor, templates, palette, health, sessions, controls, capture, workspace-plan, and workspace-export.
12. Unit tests for parser, session store, restore grouping, resource sampler, command palette, health classification, master task rows, workspace recipes, CLI parsing, and tmux backend command shape.

## Completed implementation steps

### 1. Browser-style pause shortcuts

Added Ctrl+Alt+1 through Ctrl+Alt+9 to pause/resume visible tab processes, Ctrl+Shift+Space to toggle the current tab, and pause-key display in the Tab manager.

### 2. Directory-backed tab creation

The earlier task-launcher experiment was removed. The production creation flow now asks only for a directory, opens the user's default shell there, and derives the initial tab title from the directory basename.

### 3. Browser-style restore and window grouping

Added persisted `window_id`, `window_title`, `tab_index`, and `visible` metadata. TermDeck restores visible non-stopped sessions by previous window and tab order without restarting missing/stopped commands.

### 4. Command palette and health layer

Added a command palette for named-window actions, remembered tabs, search, restore, and process controls. Added resource health classification for `OK`, `paused`, `missing`, `many processes`, `high CPU`, `critical CPU`, `high memory`, and `critical memory`. Added explicit Pause Hot actions in the side pane, master window, and CLI.

### 5. Master task manager hardening

Moved master task row formatting/filtering into `master_tasks.py` so it can be tested without GTK. Added captured-output preview and separate send modes: type text without Enter, or Send+Enter with confirmation.

### 6. Headless controls

Added CLI controls that use browser-like selectors rather than tmux terms: `pause`, `resume`, `kill`, `send`, `capture`, and `pause-hot`.

### 7. Workspace recipes

Added JSON workspace recipe loading through `workspace-plan` and `gui --workspace`. Added `workspace-export` so remembered sessions can be exported back into a shareable window/tab recipe.

### 8. Headless New Tab creation

Added `new` as the CLI equivalent of the browser-style New Tab action. It can create a task from a built-in template such as `codex` or `shell`, create a custom command with `--command`, place it into a remembered window/tab slot, and either start the hidden tmux session or only remember it with `--no-start`. Added `workspace-open` for materialising recipe files without launching GTK.

### 9. Safe capture defaults

Changed terminal capture to redact common secret-like values by default before JSON export. Added a `--raw` escape hatch for local debugging and kept `--redact` as a compatibility flag. Added regression coverage for Authorization bearer tokens and API-key assignments in captured output.


### 10. Headless master task snapshot

Added `tasks` as a CLI version of the Tab manager. It builds the same browser-task-manager rows without GTK/VTE, supports text and JSON output, filters by query/tag/visibility/running state, and sorts by health, CPU, RAM, runtime, name, tag, window, tab, or status. This makes the master-window model testable in headless environments and usable by side-pane controllers or launchers.

### 11. Safety plan and capture redaction

Added a tested safety layer for selected-session controls. Bulk sends, Enter-submitting sends, kill actions, pause-hot actions, and destructive-looking text produce structured control plans before execution unless the caller passes `--yes`. Added default redaction for captured terminal output in the master preview and CLI capture command, covering Authorization bearer headers, API-key assignments, private keys, common cloud/token formats, credential assignments, and URL credentials.

### 12. Desktop launcher integration

Added a lightweight user-local desktop/app-menu integration path. `install-desktop` writes a freedesktop launcher and SVG icon into the user's XDG data home, with `--dry-run` for inspection and `uninstall-desktop` for cleanup. This keeps the first-run experience closer to a normal browser app while retaining the lightweight GTK/VTE/tmux architecture.

## Verification after this continuation

Verified with:

```bash
python3 -m compileall -q termdeck tests
python3 -m unittest discover -s tests -v
python3 -m termdeck palette master --db :memory:
python3 -m termdeck health --db :memory: --json
python3 -m termdeck tasks --db :memory: --json
python3 -m termdeck workspace-plan --sample
python3 -m termdeck workspace-export --db <temporary-db> --name demo
python3 -m termdeck new codex --no-start --db :memory:
python3 -m termdeck workspace-open examples/sample.workspace.json --no-start --db <temporary-db>
```

Current verified suite size: 139 tests.

## v0.1.9 next concrete step: browser-session snapshots

TermDeck now separates shareable workspace recipes from local browser-session snapshots. Recipes describe a reusable project layout; snapshots back up the actual remembered browser state: window IDs, tab order, session IDs, visible/hidden state, status, command, and working directory. The import path is intentionally no-start so a backup can be inspected or restored on a machine without tmux.

Verification added: snapshot export, schema validation, dry-run import, replace import, restore-plan generation, parser coverage, full compileall, and full unit-test suite.

## v0.2.0 simplification: named windows and directory tabs

The project-detector experiment was removed from the product. TermDeck now exposes one hierarchy: users name a window, choose directories for tabs, and optionally rename those tabs. Workspace recipes remain a compatibility and automation format rather than a third GUI concept.

Verification covers directory-derived names, named-window persistence, launcher/project symbol removal, command-palette simplification, GTK runtime rendering, and the full unit-test suite.

Current verified suite size: 139 tests.

## v0.1.11 next concrete step: configurable resource budgets

TermDeck now has a second, more explicit resource-control layer beyond the default health badges. Users can define JSON budget envelopes by tag, including CPU percentage, memory, process count, runtime, and an advisory action. This makes the browser task-manager model adaptable to mixed workloads: AI-agent sessions can receive a larger budget, while scratch tabs can be guarded more tightly.

Verification added: memory and duration parsing, invalid budget validation, tag-over-global budget selection, multi-metric violation reporting, sample budget loading, budget table rendering, CLI `budget-plan --sample`, JSON budget-plan output against a live process, and confirmation-gated `pause-over-budget` planning. Full compileall and the full unit-test suite pass.

Current verified suite size: 139 tests.

## v0.1.12 next concrete step: browser-tab lifecycle controls

TermDeck now separates browser-tab lifecycle actions from terminal process controls. Users can hide a tab from restore, show it again, move it to another browser window, rename it, retag it, or archive it without typing tmux commands and without stopping the underlying process. This closes a practical gap in the browser mental model: tab organisation is now scriptable and testable independently from pause/resume/kill.

Verification added: lifecycle state transitions, tab-order compaction after hide/move, no process-state mutation for rename/retag/archive/move/show/hide, CLI JSON payloads for `hide`, `show`, `move`, `rename`, `retag`, and `archive`, parser coverage, full compileall, and the full unit-test suite.

Current verified suite size: 139 tests.

## v0.1.13 next concrete step: restore/backup hardening

This step stabilises the supportability layer around diagnostics and local settings. The optional settings module used by diagnostics and configurable health policies is restored so `python3 -m termdeck` imports cleanly in a fresh process. `settings-sample` now emits a starter config for global and per-tag health thresholds, while diagnostics can report the active settings summary without including terminal captures or environment variables.

## v0.1.13 next concrete step: tmux session adoption

Added a migration path for users who already have tmux sessions running outside TermDeck. `adopt-tmux` discovers existing tmux sessions, groups panes by tmux session, selects the active live pane, and registers each external session as a browser-like TermDeck tab without restarting the underlying process. The session model now stores `pane_target` and `external` metadata so adopted sessions can be captured, prompted, paused, and resumed through the same TermDeck controls while avoiding accidental recreation if the external tmux session disappears.

Verification added: tmux pane parsing, session grouping, TermDeck-owned-session skip logic, dry-run adoption, CLI adoption with a fake backend, adopted pane-target routing, database migration through the full store tests, compileall, CLI smoke checks, and the full unit-test suite.

Current verified suite size: 139 tests.

## v0.1.14 next concrete step: lifecycle actions in visible UI

The lifecycle model is now exposed through the GUI as well as the CLI. The side pane and Tab manager can hide or archive selected sessions without killing their processes, and the command palette can discover the same actions through ordinary browser-like search terms. This reduces the gap between the tested headless controller model and the app experience a non-terminal user sees.

Verification added: command-palette lifecycle search, static GTK lifecycle controls, CLI lifecycle smoke checks, full compileall, and the full unit-test suite.

Current verified suite size: 139 tests.

## v0.1.15 next concrete step: first-run and shortcut predictability

TermDeck now exposes a first-run `quickstart` report that combines dependency readiness, Terminator theme import, desktop launcher status, project detection, and recommended next actions into one JSON payload. The shortcut layer supports a browser profile and an emacs-safe profile, reports Terminator-imported bindings, flags duplicate accelerators, and makes protected terminal/editing keys explicit so normal shell and Emacs-like editing behaviour remains understandable. The GTK UI also exposes shortcut help through the header and command palette.

Verification added: quickstart payload tests, CLI quickstart smoke, shortcut-profile tests, Terminator override tests, terminal-conflict reporting, parser coverage, static GTK shortcut-window coverage, full compileall, and the full unit-test suite.

Current verified suite size: 139 tests.

## v0.1.16 next concrete step: browser-tab attention and history hardening

TermDeck now has a browser-style attention layer for remembered terminal tabs. Users can search sessions by metadata, optionally search redacted captured output, inspect captured-output activity states, reconcile stale registry records before restore, pin important tabs, and reopen recently hidden tabs without seeing or typing tmux commands. This step deliberately stabilises the tab/history/control model rather than adding another terminal-rendering feature.

Verification added: metadata search, redacted output search, output activity classification, stale-session reconciliation, recently closed/reopen flows, pinned-session storage and task rows, parser coverage for pin/unpin/search-output/reconcile/activity, CLI smoke checks on an in-memory database, full compileall, and the full unit-test suite.

Current verified suite size: 167 tests.
