# TermDeck

TermDeck is a lightweight Linux-first terminal application built around two concepts:

1. A **named window** groups related work.
2. Each **tab** is a terminal shell rooted in a chosen directory.

That is the primary product model. There is no task launcher, template catalogue, or project detector. GTK and VTE provide native terminal rendering, while hidden tmux processes allow tabs to survive UI closure without exposing tmux commands to the user.

See `RELEASE_NOTES.md` for the current verified build notes.

## Core workflow

- On a fresh launch, TermDeck asks for the window name first, such as **Client work**, **Backend**, or **Research**.
- After naming the window, choose the directory for its first tab.
- TermDeck never silently creates a generic Main window or opens the process working directory.
- Press **New tab** or `Ctrl+T` to add another directory.
- TermDeck opens the user's default shell in that directory.
- The tab is named from the directory's final component. For example, `/home/me/work/client-api` becomes **client-api**, never the full absolute path.
- Tabs in the same window may use completely different directories.
- The header shows only the window name, New tab, and More.
- Use **More → Rename window** to change the group name at any time.
- Use **More → New TermDeck window** to create another named group.

## What it does

- Native named windows and directory-backed terminal tabs.
- Terminal-first main layout: only the minimal tab strip and active terminal are shown by default.
- Each visible tab contains only its exact name and close control; pin and health metadata stay in contextual/advanced views.
- Optional **Tabs** pane for search, multi-selection, prompting, pause, resume, hide, archive, and stop actions; toggle it with `F9`, More, or the command palette.
- `Ctrl+left-click` multi-selection anywhere on a tab card; right-click remains context-menu only.
- Command palette for window actions, remembered tabs, search, restore, and process controls.
- Tab manager with PID, health, status, CPU, RAM, process count, runtime, command, directory, and captured-output preview.
- Window overview with aggregate tab and resource information.
- Browser-style tab rename, pin/unpin, hide, reopen, hide-other, hide-right, and drag reorder.
- Safe bulk-control plans for destructive or multi-tab operations.
- Typo-tolerant search across remembered tabs and optional redacted recent output.
- Explicit pause/resume/kill; TermDeck never auto-freezes work in the background.
- Terminator appearance and shortcut import.
- Select-to-copy, terminal right-click copy/paste, and configurable cursor/font/scrollback options.
- Local snapshots and JSON window/tab recipes for backup, migration, and automation. These are storage formats, not additional GUI concepts.

## System requirements

Linux with:

```bash
sudo apt install tmux python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
```

PyGObject and VTE are system libraries. No Electron runtime is used.

## Run from source

```bash
python3 -m termdeck doctor
python3 -m termdeck
```

Or install locally:

```bash
python3 -m pip install -e .
termdeck doctor
termdeck
```

## First-run quickstart

```bash
python3 -m termdeck quickstart .
```

The report checks runtime dependencies, summarises imported Terminator appearance, shows the directory's default tab name, checks the desktop launcher, and prints concrete next actions.

## Using the GUI

### Window header

The header contains only the current window name, **New tab**, and **More**. Search tabs, Command palette, window controls, restoration, appearance, and advanced managers are grouped inside More. The operating-system title retains the `TermDeck ·` prefix, but the in-app label does not repeat it.

### Create a tab

Press **New tab** or `Ctrl+T`, choose a directory, and TermDeck opens the default shell there. The directory basename becomes the initial tab title. Double-click the tab title or use its right-click menu to rename it.

### Tab strip

The visible tab strip shows only each tab's name and `×` close control. Category icons, pin emoji, and live health markers are intentionally excluded from the strip. Pinned state remains in the tab tooltip and right-click actions; the close tooltip explains that the session keeps running, while health and resource details remain in the optional Tabs pane and Tab Manager. Long names are visually ellipsised without changing the stored tab name.

### Name a window

Use **More → Rename window**. The name is saved across every tab belonging to that window and appears in the native window title and TermDeck header.

### Create another window

Use **More → New TermDeck window**. Enter the group name, then choose the first tab's directory.

