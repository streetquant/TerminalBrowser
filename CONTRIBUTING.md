# Contributing to TermDeck

TermDeck aims to stay lightweight, local-first, and browser-simple. Contributions should preserve three product rules:

1. Users should not need to know tmux. Any tmux use must remain behind TermDeck actions such as New Tab, Restore, Pause, Resume, and Stop.
2. A tab represents a terminal task/process capsule, not just a text buffer. Resource visibility and process control matter.
3. The app should remain lightweight. Prefer GTK/VTE, Python stdlib, SQLite, `/proc`, and small shell integrations over bundled browser runtimes or large dependencies.

Before submitting a change, run:

```bash
python3 -m compileall -q termdeck tests
python3 -m unittest discover -s tests -v
python3 -m termdeck templates codex
python3 -m termdeck sessions --db :memory:
```

GUI changes should still keep the core modules testable without GTK installed. Put dependency-free logic in separate modules and cover it with unit tests.
