# TerminalBrowser

A Linux-first GTK + VTE terminal application. Named windows group related work; each tab is a shell rooted in a chosen directory. Tabs survive UI closure via hidden tmux — no tmux knowledge required.

## Features

- **Named windows** — group terminals by project or context
- **Directory-backed tabs** — every tab opens in a specific path
- **Persistent sessions** — tabs survive app restarts via tmux
- **Side pane** — search, filter, multi-select, pause, resume, kill tabs (`F9` to toggle)
- **Command palette** — keyboard-first access to all actions
- **Themes** — built-in light/dark themes with density control
- **Workspace recipes** — launch predefined sets of tabs in one click

## Install

```bash
pip install terminalbrowser
```

Requires: Python 3.11+, GTK 3, VTE, tmux.

## Run

```bash
terminalbrowser
```

## Keyboard shortcuts

| Action | Key |
|---|---|
| New tab | `Ctrl+Shift+T` |
| Close tab | `Ctrl+Shift+W` |
| Next / prev tab | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Toggle side pane | `F9` |
| Command palette | `Ctrl+Shift+P` |
| Settings | `Ctrl+,` |

## License

MIT