### Tabs pane

The main window starts terminal-only. Press `F9`, use **More → Show/hide tabs pane**, or search for **Tabs pane** in the command palette when advanced multi-tab controls are needed. The preference persists across launches and applies to every TermDeck window. When visible, the pane can be moved left or right and supports `Ctrl+left-click` bulk selection.

## Desktop/app-menu launcher

Preview, install, or remove the local desktop entry:

```bash
termdeck install-desktop --dry-run
termdeck install-desktop
termdeck uninstall-desktop
```

## Command palette

Use **More → Command palette** or `Ctrl+Shift+P` to search window actions and remembered tabs. Useful searches include:

```text
new tab
rename window
new window
search tabs
pause selected
pause hot
tab manager
restore
```

Inspect the same model from the CLI:

```bash
python3 -m termdeck palette
python3 -m termdeck palette "rename window"
python3 -m termdeck palette "pause hot"
```

## Tab Manager and Window Overview

**Tab Manager** is the detailed, actionable view. It shows one row per tab/process with PID, state, health, CPU, RAM, process count, command, directory, captured output and multi-tab controls. Open it with `Shift+Esc`, `Ctrl+Shift+M`, or **More → Tab manager · per-tab details**.

**Window Overview** is the read-only summary. It shows one row per named TermDeck window with aggregate tab counts, resource load and health. Open it through **More → Window overview · window totals**.

## Search, activity, and browser recovery

TermDeck exposes typo-tolerant discovery in both the GUI and CLI. Open **More → Search tabs**, or search for `search tabs` in the command palette, to find remembered tabs by ID, name, window, status, command, directory, and optional redacted recent output. Terms of four or more characters tolerate one edit, or two edits for long words; shorter terms stay literal to avoid noisy matches. Literal matches rank above fuzzy matches. With **Include recent output** enabled, Search Tabs applies the same matching to the latest 120 captured terminal lines. Headlessly, `search` provides the same remembered-tab lookup, while `search-output` remains the direct cross-session output-search command.

```bash
python3 -m termdeck search codex
python3 -m termdeck search pyetst
python3 -m termdeck search "bugbounty error" --include-output --json
python3 -m termdeck search-output "needs approval" --tag ai --lines 300 --json --allow-empty
python3 -m termdeck activity --tag ai --json
```

`activity` turns captured output into browser-task states such as `needs input`, `error`, `warning`, `ready`, `success`, `active`, `silent`, `not started`, and `unavailable`. This gives the Tabs pane and Tab manager a clean way to show which terminal tabs deserve attention without making the project AI-specific.

Hidden tabs can be reopened like recently closed browser tabs, and stale remembered sessions can be reconciled before restore:

```bash
python3 -m termdeck recently-closed
python3 -m termdeck reopen-closed
python3 -m termdeck reconcile --dry-run
python3 -m termdeck reconcile --json
```

`reconcile` does not start processes. It repairs the registry: missing running sessions become stopped/hidden, paused live sessions remain paused, and never-started tabs stay visible as new tabs.

## Safety and redaction

Bulk control is treated as a safety-sensitive action. `send`, `kill`, and `pause-hot` produce a structured control plan when the operation affects several sessions, submits text with Enter, or matches common destructive terminal patterns such as recursive delete, hard reset, credential exfiltration, or force pushes. Add `--yes` only after checking the plan.

Terminal captures are redacted by default in both the Tab manager preview and CLI capture output. The redactor covers private-key blocks, AWS keys, GitHub tokens, OpenAI-style keys, Authorization bearer headers, API-key assignments, password/token assignments, and URL credentials. Use `--raw` only when you deliberately need unredacted output.

```bash
python3 -m termdeck control-plan send --tag ai --text "status"
python3 -m termdeck send --tag ai "status"
python3 -m termdeck send --tag ai "status" --yes
python3 -m termdeck capture --tag ai --lines 200
python3 -m termdeck capture --tag ai --lines 200 --raw
```

## Headless Tab manager snapshot

