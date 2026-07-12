# Using TerminalBrowser with Codex / Claude Code

TerminalBrowser is purpose-built for running multiple AI coding agents in parallel. Each agent gets its own tab. You monitor, steer, and control them exactly like browser tabs — from one window.

---

## 1. The Mental Model

```mermaid
flowchart LR
    subgraph Browser["🌐 Web Browser (what you know)"]
        direction TB
        BW["Window\n─────\nProject A"]
        BT1["Tab: Gmail"]
        BT2["Tab: GitHub"]
        BT3["Tab: Docs"]
        BW --> BT1
        BW --> BT2
        BW --> BT3
    end

    subgraph TB_App["⚡ TerminalBrowser (same idea)"]
        direction TB
        TW["Window\n─────\nProject A"]
        TT1["Tab: codex\n(agent 1)"]
        TT2["Tab: claude\n(agent 2)"]
        TT3["Tab: tests"]
        TT4["Tab: shell"]
        TW --> TT1
        TW --> TT2
        TW --> TT3
        TW --> TT4
    end

    Browser -->|"same mental model\ndifferent medium"| TB_App
```

---

## 2. Setting Up an Agent Workspace

Create a workspace recipe file once, launch everything in one command.

```mermaid
flowchart TD
    A(["📄 agents.workspace.json"]) --> B["termdeck gui\n--workspace agents.workspace.json"]

    B --> W1
    B --> W2

    subgraph W1["Window: Agents"]
        T1["Tab: codex-api\ncommand: codex\ncwd: ~/projects/api"]
        T2["Tab: codex-frontend\ncommand: codex\ncwd: ~/projects/frontend"]
        T3["Tab: claude-review\ncommand: claude\ncwd: ~/projects/api"]
    end

    subgraph W2["Window: Dev"]
        T4["Tab: api-server\ncommand: npm run dev"]
        T5["Tab: tests\ncommand: pytest -f"]
        T6["Tab: shell\ncommand: {shell}"]
    end
```

**Recipe file** (`agents.workspace.json`):
```json
{
  "name": "my-project",
  "windows": [
    {
      "title": "Agents",
      "tabs": [
        {"name": "codex-api",      "tag": "ai", "command": "codex",  "cwd": "~/projects/api"},
        {"name": "codex-frontend", "tag": "ai", "command": "codex",  "cwd": "~/projects/frontend"},
        {"name": "claude-review",  "tag": "ai", "command": "claude", "cwd": "~/projects/api"}
      ]
    },
    {
      "title": "Dev",
      "tabs": [
        {"name": "api-server", "tag": "dev",   "command": "npm run dev"},
        {"name": "tests",      "tag": "tests", "command": "pytest -f"},
        {"name": "shell",      "tag": "default", "command": "{shell}", "pinned": true}
      ]
    }
  ]
}
```

---

## 3. The Agent Lifecycle

How a single Codex / Claude Code tab moves through states during a session.

```mermaid
stateDiagram-v2
    direction TB

    [*] --> New : workspace recipe loaded\nor tab created manually

    New --> Running : termdeck starts\ncodex / claude process

    state Running {
        direction LR
        Thinking : 🤔 Thinking\n(agent is working)
        Writing  : ✏️ Writing code\n(agent is editing)
        Thinking --> Writing
        Writing  --> Thinking
    }

    Running --> NeedsInput : agent prints a question\nor awaits y/n approval

    state NeedsInput {
        Highlight : 💬 card highlights\nin side pane (F9)
    }

    NeedsInput --> Running : click card → focus tab\ntype answer → Enter

    Running --> Paused  : tb pause codex-api\n→ SIGSTOP sent to tmux pane\n→ CPU freed immediately
    Paused  --> Running : tb resume codex-api\n→ SIGCONT — picks up exactly\n   where it left off

    Running --> Done : agent prints completion\nor exits cleanly

    Done --> Running : tb send codex-api\n"next task: add tests"

    Running --> Hidden : right-click → Hide\n(tab disappears from strip\n but tmux still alive)
    Hidden  --> Running : F9 side pane\n→ click card → focus

    Running --> Stopped : tb kill codex-api\n→ tmux pane closed
    Paused  --> Stopped : tb kill codex-api
    Hidden  --> Stopped : tb kill codex-api

    Stopped --> [*]

    note right of Paused
        💡 Use pause to free CPU\n        while reviewing diffs\n        or waiting for CI.
        Agent resumes from\n        exact same position.
    end note

    note right of NeedsInput
        💡 tb activity --tag ai\n        shows all agents\n        needing attention\n        across every tab.
    end note
```

---

## 4. Monitoring Multiple Agents at Once

The side pane and CLI give you a fleet view without switching tabs.

