# TermDeck architecture

TermDeck is organised around one visible hierarchy:

```text
Named window -> Directory-backed tab -> Hidden tmux-backed shell/process
```

A window is the user-defined group. A tab is rooted in one directory and defaults to that directory's basename. The main layout shows only the tab strip and active terminal; the advanced Tabs pane is hidden by default and persisted through `appearance.tabs_pane_visible`. The GUI does not expose task templates, projects, tags, or tmux as peer concepts. SQLite and tmux remain implementation details that provide restore, persistence, and process control.

## Main modules

- `gtk_app.py`: GTK/VTE named-window UI, directory chooser, tab pane, tab manager, window overview, search, and terminal controls.
- `command_render.py`: default shell resolution, portable saved-command placeholders, directory normalisation, and directory-derived tab names.
- `session_launcher.py`: dependency-free creation of one directory-backed tab.
- `session_store.py`: SQLite registry for tabs, named-window metadata, lifecycle state, schema migration, and restore candidates.
- `tmux_backend.py`: hidden tmux operations: create/resume, attach, send text, capture, pause, resume, and terminate.
- `tmux_discovery.py`: migration support for already-running tmux sessions.
- `resources.py`: Linux `/proc` resource sampling for process trees.
- `command_palette.py`: window actions and remembered-tab discovery; it does not contain launch templates.
- `shortcuts.py`: shortcut profiles, generated tab focus/pause helpers, Terminator imports, duplicate detection, and shell-key conflict reporting.
- `quickstart.py`: first-run dependency, directory-tab, Terminator appearance, and desktop-launcher report.
- `workspace_recipe.py`: compatibility and automation format for serialising named windows and tabs. Recipes are not a separate GUI object.

## Creation model

The normal creation flow has one decision: choose a directory. TermDeck opens the user's default shell in that directory and derives the initial tab title from its final path component. A separate window-name dialog changes the logical group name and persists that title across every tab in the window.

## Process control

Hiding a tab detaches the visible terminal client but leaves its hidden process alive. Pause/resume signals the process group. Stop is destructive and terminates the hidden tmux session. These lifecycle controls do not change the user-facing window/tab hierarchy.

## External tmux adoption

`adopt-tmux` remains a migration utility for existing tmux users. Adopted processes become TermDeck tabs, retain their external tmux identity internally, and are reported missing rather than silently recreated if the external session disappears.

## Lightweight boundary

TermDeck remains Linux-first and avoids Electron. GTK/VTE and PyGObject are system libraries, tmux provides persistence, SQLite stores local metadata, and `/proc` provides resource information.