The same browser-task-manager model is available without launching GTK/VTE. This is useful for scripts, launchers, CI checks, and future side-pane integrations. It shows the remembered tabs with session ID, window, name, tag, PID, state, health, CPU, RAM, focus shortcut, and pause shortcut.

```bash
python3 -m termdeck tasks
python3 -m termdeck tasks codex --json --sort cpu
python3 -m termdeck tasks --tag bugbounty --visible-only
python3 -m termdeck tasks --running-only --sort ram
```


## Headless activity monitor

The activity monitor gives the future side pane and browser-style Tab manager an attention queue without launching GTK/VTE. It captures recent terminal output, redacts sensitive values by default, then classifies each remembered session by what the user probably needs to do next. This is useful for Codex sessions waiting for approval, dev servers reporting errors, test watchers turning green, silent shells, and sessions whose output cannot be captured.

```bash
python3 -m termdeck activity
python3 -m termdeck activity --json
python3 -m termdeck activity --tag ai --sort severity
python3 -m termdeck activity waiting --visible-only
python3 -m termdeck activity --raw
```

The command reports browser-ready fields such as `state`, `severity`, `reason`, `suggested_action`, `preview`, `window`, `tab_index`, `focus_key`, and `pause_key`. `--raw` deliberately disables redaction only for users who need exact captured text.

## Window overview

Open **Windows** from the GUI or search for `windows resource` in the command palette to see the browser-level view. This is the Tab manager-of-windows: it aggregates every remembered tab under its TermDeck window and shows total CPU, RAM, process count, longest runtime, visible tab count, running/paused/stopped summary, hot-session count, critical count, and top health.

The same view is available headlessly:

```bash
python3 -m termdeck windows
python3 -m termdeck windows --json --sort ram
python3 -m termdeck windows agents --sort cpu
python3 -m termdeck windows --visible-only
```

## Health and resource control

TermDeck classifies remembered tasks with local health badges: `OK`, `paused`, `missing`, `many processes`, `high CPU`, `high memory`, `critical CPU`, and `critical memory`.

```bash
python3 -m termdeck health
python3 -m termdeck health --json
python3 -m termdeck pause-hot --all
```

`pause-hot` is explicit. It only pauses high/critical CPU or memory sessions when the user asks for it.


## Configurable resource budgets

TermDeck also supports explicit per-tag resource budgets for teams or heavy local workflows. This keeps the browser-task-manager model adaptable: AI-agent tabs can have a larger memory allowance, while scratch tabs can have a tighter guardrail.

Generate a sample budget file:

```bash
python3 -m termdeck budget-plan --sample > ~/.config/termdeck/budgets.json
```

Preview current sessions against the built-in budgets or a custom budget file:

```bash
python3 -m termdeck budget-plan --json
python3 -m termdeck budget-plan examples/sample.budgets.json --violations-only
python3 -m termdeck budget-plan ~/.config/termdeck/budgets.json --tag ai
```

A budget can set `max_cpu_percent`, `max_memory`, `max_processes`, `max_runtime`, and an advisory `action`. TermDeck does not pause sessions merely because a budget exists. `pause-over-budget` only acts on sessions whose matching budget says `"action": "pause"`, and it still returns a confirmation plan unless `--yes` is supplied.

```bash
python3 -m termdeck pause-over-budget examples/sample.budgets.json --tag scratch
python3 -m termdeck pause-over-budget examples/sample.budgets.json --tag scratch --yes
```

## Settings and diagnostics

TermDeck supports a small local JSON settings file for resource-health thresholds. This keeps the Tab manager useful across different workloads without adding a heavy preferences system. Generate a starter file with:

```bash
termdeck settings-sample > ~/.config/termdeck/config.json
```

The settings file can set global health thresholds and per-tag overrides such as larger limits for `ai` sessions. Diagnostics can then include the active settings summary without including environment variables or terminal captures:

```bash
termdeck diagnostics
termdeck diagnostics --output termdeck-diagnostics.zip
```