```mermaid
flowchart TD
    subgraph Fleet["Running Agents (all in parallel)"]
        A1["codex-api\n🟢 active"]
        A2["codex-frontend\n⏸ needs input"]
        A3["claude-review\n🔴 high CPU"]
        A4["codex-tests\n🟢 active"]
    end

    subgraph SidePane["F9 → Side Pane"]
        SP["Search · filter · multi-select\n────────────────────────\ncodex-api       🟢 running\ncodex-frontend  💬 needs input  ◀ click to focus\nclaude-review   🔴 high CPU\ncodex-tests     🟢 running"]
    end

    subgraph CLI["Terminal CLI (headless)"]
        C1["tb activity --tag ai\n→ see who needs attention"]
        C2["tb capture --tag ai\n→ read all agent output"]
        C3["tb send --tag ai 'status'\n→ broadcast a message"]
        C4["tb pause-hot --all\n→ throttle high-CPU agents"]
    end

    Fleet --> SidePane
    Fleet --> CLI
```

---

## 5. The Approval Loop

Codex / Claude often pauses and waits for you. TerminalBrowser makes this instant.

```mermaid
sequenceDiagram
    participant You
    participant SidePane as Side Pane (F9)
    participant Tab as Agent Tab
    participant Agent as Codex / Claude

    Agent->>Tab: "Waiting for approval..."
    Tab->>SidePane: status updates → 💬 needs input
    SidePane->>You: card highlights in pane

    You->>SidePane: click card
    SidePane->>Tab: switch + grab_focus() + present()
    You->>Agent: review diff, press y / Enter

    Agent->>Tab: continues working 🟢
    Tab->>SidePane: status → running

    Note over You,Agent: Zero context switch cost —\nexact same flow as switching browser tabs
```

---

## 6. Full Day Workflow

```mermaid
gantt
    title A day of parallel agent work in TerminalBrowser
    dateFormat HH:mm
    axisFormat %H:%M

    section Morning setup
    Launch workspace recipe          :done, 09:00, 5m
    Agents start working in parallel :done, 09:05, 55m

    section Mid-morning
    codex-api needs approval         :crit, 10:00, 5m
    You review + approve via tab     :done, 10:05, 5m
    codex-frontend completes task    :done, 10:00, 30m
    Send next task to frontend agent :done, 10:30, 2m

    section Afternoon
    Pause high-CPU agents for review :done, 13:00, 10m
    Review captured output (CLI)     :done, 13:10, 15m
    Resume all agents                :done, 13:25, 5m
    All agents working               :done, 13:30, 90m

    section End of day
    Close TerminalBrowser            :done, 17:00, 1m
    Agents keep running in tmux      :active, 17:01, 59m
    Reopen tomorrow — tabs restored  :active, 18:00, 1m
```

---

## 7. Advanced Agent Controls

These are the power moves once you have multiple agents running.

```mermaid
flowchart TD
    subgraph Pause["Pause • Resume"]
        P1["tb pause codex-api\n→ freeze one agent"]
        P2["tb pause --tag ai\n→ freeze ALL agents"]
        P3["tb pause-hot --all\n→ auto-freeze high-CPU"]
        P4["tb resume --tag ai\n→ unfreeze all at once"]
    end

    subgraph Broadcast["Broadcast to all tabs"]
        B1["tb send --tag ai 'status'\n→ every agent gets it"]
        B2["tb send --tag ai 'stop and summarise'\n→ graceful checkpoint"]
        B3["tb send --tag ai 'continue' --no-enter\n→ stage text, don't submit"]
    end

    subgraph Observe["Observe without switching tabs"]
        O1["tb activity --tag ai\n→ who needs attention?"]
        O2["tb capture --tag ai --lines 200\n→ all output in one view"]
        O3["tb search-output 'error' --tag ai\n→ find issues across all tabs"]
        O4["tb tasks --tag ai --sort cpu\n→ resource usage table"]
    end

    subgraph Lifecycle["Lifecycle without losing work"]
        L1["right-click → Hide\n→ gone from strip, still running"]
        L2["tb rename codex-api braze-agent\n→ rename without restart"]
        L3["tb move codex-api --window Agents\n→ reorganise windows live"]
        L4["termdeck (after reboot)\n→ all tabs auto-restore"]
    end
```

---

## Quick Reference

| What you want | How |
|---|---|
| Launch all agents at once | `termdeck gui --workspace agents.workspace.json` |
| See who needs attention | `tb activity --tag ai` |
| Read all agent output | `tb capture --tag ai --lines 200` |
| Send a message to all agents | `tb send --tag ai "status"` |
| Approve in the UI | `F9` → click card → answer in terminal |
| Pause runaway agent | `tb pause claude-review` |
| Pause all high-CPU agents | `tb pause-hot --all` |
| Resume paused agents | `tb resume --tag ai` |
| Restart after reboot | `termdeck` — tabs auto-restore |
