# TermDeck architecture

TermDeck is organised around one visible hierarchy:

```text
Named window -> Directory-backed tab -> Hidden tmux-backed shell/process
```

A window is the user-defined group. A tab is rooted in one directory and defaults to that directory's basename. The HeaderBar exposes only the window name, New tab, and More; Search and Command palette are overflow actions. The notebook strip renders only the exact tab name and close control; category, pin, health, and attention metadata live in contextual or advanced surfaces. The main layout shows only this minimal tab strip and active terminal; the advanced Tabs pane is hidden by default and persisted through `appearance.tabs_pane_visible`. The GUI does not expose task templates, projects, tags, or tmux as peer concepts. SQLite and tmux remain implementation details that provide restore, persistence, and process control.

## Visual system

`ui_theme.py` defines semantic chrome tokens; `chrome_css.py` converts those tokens into one GTK3 stylesheet. The main root, terminal notebook and explanatory copy use semantic classes (`td-main-root`, `td-terminal-notebook`, `td-section-description`) rather than widget-position selectors alone. The terminal notebook owns the compact frame and active-tab hierarchy, while density-specific CSS reduces spacing without changing the information model. Large shadows and broad CSS transitions are deliberately absent from persistent surfaces. A real GTK smoke verifies these classes, computed margins and New-tab action structure before CI captures desktop and compact frames.

## Main modules

- `gtk_app.py`: GTK/VTE named-window UI, immediate shell-tab creation, tab pane, tab manager, window overview, search, and terminal controls.
- `capacity.py`: explicit startup, GTK-model, and per-refresh working-set limits for large registries.
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

VTE and a persistent Terminator-style scrollbar share the terminal row, and scroll-on-output is disabled. Every visible child is a full-screen tmux client, so VTE's alternate screen reports a one-page adjustment with no drawable thumb and cannot expose the real pane history. The visible scrollbar therefore uses a bounded proxy adjustment whose value is routed to the same session-history controller as wheel events. Normal panes enter tmux copy mode; a foreground Codex process opens its built-in transcript, with large thumb jumps compressed into page navigation. TermDeck suppresses the historical pager's caret at the VTE presentation layer, so transcript navigation cannot display it as a roaming live cursor. A first live key, paste, or ordinary click closes that historical view, restores cursor visibility, and resets the thumb to the live bottom before input reaches the child. tmux mouse capture stays disabled so pointer selection remains VTE-owned. A completed VTE selection is copied to CLIPBOARD and PRIMARY; PRIMARY remains VTE-owned so auto-copy does not clear the visible highlight, and `Shift` preserves the reviewed position.

## Responsiveness boundary

GTK owns the UI thread, while VTE performs the terminal rendering natively. Clipboard reads use GTK's asynchronous request API so an unavailable clipboard owner cannot block input. Startup reconciliation and tab restoration query tmux in batches, reuse the short-lived pane snapshot, and bound every subprocess call. Adaptive deadlines allow a 2,000-row response more time than a single-pane lookup, while failure backoff prevents retry storms. A failed all-pane query is stale state: it preserves last-known PIDs/lifecycle state and never expands into one synchronous query per tab. Capture skips redundant existence checks and clamps requested history.

Startup recreates every visible remembered tab in its original window; there is no VTE attachment cap that defers tabs after a relaunch or reboot. Large-registry refresh work remains bounded: TermDeck samples 128 process trees per UI refresh in round-robin order, materialises 100 Tabs-pane cards, and materialises 500 Tab Manager rows. Registry iteration and filtering may inspect lightweight metadata for all sessions, while `/proc` traversal and management widgets stay bounded. PID churn caches retain at most 4,096 entries. Reconciliation writes changed runtime states through one SQLite transaction. Temporarily unsampled sessions remain alive and addressable.

## Exact reboot recovery

The registry keeps the tab's canonical launch command separate from its observed recovery state. While a managed pane is live, TermDeck walks the descendant process tree and considers every foreground Codex candidate instead of accepting the first launcher wrapper. The native Codex process exposes open rollout JSONL descriptors; TermDeck reads only the bounded metadata record, requires `source="cli"`, rejects structured subagent sources, validates the primary UUID, and persists a canonical `codex` invocation with restart-safe value-free flags followed by `resume <UUID>`. A primary rollout remains authoritative inside Codex's bubblewrap containment even though `--new-session` removes the native process's host controlling TTY; weaker launchers still require foreground-TTY ownership. Positional prompts, config/path/token values, names in place of UUIDs, unknown arguments, background jobs, terminal scrollback, and external/adopted sessions are rejected. Exact UUID recovery is monotonic: a launcher-only observation cannot overwrite an already persisted exact UUID, while a readable idle shell can clear it after Codex exits.

