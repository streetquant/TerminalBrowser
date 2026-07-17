# T-1000: The terminal manager that never breaks down

<p align="center">
  <img src="docs/assets/t1000-mascot.png" alt="T-1000 mascot" width="320" />
</p>

> Named after the T-1000 from *Terminator 2: Judgment Day* — the antagonist that could not be stopped, never crashed, and kept coming back no matter what.

You already know how to use a browser: open tabs, name your windows, switch between sessions, search across everything, and nothing disappears when you close it. T-1000 brings that exact mental model to your terminal work.

Each tab is a live shell. Each window is a named workspace. Sessions survive restarts via tmux — invisibly, with no tmux knowledge needed. It's the browser UX you're already fluent in, applied to the terminal you actually live in. And like the machine it's named after: it does not crash, does not lose your work, and keeps running.

## How it's structured

```mermaid
flowchart TD
    App(["T-1000"])

    App --> W1["Window: Backend\n(named workspace)"]
    App --> W2["Window: Agents\n(named workspace)"]

    W1 --> T1["Tab: api\nshell • ~/projects/api"]
    W1 --> T2["Tab: tests\npytest -f"]
    W1 --> T3["Tab: shell\nbash"]

    W2 --> T4["Tab: codex-api\ncodex • ~/projects/api"]
    W2 --> T5["Tab: claude-review\nclaude • ~/projects/api"]
    W2 --> T6["Tab: opencode-frontend\nopencode • ~/projects/frontend"]

    style App fill:#1e1e2e,color:#cdd6f4,stroke:#89b4fa
    style W1 fill:#313244,color:#cdd6f4,stroke:#a6e3a1
    style W2 fill:#313244,color:#cdd6f4,stroke:#f38ba8
    style T1 fill:#1e1e2e,color:#a6e3a1,stroke:#45475a
    style T2 fill:#1e1e2e,color:#a6e3a1,stroke:#45475a
    style T3 fill:#1e1e2e,color:#a6e3a1,stroke:#45475a
    style T4 fill:#1e1e2e,color:#f38ba8,stroke:#45475a
    style T5 fill:#1e1e2e,color:#f38ba8,stroke:#45475a
    style T6 fill:#1e1e2e,color:#f38ba8,stroke:#45475a
```

Each **Window** is a named group (like a browser window). Each **Tab** is a live terminal shell backed by tmux — close the app, reopen it, everything is still running.

## Platform support

| OS | Supported | Notes |
|---|---|---|
| **Linux** | ✅ Full | Primary platform. All features work. |
| **macOS** | ⚠️ Partial | GTK3/VTE via Homebrew. `/proc` resource stats unavailable — CPU/RAM monitoring disabled. |
| **Windows** | ⚠️ WSL2 only | Run inside WSL2 (Ubuntu). Native Windows has no GTK3/VTE/tmux. |

## Install

### 🐧 Debian / Ubuntu

```bash
# 1. System dependencies
sudo apt update
sudo apt install -y tmux python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 git python3-pip

# 2. Install T-1000
git clone https://github.com/streetquant/TerminalBrowser.git
cd TerminalBrowser
pip install -e .

# 3. Run
t1000
```

### 🏹 Arch Linux / Manjaro

```bash
# 1. System dependencies
sudo pacman -S --needed tmux python-gobject gtk3 vte3 git python-pip

# 2. Install T-1000
git clone https://github.com/streetquant/TerminalBrowser.git
cd TerminalBrowser
pip install -e . --break-system-packages

# 3. Run
t1000
```

### 🌶️ Fedora / RHEL / CentOS

```bash
# 1. System dependencies
sudo dnf install -y tmux python3-gobject gtk3 vte291 git python3-pip

# 2. Install T-1000
git clone https://github.com/streetquant/TerminalBrowser.git
cd TerminalBrowser
pip install -e .

# 3. Run
t1000
```

### 🍎 macOS (partial support)

> ⚠️ Resource monitoring (CPU/RAM per tab) is disabled on macOS — `/proc` is Linux-only.

