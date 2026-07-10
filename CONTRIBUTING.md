# Contributing to TermDeck

TermDeck aims to stay lightweight, local-first, and conceptually simple. Contributions should preserve four product rules:

1. The visible hierarchy is **named window → directory-backed tabs**. Do not add parallel project, task-template, tag, or launcher concepts to the main GUI.
2. New tabs should require only a directory choice, use the user's default shell, and derive their initial title from the directory basename.
3. Users should not need to know tmux. Persistence and process control must remain behind TermDeck actions such as New tab, Restore, Pause, Resume, Hide, and Stop.
4. Prefer GTK/VTE, Python stdlib, SQLite, `/proc`, and small integrations over bundled browser runtimes or large dependencies.

Before submitting a change, run:

```bash
python3 -m compileall -q termdeck tests scripts
python3 -m pytest tests -q
python3 -m termdeck quickstart .
python3 -m termdeck palette "new tab"
python3 -m termdeck sessions --db :memory:
```

GUI changes should keep dependency-free logic in separate modules and cover window naming, directory-derived tab naming, and persistence behavior with unit tests. Changes to the visible hierarchy should also pass the desktop and compact GTK visual-smoke renders.