Diagnostics are designed for open-source issue reports: they include dependency checks, session counts, task/window summaries, settings status, budget status, and tmux availability, while excluding captured terminal output by default.

## Headless launch and session controls

The GUI remains the main experience, but the same browser-like controls are available from the CLI for scripts, launchers, and the future side-pane controller. These commands still hide tmux. Users create tasks by template or command, and target remembered sessions by ID/prefix, exact name, exact tmux name, tag, or `--all`.

```bash
python3 -m termdeck new codex --name braze-agent --tag bugbounty
python3 -m termdeck new --command "npm run dev" --name frontend --tag dev
python3 -m termdeck new shell --no-start
python3 -m termdeck workspace-open termdeck.workspace.json --no-start
python3 -m termdeck control-plan send --tag ai --text "status"
python3 -m termdeck pause codex-one
python3 -m termdeck resume --tag ai
python3 -m termdeck kill --tag scratch
python3 -m termdeck send --to codex-one "Pause and summarise current state"
python3 -m termdeck send --to codex-one "status" --no-enter
python3 -m termdeck capture --tag ai --lines 200
python3 -m termdeck capture --tag ai --raw
```

`capture` redacts common secret-like values by default before printing terminal output. Use `--raw` only when you explicitly need the unfiltered output locally.

`new` creates the same process capsule that a browser-style New Tab action creates in the GUI. `--no-start` remembers the tab without starting tmux, which is useful for recipe checks and CI. Hiding a tab keeps the task alive. `kill` stops the hidden session and hides it from restore.

## Browser-tab lifecycle controls

TermDeck separates browser-tab organisation from process control. These commands change the remembered tab/window model without sending tmux commands, killing processes, or restarting sessions. Use them when you want to clean up the UI, retag work, move sessions between windows, or hide old tasks while keeping their process state unchanged.

```bash
python3 -m termdeck hide codex-one
python3 -m termdeck show codex-one --window-id agents --window-title "Agents"
python3 -m termdeck recently-closed
python3 -m termdeck reopen-closed
python3 -m termdeck move codex-one pytest --window-id dev --window-title "Development"
python3 -m termdeck rename codex-one braze-agent-main
python3 -m termdeck retag bugbounty codex-one
python3 -m termdeck pin codex-one
python3 -m termdeck unpin codex-one
python3 -m termdeck archive old-scratch-session
```

`hide` removes a tab from visible restore, `show` makes it visible again, `recently-closed` lists hidden tabs, `reopen-closed` restores the most recent hidden tab, `move` changes the browser window/tab slot, `rename` changes the tab label, `retag` changes the workspace tag, `pin` protects an important tab from cleanup-style workflows, `unpin` removes that protection, and `archive` removes a remembered session from normal views. None of these lifecycle commands stops a process; use `kill` when you intend to stop the underlying terminal task.

## Adopting existing tmux sessions

TermDeck can register already-running tmux sessions as browser-like tabs. This is intended for migration from a scattered terminal setup: existing work keeps running, while TermDeck records the session name, active pane target, command, working directory, PID, tag, and browser window placement.

```bash
python3 -m termdeck adopt-tmux --list
python3 -m termdeck adopt-tmux --dry-run
python3 -m termdeck adopt-tmux --tag legacy --window-title "Imported tmux"
python3 -m termdeck adopt-tmux my-existing-session --tag bugbounty
```

By default, adoption skips tmux sessions whose names already look TermDeck-managed, so repeated imports do not loop over TermDeck's own hidden sessions. Use `--include-termdeck` only for debugging. Existing tmux sessions are stored as external process capsules; TermDeck targets their active pane for capture, send, pause, and resume, but it does not recreate them if the original tmux session disappears.

## Workspace recipes

Workspace recipes are plain JSON descriptions of windows and tabs. They preserve window titles, tab order, names, tags, commands, working directories, and whether a tab should open visibly.

Generate a sample recipe:

```bash
python3 -m termdeck workspace-plan --sample > termdeck.workspace.json
```

Preview a recipe before opening it:

```bash
python3 -m termdeck workspace-plan termdeck.workspace.json
```