A compatibility resolver handles rows produced before exact UUID persistence existed. After startup reconciliation marks missing processes `recovery-pending`, TermDeck scans at most a bounded number of rollout files and reads only each first metadata record. Launcher-only evidence may select the newest primary CLI session for an exact non-generic working directory; rows without launcher evidence require a unique archive session whose creation or modification time lies near tab creation. Broad roots such as `/home/arch` receive the stricter near-time rule. Already occupied UUIDs are excluded so two tabs cannot concurrently resume one conversation. Rows that cannot be proven exact but have a canonical plain-shell command become new shell surfaces in their saved directory; they are never labelled exact recoveries.

The production backend never places a tmux tab below DMTCP. Real PTY tests showed that DMTCP's checkpoint signal can collide with application handlers, alternative signals can destabilise Bash's fork path, and processes with external HTTPS sockets can remain indefinitely in the kernel-buffer drain phase. The experimental checkpoint manager is therefore disabled by default, and its schema was advanced so legacy images are not treated as trusted reboot state. Dependency-injected test or research backends may still exercise the isolated image-validation machinery, but normal TermDeck tabs never launch or restart from it.

A missing previously running process becomes `recovery-pending`, which is distinct from a deliberately `new` tab. Recovery is fail closed: the production backend runs an exact `codex resume <UUID>` when one was durably observed. Otherwise the tab remains visible with an explicit recovery error and the canonical launch command is not replayed. This prevents a reboot from silently replacing a Codex conversation, Python interpreter, shell job, or networked worker with a different process. Non-Codex workloads must provide application-level checkpoints when reboot continuity matters.

`termdeck-checkpoint.service` has two installation modes. The strict system-level mode is ordered after `user@UID.service` and required mounts while active; systemd reverses that order during shutdown, so `t1000 checkpoint --all` completes its final Codex UUID scan before the user manager, dynamic tmux scopes, and mounted workspaces are stopped. The user-level unit is a compatibility fallback for installations without administrative access. GUI shutdown uses the same persistence operation, and the CLI reports exact Codex recoveries plus explicit unsupported/failure results for other process types.

The optional Tabs pane performs no store, tmux, `/proc`, or widget-rebuild work while hidden. The file-backed SQLite registry uses WAL/NORMAL mode and a bounded lock wait; unsupported filesystems retain SQLite's compatible default journal. No pressure or health path invokes lifecycle controls automatically.

## Frankenterm boundary

Frankenterm is a useful reference for passive-first observation, delta capture with explicit gaps, tiered scrollback, bounded batch retrieval, and pressure hysteresis. TermDeck adopts those control-plane principles where they fit its existing GTK/VTE/tmux architecture. It does not embed Frankenterm or depend on WezTerm. Fleet-wide transcript indexing would belong behind an optional capture/indexing interface rather than on GTK's main thread.

## Management surfaces

Tab Manager is a per-tab/process operational table with resource data, output preview and controls. It partitions registry rows into current Tabs and durable Killed history from the persisted lifecycle status. Remove is post-kill cleanup only; it never signals or deletes a live, paused, new, missing, or pinned tab. Focus routes to the owning window without changing registry ownership. Its row context menu can move a tab between open named windows or create an empty named destination for that tab: registry ownership is updated through the lifecycle layer, while a materialised tab's existing GTK/VTE widget is reparented so neither its client nor tmux task restarts. Window Overview aggregates those rows into one read-only summary per named TermDeck window. They deliberately answer different questions and share the same underlying snapshots.

## Process control

Hiding a tab detaches the visible terminal client but leaves its hidden process alive. Pause/resume signals the process group. Kill is destructive and terminates the hidden tmux session. These lifecycle controls do not change the user-facing window/tab hierarchy.

## External tmux adoption

`adopt-tmux` remains a migration utility for existing tmux users. Adopted processes become TermDeck tabs, retain their external tmux identity internally, and are reported missing rather than silently recreated if the external session disappears.

## Lightweight boundary

TermDeck remains Linux-first and avoids Electron. GTK/VTE and PyGObject are system libraries, tmux provides persistence, SQLite stores local metadata, and `/proc` provides resource information.
