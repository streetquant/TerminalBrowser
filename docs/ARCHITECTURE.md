# TermDeck architecture

TermDeck is organised around a browser-like process-capsule model:

```text
Workspace/tag -> Window -> Tab -> hidden tmux-backed terminal task
```

The GUI never exposes tmux. The hidden tmux backend keeps terminal tasks alive when the GTK window closes, while the SQLite session store remembers names, tags, windows, visible tabs, and last-known process state.

## Main modules

- `gtk_app.py`: GTK/VTE shell, tab UI, side pane, master task window, and launcher dialogs.
- `tmux_backend.py`: hidden tmux operations: create/resume, attach, send text, capture, pause, resume, terminate.
- `tmux_discovery.py`: parser and adopter for already-running tmux sessions, mapping existing work into external TermDeck process capsules.
- `session_store.py`: SQLite registry for sessions, window restore metadata, schema migration from the first MVP, and restore candidates.
- `resources.py`: Linux `/proc` resource sampling for process trees.
- `terminator_config.py`: Terminator config parser for fonts, palette, cursor, scrollback, window size, and keybindings.
- `task_templates.py`: dependency-aware launch templates used by the GUI and CLI.
- `shortcuts.py`: browser-style shortcut profiles, generated tab focus/pause helpers, Terminator keybinding imports, duplicate detection, and terminal/Emacs conflict reporting.
- `quickstart.py`: first-run readiness report combining dependency checks, Terminator import status, desktop launcher state, project detection, and recommended next actions.

## First-run and keyboard policy

`quickstart` is the browser-like onboarding surface for a new user: it checks whether the lightweight runtime is available, summarises imported Terminator look-and-feel, detects useful project tabs, and suggests commands that can be copied or wired into a future first-run screen. Shortcut resolution is separate from theme import. The default `browser` profile favours familiar browser keys, while `emacs-safe` refuses Terminator overrides that would steal protected shell/readline editing keys.

## Process control

Pause/resume is deliberately separate from hiding a tab. Hiding a tab detaches the VTE/tmux client but leaves the task alive. Pause/resume sends stop/continue signals to the process group behind the tmux pane. Stop is the destructive action that kills the hidden tmux session.

## External tmux adoption

TermDeck normally creates one hidden tmux session per tab. `adopt-tmux` is the migration path for users who already have tmux sessions running before they install TermDeck. Adopted sessions are marked as external, keep their original tmux session name, and store an explicit active pane target so capture, send, pause, and resume can act on the correct pane. If an adopted external session disappears, TermDeck reports it as missing instead of recreating it unexpectedly.

## Lightweight boundary

The current Linux-first MVP avoids Electron and avoids Python package dependencies. GTK/VTE and PyGObject are system libraries, tmux provides persistence, SQLite stores local metadata, and `/proc` provides resource information.