Launch the GUI with a recipe:

```bash
python3 -m termdeck gui --workspace termdeck.workspace.json
```

Register or start the same recipe without opening the GUI:

```bash
python3 -m termdeck workspace-open termdeck.workspace.json --no-start
python3 -m termdeck workspace-open termdeck.workspace.json --dry-run
python3 -m termdeck workspace-open termdeck.workspace.json --no-reuse
```

`workspace-open --dry-run` validates and shows the exact window/tab capsules without writing to the session database. `--no-start` writes the browser-like layout without requiring tmux, which makes recipe checks usable in headless CI. By default, reopening the same recipe reuses equivalent remembered capsules instead of creating duplicate tabs.

Export remembered sessions back into a shareable recipe:

```bash
python3 -m termdeck workspace-export --name my-project > termdeck.workspace.json
python3 -m termdeck workspace-export --visible-only --name visible-tabs > termdeck.workspace.json
```

Minimal recipe:

```json
{
  "name": "my-project",
  "windows": [
    {
      "title": "Development",
      "tabs": [
        {"name": "shell", "tag": "default", "command": "{shell}"},
        {"name": "api", "tag": "dev", "command": "python3 -m http.server 8000"},
        {"name": "tests", "tag": "tests", "command": "pytest"}
      ]
    },
    {
      "title": "Agents",
      "tabs": [
        {"name": "codex", "tag": "ai", "command": "codex"}
      ]
    }
  ]
}
```

Relative `cwd` values resolve relative to the recipe file. A tab can set `visible: false` to start as a hidden remembered session.


## Browser-session snapshots

Workspace recipes are the shareable project format. Browser-session snapshots are different: they are local backups of the actual remembered TermDeck state, including window IDs, window titles, tab order, session IDs, visible/hidden state, stopped/running/paused status, commands, and working directories. Snapshots deliberately omit tmux-owned names so they can be imported safely without colliding with live backend sessions.

Export a local backup:

```bash
python3 -m termdeck snapshot-export --name daily-backup > termdeck.snapshot.json
python3 -m termdeck snapshot-export --visible-only --name visible-tabs > termdeck.snapshot.json
```

Preview what the browser restore layer would reopen:

```bash
python3 -m termdeck restore-plan
python3 -m termdeck restore-plan --tag ai
```

Import a snapshot without starting processes:

```bash
python3 -m termdeck snapshot-import termdeck.snapshot.json --dry-run
python3 -m termdeck snapshot-import termdeck.snapshot.json
python3 -m termdeck snapshot-import termdeck.snapshot.json --replace
```

`--replace` archives existing remembered sessions before import. `--restart-stopped` restores stopped tabs as new remembered tabs rather than stopped tabs. `--preserve-window-ids` keeps the original window IDs, which is useful when restoring onto the same machine.

## Session reconciliation and recovery

Before restoring browser-style windows and tabs, TermDeck can reconcile the remembered SQLite registry against the hidden backend. This prevents a stale tab from looking alive after its tmux session or process tree has disappeared. Reconciliation is conservative: it never starts a new process, never restarts a stopped task, and never kills a running task. It only repairs remembered state.

```bash
python3 -m termdeck reconcile --dry-run
python3 -m termdeck reconcile --tag ai
python3 -m termdeck reconcile --keep-visible
```

By default, remembered `running` or `paused` tabs whose backend session is missing are marked `stopped`, their remembered PID is cleared, and they are removed from visible restore. Tabs that were created with `--no-start` and still have status `new` remain visible because the user may still intend to launch them. The GTK application runs this reconciliation step automatically before browser-style restore.

## Browser-style restoration

Visible tabs are restored on the next launch and grouped by their previous TermDeck window. Hiding a tab removes it from the visible restore set but keeps the hidden tmux-backed task alive. Stopping a session is the destructive action that kills the task. Missing or stopped sessions are not restarted during restore.

