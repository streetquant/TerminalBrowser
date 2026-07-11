# TermDeck architecture

TermDeck is organised around one visible hierarchy:

```text
Named window -> Directory-backed tab -> Hidden tmux-backed shell/process
```

A window is the user-defined group. A tab is rooted in one directory and defaults to that directory's basename. The HeaderBar exposes only the window name, New tab, and More; Search and Command palette are overflow actions. The notebook strip renders only the exact tab name and close control; category, pin, health, and attention metadata live in contextual or advanced surfaces. The main layout shows only this minimal tab strip and active terminal; the advanced Tabs pane is hidden by default and persisted through `appearance.tabs_pane_visible`. The GUI does not expose task templates, projects, tags, or tmux as peer concepts. SQLite and tmux remain implementation details that provide restore, persistence, and process control.

## Visual system

`ui_theme.py` defines semantic chrome tokens; `chrome_css.py` converts those tokens into one GTK3 stylesheet. The main root, terminal notebook and explanatory copy use semantic classes (`td-main-root`, `td-terminal-notebook`, `td-section-description`) rather than widget-position selectors alone. The terminal notebook owns the inset frame and active-tab hierarchy, while density-specific CSS reduces spacing without changing the information model. A real GTK smoke verifies these classes, computed margins and primary-action structure before CI captures desktop and compact frames.

## Main modules

- `gtk_app.py`: GTK/VTE named-window UI, immediate shell-tab creation, tab pane, tab manager, window overview, search, and terminal controls.
- `command_render.py`: default shell resolution, portable saved-command placeholders, directory normalisation, and directory-derived tab names.
- `session_launcher.py`: dependency-free creation of one directory-backed tab.
- `session_store.py`: SQLite registry for tabs, named-window metadata, lifecycle state, schema migration, and restore candidates.
- `tmux_backend.py`: hidden tmux operations: create/resume, attach, send text, capture, pause, resume, and terminate.
- `tmux_discovery.py`: migration support for already-running tmux sessions.
- `resources.py`: Linux `/proc` resource sampling for process trees.
- `command_palette.py`: window actions and remembered-tab discovery; it does not contain launch templates.
- `session_search.py`: conservative typo-tolerant matching across tab metadata and optional redacted recent output, using bounded edit distance without an external search dependency.
- `shortcuts.py`: shortcut profiles, generated tab focus/pause helpers, Terminator imports, duplicate detection, and shell-key conflict reporting.
- `quickstart.py`: first-run dependency, directory-tab, Terminator appearance, and desktop-launcher report.
- `workspace_recipe.py`: compatibility and automation format for serialising named windows and tabs. Recipes are not a separate GUI object.

## Creation model

The normal tab-creation flow has no modal decision. TermDeck opens the user's default shell immediately, preferring the active tmux pane's live working directory, then the stored tab directory, then the user's home directory. The initial title comes from that starting directory's final path component; users change location with `cd` inside the terminal. A separate window-name dialog changes the logical group name and persists that title across every tab in the window.

## Terminal attachment and first-frame geometry

`AttachTerminal` waits for a stable VTE `size-allocate` grid before starting the visible tmux client. It seeds both ends of the PTY with that row/column size and keeps the VTE PTY geometry synchronised on later allocations. This prevents tmux from briefly rendering an `80x24` client inside a maximised terminal and filling the unused area with dotted padding.

## Management surfaces

Tab Manager is a per-tab/process operational table with resource data, output preview and controls. Window Overview aggregates those rows into one read-only summary per named TermDeck window. They deliberately answer different questions and share the same underlying snapshots.

## Process control

Hiding a tab detaches the visible terminal client but leaves its hidden process alive. Pause/resume signals the process group. Stop is destructive and terminates the hidden tmux session. These lifecycle controls do not change the user-facing window/tab hierarchy.

## External tmux adoption

`adopt-tmux` remains a migration utility for existing tmux users. Adopted processes become TermDeck tabs, retain their external tmux identity internally, and are reported missing rather than silently recreated if the external session disappears.

## Lightweight boundary

TermDeck remains Linux-first and avoids Electron. GTK/VTE and PyGObject are system libraries, tmux provides persistence, SQLite stores local metadata, and `/proc` provides resource information.