```bash
# 1. Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. System dependencies
brew install tmux pygobject3 gtk+3 vte3

# 3. Install T-1000
git clone https://github.com/streetquant/TerminalBrowser.git
cd TerminalBrowser
pip3 install -e .

# 4. Run (may require XQuartz for display)
t1000
```

### 🪩 Windows (WSL2 only)

> ⚠️ T-1000 does not run natively on Windows. Use WSL2 with an Ubuntu distro.

```powershell
# 1. Enable WSL2 and install Ubuntu (PowerShell as Administrator)
wsl --install
# Reboot, then open Ubuntu from Start menu
```

```bash
# 2. Inside Ubuntu WSL2 — same as Debian install above
sudo apt update
sudo apt install -y tmux python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 git python3-pip

# For GUI support install a Wayland/X11 compositor (WSLg is built into Win11)
# Win11: WSLg is automatic. Win10: install VcXsrv and set DISPLAY=:0

git clone https://github.com/streetquant/TerminalBrowser.git
cd TerminalBrowser
pip install -e .
t1000
```

## Features

- **Named windows** — group terminals by project or context, just like browser windows
- **Directory-backed tabs** — every tab opens in a specific path, like a bookmark that runs
- **Persistent sessions** — close the app, reopen it, your tabs are all still there
- **Side pane** — search, filter, multi-select, pause, resume, kill tabs (`F9` to toggle)
- **Command palette** — keyboard-first access to all actions (`Ctrl+Shift+P`)
- **Themes** — built-in light/dark themes with density control
- **Workspace recipes** — save and restore a full set of tabs in one click

## Advanced features

### Pause / resume any session
Free up CPU from a runaway agent without killing it. The tmux session keeps existing — just frozen.
```bash
t1000 pause codex-api          # pause one agent
t1000 pause --tag ai           # pause all agents
t1000 resume --tag ai          # resume all at once
t1000 pause-hot --all          # auto-pause anything using high CPU
```

### Broadcast a command to all agents at once
Send the same text to every tab in a tag group — like texting all your agents simultaneously.
```bash
t1000 send --tag ai "status"                    # ask all agents for status
t1000 send --tag ai "stop and summarise"        # graceful checkpoint
t1000 send --tag ai "continue" --no-enter       # stage input, don't submit yet
```

### Read all agent output without switching tabs
```bash
t1000 capture --tag ai --lines 200             # last 200 lines from every agent
t1000 activity --tag ai                        # who needs attention right now?
t1000 search-output "needs approval" --tag ai  # find specific text across all tabs
```

### Workspace recipes — one command to start everything
```bash
t1000 gui --workspace agents.workspace.json
```
One JSON file defines all windows, tabs, commands, and directories. Reopen any time and everything restores exactly.

### Sessions survive reboots
Close T-1000, reboot the machine, reopen — all tabs come back. Agents that were mid-task can be resumed exactly where they were.

## Keyboard shortcuts

| Action | Key |
|---|---|
| New tab | `Ctrl+Shift+T` |
| Close tab | `Ctrl+Shift+W` |
| Next / prev tab | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Toggle side pane | `F9` |
| Command palette | `Ctrl+Shift+P` |
| Settings | `Ctrl+,` |

## Using with Codex / Claude Code / OpenCode

T-1000 shines as a multi-agent cockpit. See **[docs/codex-claude-workflow.md](docs/codex-claude-workflow.md)** for:
- How to launch 3+ agents in parallel with a single workspace recipe
- The agent lifecycle state machine (running → needs input → paused → done)
- The approval loop — click card in side pane, answer, back to work
- CLI commands to monitor, capture output, and steer agents by tag
- Full day workflow Gantt

You can use T-1000 with Codex, Claude Code, and OpenCode side by side. Give each agent its own tab or tag, then monitor and steer them from the same workspace.

### Quick install using Codex / Claude Code / OpenCode

Paste this prompt into Codex, Claude Code, or OpenCode and it will install T-1000 on your machine:

```
Install T-1000 from https://github.com/streetquant/TerminalBrowser by:
1. Installing system dependencies for this OS (tmux, GTK3, VTE, python3-gi)
2. Cloning the repo into ~/t1000
3. Running pip install -e . inside it
4. Verifying with: t1000 --version
```

## License

MIT