## Browser-like shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+T | New shell tab |
| Ctrl+Shift+T | Reopen the most recently closed tab |
| Ctrl+W | Close current tab, keep its tmux session alive |
| Ctrl+Shift+W | Stop current session |
| Ctrl+Q | Quit TermDeck, keep all tmux sessions alive |
| Ctrl+Shift+L | Focus the side-pane command box |
| Ctrl+Shift+P | Open the command palette |
| Shift+Esc / Ctrl+Shift+M | Open Tab manager |
| Ctrl+Shift+Space | Pause/resume current tab process |
| Ctrl+Tab | Next tab |
| Ctrl+Shift+Tab | Previous tab |
| Alt+1..9 | Jump to tab |
| Ctrl+Alt+1..9 | Pause/resume that numbered tab process |
| F9 | Show or hide the Tabs pane |

Terminator keybindings are imported from `~/.config/terminator/config` when present. Inspect the resolved map and any conflicts with:

```bash
python3 -m termdeck shortcuts
python3 -m termdeck shortcuts --json
python3 -m termdeck shortcuts --profile emacs-safe --json
```

The default `browser` profile keeps familiar browser keys such as Ctrl+T, Ctrl+W and Ctrl+Q. The `emacs-safe` profile refuses imported bindings that would steal protected shell/readline editing keys. Emacs-style shortcuts such as Ctrl+A, Ctrl+E, Ctrl+K, Ctrl+R, Ctrl+Y, and Ctrl+W are reported as protected terminal keys so users understand what passes through to the running tab process.

Print the active shortcut report without launching the GUI:

```bash
python3 -m termdeck shortcuts
python3 -m termdeck shortcuts --json
python3 -m termdeck shortcuts --profile emacs-safe
python3 -m termdeck shortcuts --no-terminator
```

The GUI also exposes this through **More ▾ → Keyboard shortcuts** and the command palette action **Show keyboard shortcuts**. The JSON report flags duplicate accelerators, protected terminal-key conflicts, Terminator-imported bindings, and Terminator bindings ignored by the `emacs-safe` profile.

## Terminator import

TermDeck reads the default Terminator profile from:

```text
~/.config/terminator/config
```

It imports:

- `font`
- `use_system_font`
- `foreground_color`
- `background_color`
- `palette`
- `cursor_color`
- `cursor_shape`
- `scrollback_lines`
- common `[keybindings]`
- first layout window size where available

Inspect what TermDeck sees:

```bash
python3 -m termdeck parse-terminator
python3 -m termdeck palette master
```

## Design notes

TermDeck treats every tab as a process capsule:

```text
Workspace/tag -> window -> tab -> tmux-backed terminal process
```

The tmux layer is hidden. It is used because it is lightweight, mature, and lets tasks survive UI closure. The UI remains browser-like.

## Current limitations

This is an MVP, not a finished replacement for mature terminals. The implementation is Linux-first and assumes tmux is installed. Resource accounting uses `/proc`, so the Tab manager is Linux-only. macOS and Windows support should use platform-specific process-group APIs in later releases.

## Repository hygiene

The repository includes a GitHub Actions workflow under `.github/workflows/ci.yml` that runs syntax checks, unit tests, CLI smoke checks, and an isolated GTK visual-smoke job that uploads desktop and compact TermDeck frames.

## Development checks

```bash
python3 -m compileall -q termdeck tests scripts
python3 -m pytest tests -q
```

Render the real GTK window in an isolated Xvfb, D-Bus, HOME, database, and tmux environment:

```bash
python3 scripts/render_ui_smoke.py --output /tmp/termdeck-ui-smoke.png
python3 scripts/render_ui_smoke.py \
  --output /tmp/termdeck-ui-smoke-compact.png \
  --window-width 640 \
  --window-height 520
```

The visual smoke harness captures the actual `termdeck gui` entry point, reports requested and captured dimensions, and fails when the window is clamped instead of honouring the requested compact viewport. It also separates TermDeck application errors from expected headless desktop-portal warnings. It requires `Xvfb`, ImageMagick, `xwininfo`, `xdotool`, `dbus-run-session`, and `tmux`.
