# Release notes

## Unreleased

## 0.2.18

T1000 0.2.18 closes the complete OpenCode composer, scrolling, keyboard, and false-recovery incident without changing the selected model in any existing tab or replacing any payload that is already live.

### Input belongs to the focused terminal exactly once

The parent GTK window previously retained a fallback route for keys pressed while window chrome owned focus. Under a focused VTE, that fallback could observe an event that the terminal had already received, duplicate a printable byte, or make the next control byte look like an immediate submit. The routing boundary is now explicit: browser-style T1000 actions are handled by T1000, while ordinary printable, editing, navigation, and application shortcuts stay on the native GTK-to-VTE input path exactly once.

Plain `Ctrl+L` is no longer reserved as a T1000 no-op. It reaches the focused shell or TUI as form-feed, allowing OpenCode to redraw its complete composer frame. Clipboard text still enters through VTE so an application that negotiated bracketed paste receives one protected `CSI 200~ … CSI 201~` transaction. Embedded newline normalization may be CR or LF according to VTE, but it cannot escape the closing wrapper or synthesize a submit afterward.

### The application owns alternate-screen scrolling

T1000 now classifies the pane before deciding that downward movement is already at the live edge. A mouse-aware alternate-screen application owns both wheel directions. Physical wheel events continue through VTE, and persistent-scrollbar movements are encoded as bounded SGR wheel events through the existing control-mode input transport. The scrollbar tracks a virtual application-history depth, moves incrementally, and returns to the live endpoint rather than reverting or remaining stuck.

The scrollbar worker no longer resolves a current session row through `SessionStore`. SQLite connections are owned by the GTK thread; using one from the bounded history worker could fail silently and revert every thumb movement. The worker captures only the immutable session identity needed by tmux, while live PID/status updates remain on GTK-owned paths.

### Replay preserves a complete composer

Pane replay now restores every relevant mode that tmux 3.7b can report, including bracketed paste. Investigation also found that `#{focus_flag}` is not a tmux format on the production version and expands to an empty field. Treating it as a required state column made otherwise healthy stream setup fail its field-count check and retry indefinitely. The unsupported field has been removed; focus-reporting remains application/tmux-owned rather than inferred from nonexistent metadata.

A real alternate-screen composer smoke now proves, before and after `Ctrl+L`, that the tmux capture and VTE surface both contain the top border, editable body, bottom border, and terminal-bottom marker. It also proves physical wheel up/down, persistent scrollbar up/down, zero tmux copy-mode entry, ordinary text without auto-submit, and protected multiline paste.

### A live pane cannot remain falsely red

A delayed or duplicate pane-dead notification may arrive after the target is already live. The recovery backend now revalidates one stable live pane ID and authoritative PID and returns an explicit `alive` no-op result. It does not respawn, send keys, change the command, or claim that a recovery occurred.

Startup reconciliation separately repairs durable rows whose status contradicts their proven live PID. `recovery-pending`, `missing`, and `start-error` rows become `running`, retain their pane/process identity, and clear stale recovery errors. Paused rows remain paused, ordinary running no-ops do not generate writes, and genuinely missing sessions remain explicit recovery work.

### Existing sessions and model choices remain unchanged

This release does not mass-restart OpenCode, Claude, Codex, or gateway-backed tabs. It does not replace direct `opencode` commands, add `opencode/x-preview-f-free` to tabs that did not already select it, or alter any manually selected model. Existing exact gateway refresh remains an explicit per-tab action with the same pane, resume UUID, working directory, and input-restoration proofs.

The surrounding 0.2.17 contracts remain intact: a clean application exit returns to an interactive shell in the same tab, explicit Kill remains terminal, killed-terminal history remains until Remove, all visible windows restore, and arbitrary non-Codex commands use digest-bound generic recipes without depending on an agent parser.

### Validation boundary

Promotion requires the complete test inventory, every GTK/VTE/tmux smoke, deterministic real-tmux key and scrollbar evidence, false-red repair tests, production-profile validation on the exact clean commit, reproducible double-build, strict wheel/manifest verification, managed installation with the previous immutable release retained, GUI-only handoff, and live post-install comparison proving that tmux server, session, pane, payload, command, model selection, authentication, working-directory, and recovery identities were not lost.

## 0.2.17

T1000 0.2.17 repairs the three live regressions reported against the installed 0.2.9 runtime: model transitions could destroy the pane/stream handoff, the tab-strip `+` could disappear after overflow, and already-running `cc-go-ulw` processes could remain bound to an older gateway-model inventory. The release also completes the generic durable-recovery, killed-history, shell-return, and per-tab scrolling work that was present only in the newer source tree and had never reached the managed installation.

### Model and account changes are protected pane transactions

A target replacement now begins by disconnecting the old control-mode client and marking the tab transition-protected. The backend then mutates the tmux target while no VTE transport is still bound to the old process generation. Completion keeps protection until a newly connected stream remains stable; a failed mutation restores or reconnects the prior surface and records a bounded recovery error. The first key is therefore never delivered into a dead pane, and a model/account transition cannot leave a visually present but input-less tab.

For `cc-go-ulw`, Claude Code memoizes `~/.claude/cache/gateway-models.json` inside each process. Updating the shared cache cannot update a process that started earlier. T1000 now validates the cache as a bounded regular file, canonicalizes and fingerprints its model IDs, compares the cache generation with the tab's observed process start, and shows a highlighted refresh control only when that exact tab is stale.

The refresh itself is deliberately strict:

1. the pane, PID, working directory, and exact `--resume UUID` are observed;
2. terminal input is disabled and the exact process tree is frozen with PID/start-time receipts;
3. the same evidence is collected again after quiescence and any private-scope lease;
4. `respawn-pane -k` replaces only that pane with the exact saved resume command;
5. the same pane ID, a new PID, the same UUID/directory, and a ready process tree are proved;
6. input and the VTE control stream are restored.

A mismatch fails before mutation. The production fleet contained 86 live gateway sessions at investigation time: 85 predated the validated 43-model cache and one was already current. T1000 does not restart those 85 processes automatically because screen capture cannot prove the absence of an unsent composer draft. Each stale tab receives the exact in-place refresh control, while a model-change crash is recovered automatically through the same protected handoff.

### The new-tab control is outside tab overflow

The old `+` was a floating overlay positioned after the last visible tab. Its own layout function hid it when the last tab scrolled outside the notebook allocation. The new control is a `GtkNotebook` end action, independent of the scrollable tab pages. A real GTK regression constrains a 30-tab notebook to a narrow window and proves the button remains visible, mapped, and attached to `Gtk.PackType.END`.

### A tab outlives the application launched inside it

New and recovered application commands run under a small terminal supervisor. A normal exit or Ctrl-C returns to the user's interactive shell in the same pane; other non-zero exits remain real failures and follow the durable restart policy. Pre-upgrade panes that do not have the supervisor receive a one-shot bridge when they die: `on-failure` exit 0/130 becomes a shell-only durable recipe, other failures resume the saved application, and `always` still restarts after every exit.

The bridge changes neither the tmux pane ID nor the TermDeck tab. A real disposable tmux smoke proves that the old process is not relaunched, the shell accepts input, and the new recipe contains no agent recovery command.

### Restoration and history are generic and durable

Every managed tab now owns a canonical JSON launch recipe bound to a SHA-256 digest. The recipe contains only the command, absolute working directory, explicit environment overlay, restart policy, and origin. It does not require Codex, Claude, OpenCode, or any other agent parser. Legacy rows receive an additive backfill; malformed receipts stay explicit rather than being silently replaced.

Killed and terminally exited tabs remain in durable history until explicit removal. Multi-window startup restores every visible tab rather than truncating each window at twelve. External adopted sessions, manual/never policies, corrupt receipts, missing directories, and optional private-account adapters retain fail-closed boundaries.

### Per-tab scrolling is independent again

The persistent scrollbar once again represents the application-aware tmux/TUI history owner, while VTE's native adjustment is used only for a real text selection. Backend evidence—not scrollbar geometry—decides whether the live edge has been reached. Thumb work is coalesced through a bounded executor, stale completions are generation-fenced, selection scrolling stays inside VTE, and switching tabs does not reset another tab's reading position.

### Validation boundary

The release contract includes the complete test inventory, every existing GTK/VTE/tmux smoke, a real generic recovery smoke, and a real exact gateway-model refresh smoke. The new smokes prove same-pane/new-PID exact resume, retained UUID and directory, post-refresh input echo, successful legacy exit to shell, no agent relaunch, explicit `always` semantics, tool-independent recovery, and cleanup of every disposable process and tmux server.

## 0.2.16

T1000 0.2.16 makes persistent terminal ownership independent of the GTK process that presents it. The tmux server is now an explicit lifecycle authority rather than an accidental descendant of whichever GUI process happened to create the first tab.

### The tmux server has its own lifecycle

When no server exists and the caller shares the tmux socket mount with user systemd, the first `tmux new-session` or `start-server` command runs inside a delegated transient scope. In a container or validation mount namespace, T1000 starts the client locally and moves the exact daemon PID into a delegated anchor before the operation returns. Closing, upgrading, or replacing T1000 therefore cannot remove terminal payloads through GUI service cleanup.

Existing servers are handled without restart. T1000 reads the exact tmux server PID, binds it to `/proc/PID/stat` start time, creates a delegated `KillMode=process` anchor, moves only that PID through systemd's `AttachProcessesToUnit`, and then repeats the PID, start-time, cgroup, and unit-policy checks. PID reuse, an unavailable manager, an unsafe existing unit, attachment failure, or identity drift never becomes a destructive fallback.

### GUI activation is now a first-class control operation

The application exports a `quit-preserving-sessions` GApplication action. Release tooling can request orderly persistence and window closure over D-Bus instead of sending an unstructured process signal. The tmux authority remains alive while the old GUI exits and the verified replacement attaches.

### Recovery remains conservative

The generation-safe recovery rules introduced in the 0.2.15 source remain unchanged. Managed tabs use stable session targets rather than persisted `%pane` identifiers. A missing plain shell can start fresh only when it is a trusted configured system shell and carries no recovery metadata. Exact Codex and Claude identities may resume through their validated commands; incomplete or launcher-only agent state stays explicit and fail-closed.

### Validation boundary

The release contract includes focused lifecycle and PID-reuse tests, the complete existing test inventory, all GUI/VTE/tmux smokes, and a disposable real-systemd smoke that proves the first private tmux server enters an independent delegated scope and is fully removed during cleanup.

## 0.2.15

T1000 0.2.15 repairs the Codex composer failure reported against the installed 0.2.14 runtime and closes the surrounding startup, containment-notification, validation, and release-evidence gaps without replacing the existing GTK/VTE/tmux architecture.

### One authoritative geometry exists before the first composer frame

A full-screen TUI draws immediately after its terminal is created. The prior GUI path could create a missing tmux payload at tmux's implicit `80x24`, then resize it after GTK/VTE settled on the real grid. Codex briefly composed two incompatible layouts, leaving clipped borders, displaced blocks, or a collapsed composer.

The accepted invariant is now:

```text
payload first grid = VTE grid = tmux pane grid
                   = tmux window grid = control-stream grid
```

Visible tabs, first tabs in empty windows, restored missing tabs, exact recovery, hidden workspace recipes, and all-hidden windows all use the canonical mapped grid. Deferred startup owns rollback of a newly created session row and partial tmux target, while durable recovery rows retain their retry semantics.

### Private contained-resize admission is exact and fail closed

A resize notification is admitted only for the production containment graph:

```text
tmux pane root -> outer bwrap -> inner bwrap session leader -> private Codex witness
```

The witness must carry the complete private TermDeck tuple:

```text
CODEX_HOME
TERMDECK_AUTH_SCOPE_ID
CODEX_TERMDECK_GATE_ROOT
```

Each key must occur exactly once; values must be UTF-8, canonical, absolute where required, scope-consistent, and unchanged at commit. Copied names, partial/duplicate tuples, intervening processes, sibling sandboxes, ambiguous witnesses, changed parent edges, or unreadable evidence produce a no-op.

On pidfd-capable Linux systems, the exact witness is opened with `pidfd_open`, revalidated while the descriptor is held, checked for exit, and sent only `SIGWINCH` through `pidfd_send_signal`. The numeric process-group fallback remains available only for genuine capability absence or `ENOSYS`; lookup, permission, identity, topology, tuple, liveness, and sender failures never widen into fallback.

### Resize work no longer blocks ordered terminal input

One bounded process-wide dispatcher performs containment discovery and signaling outside the pane worker. Ordinary resize requests are latest-value coalesced by callback/pane identity. Setup nudge and final notifications use unique bounded tickets so recovery still completes before pane capture and replay. Queue capacity, history, wait time, error representation, and shutdown are finite.

### Release evidence exercises the selected package

The canonical validation manifest covers every discovered test file and 21 smokes. Each test file runs in a fresh bounded process; smokes have explicit environment, timeout, output, and JSON contracts. The selected-package contained-composer smoke builds and verifies a wheel, imports `termdeck`, `pane_stream`, and `resize_signal` only from that extracted site, creates the exact containment graph, and proves:

- the composer redraw completes before stream readiness;
- the collapsed alternate-screen frame is absent from committed replay;
- pane, window, controller, and requested grids are identical;
- no terminal input is sent and no payload is restarted;
- pane, outer/inner bwrap, private payload, and MCP-sibling identities survive;
- the MCP-like sibling receives no signal;
- pidfd-witness signaling is used on capable hosts.

### Compatibility and operational boundary

The managed installer, immutable release tree, bootstrap/launcher authentication, deterministic wheel policy, and retained 0.2.11-0.2.14 rollback generations remain intact. The new containment signal path is Linux/procfs-specific and fails closed elsewhere. Kernels without pidfd support retain the complete userspace identity/topology revalidation fallback.

## 0.2.14

This release supersedes 0.2.13 for deterministic promotion. The installed 0.2.13 runtime remains a verified rollback generation, but a fresh exact-source replay found that the wheel backend inherited the caller's `umask`: the same source and payload bytes produced central-directory file modes `0644` under `umask 022` and `0600` under `umask 077`. Because the wheel hash is part of the manifest and release ID, ambient process state changed the content-addressed production identity.

### Release identity is independent of caller umask

- Both backend builds run in an isolated `-I -B` child. That child alone applies `umask 022`; the controller and concurrent application threads never receive a process-global umask mutation.
- Before the two-build comparison, the release manager parses the bounded single-disk ZIP central directory and canonicalizes every regular member to exact Unix `0644`, every directory member to exact Unix `0755`, creator system to Unix, and internal/low external attributes to zero.
- The normalizer patches only central-directory metadata. It proves member payload hashes, compressed sizes and streams, local-header offsets, CRCs, timestamps, ordering, names, extra fields, comments, extraction requirements, and RECORD content are unchanged before atomically replacing the temporary wheel.
- Empty, multi-disk, ZIP64-sentinel, commented, prefixed, trailing-data, truncated, malformed-signature, inconsistent-count, size, offset, or member-name archives fail closed before publication.

### Strict new policy with historical rollback compatibility

- Verification requires exact canonical metadata for 0.2.14 and later. Unknown future version syntax also receives the strict policy rather than silently opting out.
- Retained real 0.2.11, 0.2.12, and 0.2.13 wheels use ordinary `0644` members and a generated RECORD member at `0664`. That narrow, version-gated legacy exception remains acceptance-only so those already verified generations continue to roll back safely. Every newly built 0.2.14 member is `0644`.
- 0.2.13 is not rewritten or silently relabeled. 0.2.14 receives a new build, manifest, release ID, immutable release tree, and complete install/rollback/reinstall proof.

### Regression boundary

The repair is confined to release construction and archive verification. GTK/VTE rendering, raw tmux control framing, pane streaming, scrolling, selection, clipboard behavior, session restoration, Codex recovery, account switching, per-tab `CODEX_HOME`, SQLite semantics, and persistent tmux processes are unchanged.

## 0.2.13

This release completes the installed-runtime hardening discovered during the 0.2.12 promotion. The terminal byte and tmux framing corrections remain unchanged. The new work makes the content-addressed release tree enforce its own immutability and preserves Python isolation and bytecode suppression across every launch and re-exec boundary.

### Corrected investigation result

The first 0.2.12 GUI started through the managed bootstrap with `sys.dont_write_bytecode` and `PYTHONDONTWRITEBYTECODE=1` already set. Its service log remained empty, and its startup timestamp preceded the bytecode files by several minutes. The GUI did not create them.

An adversarial verifier later inserted the installed `site` directory into `sys.path` and imported it directly under Python without `-B`, bypassing the managed bootstrap. Because the release directories were mode `0755`, that verifier created writable `__pycache__/*.pyc` members. The next stable-launcher invocation correctly rejected the changed file set and writable members. The detection was correct; the missing control was enforced directory immutability.

### Release trees are now actually immutable

- Releases whose build version is 0.2.13 or later freeze every staging-tree directory to exact mode `0555` before verification and publication. Payload files remain read-only and single-link.
- A same-UID process can read and execute the release but cannot add bytecode, cache directories, or arbitrary files. A direct import without `-B` therefore succeeds without mutating the tree.
- Source-side verification and the self-contained managed bootstrap independently walk the complete release tree and reject symlinks, special directories, escapes, and any non-`0555` directory for the new policy.
- The policy is version-gated. Verified 0.2.11 and 0.2.12 releases remain valid rollback targets with their historical directory modes; unknown future version syntax fails closed into the stricter policy.
- A failed verification after freezing thaws and removes only the unique `.staging-<release-id>-…` path owned by that install operation. Cleanup refuses any path outside the release staging namespace and never follows a directory symlink.

### Python launch invariants survive every path

- The stable launcher now invokes the configured interpreter with both `-I` and `-B`.
- The staged-import verifier also uses `-I -B`.
- If managed state changes during bootstrap verification, the bounded `os.execv` retry preserves `-I -B` instead of silently dropping isolation on re-entry.
- The bootstrap continues to set `sys.dont_write_bytecode` and `PYTHONDONTWRITEBYTECODE` before any installed package import, providing defense in depth even though the frozen tree already prevents writes.

### Backward-compatible process and launcher identity

- Managed-GUI discovery recognizes the prior exact `[python, -I, bootstrap]` prefix and the new canonical `[python, -I, -B, bootstrap]` prefix. Each record reports `legacy-isolated` or `isolated-no-bytecode` as a redaction-safe launch profile.
- The scanner still requires canonical interpreter and bootstrap paths, matching `/proc/<pid>/exe`, Linux boot ID, PID start ticks, UID, repeated cmdline stability, and an exact supported GUI tail.
- A launcher or bootstrap template upgrade is accepted only when its state identity is authenticated against executable code: either the current source template or exact bytes derived from the selected immutable release wheel. State-recorded hashes are evidence to verify, never authority by themselves.

### Custom managed installs retain their launcher directory

- Clean-artifact replay under an isolated custom managed root and custom bin directory exposed that runtime `release info` could fall back to `~/.local/bin` even though the generated launcher itself was valid. The root identity was propagated; the custom launcher directory was not.
- Runtime layout selection now has one explicit order: a supplied `bin_dir`, then a non-empty `T1000_MANAGED_BIN_DIR`, then the unchanged `~/.local/bin` default.
- The bootstrap does not trust an inherited value. Only after validating canonical state, the launcher path, launcher bytes, and state stability does it overwrite `T1000_MANAGED_BIN_DIR` with the verified launcher's parent before importing the CLI.
- Custom launchers can therefore run `release info --verify`, process inventory, rollback, and related managed commands without repeating `--bin-dir`; explicit API or CLI arguments still override the runtime default.

### Rollback selects one complete activation bundle

- Real promotion replay proved that changing only `current_release_id` was not a complete cross-version rollback. The selected 0.2.11 application started and reported its version, but its own `release info --verify` rejected the still-newer 0.2.13 bootstrap and launcher hashes.
- A managed generation is now treated as `(release tree, bootstrap bytes, launcher bytes, interpreter, state)`. Rollback derives the target version's deterministic bootstrap and launcher from its verified wheel under `-I -B`, then publishes bootstrap, canonical state, and launcher in one rollback-capable transaction.
- The historical wheel is read through an `openat`/`O_NOFOLLOW` chain rooted at the managed installation. The release and artifacts directories must be real direct children; the manifest and wheel must be read-only, single-link, canonical, and hash/size consistent. The possibly contaminated extracted site is not trusted for recovery.
- Probe bytes are copied into a private `0700` directory, reverified, and imported only from that copy. Stdout, stderr, elapsed time, and the complete process group are bounded while the probe runs. Symlink redirection, malformed or oversized output, inherited-pipe descendants, timeout, and output overflow fail closed.
- Install authenticates an existing activation against the current template or the selected immutable wheel before repairing missing or damaged bootstrap/launcher files. An arbitrary pair of hashes written into state cannot authorize its own replacement.
- Reinstalling a newer release after rollback upgrades the authenticated historical activation back to the current `-I -B` templates. Failure during any bootstrap, state, launcher, or post-publication verification write restores every preimage.

### Regression coverage

- Synthetic 0.2.13 installs prove every directory is `0555`, same-UID file creation raises `PermissionError`, and a direct import without `-B` produces no `__pycache__` or `.pyc` member.
- Directory-mode tamper is rejected for 0.2.13, while a 0.2.12 fixture remains verifiable.
- Failure injected after freezing proves staging cleanup leaves no `.staging-*` path.
- Launcher, staged-import, and state-race re-exec tests require `-I -B`.
- Process tests cover both exact prefixes and retain the existing self-match, substring, UID, executable, boot, PID-reuse, redaction, and incomplete-scan boundaries.

## 0.2.12

This release fixes the corrupted characters that could appear throughout a restored Codex terminal and hardens the complete byte, control-protocol, release, and activation path around that correction. It preserves the existing GTK/VTE/tmux architecture, persistent tasks, terminal interaction behavior, and per-tab Codex account isolation.

### Codex Unicode replay is byte-faithful

- Tmux control-mode command output is represented internally as a reversible Latin-1 byte carrier. Initial pane capture previously treated that carrier as decoded text and encoded it as UTF-8 again, turning valid bytes for glyphs such as `•`, `└`, `│`, `…`, `─`, `»`, `—`, and `’` into mojibake.
- `CommandResult.byte_lines` now exposes the exact immutable command-body bytes. Binary consumers use that API instead of guessing an encoding from the textual protocol carrier.
- Pane history and visible-screen replay are assembled as bytes, preserving UTF-8, CSI/SGR/OSC sequences, backslashes, line boundaries, and arbitrary eight-bit values until VTE receives the stream.
- Live `%output` remains on its existing byte-preserving path. Replay ordering, contrast correction, cursor/mode restoration, scrolling, selection, alternate-screen behavior, and reconnect recovery are unchanged.

### Tmux command bodies cannot impersonate protocol records

- A captured terminal can legitimately contain lines beginning with `%output`, `%message`, `%window-add`, `%begin`, `%end`, or `%error`. These lines were previously at risk of being dispatched as fake notifications or prematurely terminating a command reply.
- Reply framing now accepts only an exact full-line `%begin`, `%end`, or `%error` record followed by three decimal fields. A closing record must match the opening time, command number, and flags.
- While a reply or greeting block is active, every nonmatching line belongs to that block, including notification-looking and malformed frame-looking text. Genuine notifications continue to dispatch outside command blocks.
- Real tmux diagnostics remain in the reply body and are preserved byte-for-byte; the human-readable body is used as the error summary instead of the numeric frame identity.

### Race-aware managed-GUI identity

- `release info` now reports one read-only managed-GUI inventory instead of relying on `pgrep`, process-name matching, or command-line substrings.
- A GUI candidate must begin with the exact configured Python executable, `-I`, and managed bootstrap argv elements; `/proc/<pid>/exe` must resolve to that interpreter; and the remaining arguments must be one of the supported GUI invocation forms. Managed CLI commands and substring lookalikes are excluded.
- Every accepted observation is bound to the current Linux boot ID, PID, process start ticks, real/effective UID, executable, invocation kind, argument count, and SHA-256 of its raw cmdline. Repeated reads reject PID reuse, exec, exit, permission failure, and torn observations.
- Internal empty argv elements are retained, preventing malformed extra arguments from collapsing into the default no-argument GUI form.
- Runtime posture is explicit: `stopped`, `single-gui`, `multiple-gui`, or `scan-incomplete`. Incomplete and multiple-GUI evidence refuses activation, while process posture remains logically separate from immutable release integrity.
- Arbitrary database, workspace, recovery, account, and `CODEX_HOME` values are never returned.

### Real regression coverage

- The Unicode replay smoke creates a disposable real tmux pane, compares authoritative capture with the pane replay, passes the bytes through the real contrast filter and VTE widget, and proves that valid Unicode is visible while common mojibake forms are absent.
- The command-body smoke compares direct tmux capture with `ControlModeClient.byte_lines` for percent-prefixed body text and Unicode, requiring identical SHA-256 values and zero manufactured events.
- The managed-process smoke independently rechecks boot ID, PID/start-time/UID/executable/cmdline-hash identity in real procfs, proves the verifier does not match itself, and rejects private argv leakage.

### Verification

- Full inherited suite: 1,182 passed, 1 skipped, 98 subtests passed.
- Release subsystem: 82 tests covering source snapshot, reproducible build, wheel/manifest/RECORD identity, transactional install, tamper rejection, repair, rollback, release CLI, build status, and diagnostics.
- All 19 strict GTK/VTE/tmux runtime smokes passed with empty stderr and unchanged source, Git, Xvfb, managed-process, and 281-row semantic runtime identities.
- Independent sealed reviews replayed the process scanner over 20,000 generated argv vectors, exact GUI grammar boundaries, race/error cases, redaction and non-mutation contracts, and the complete release-test surface.

## 0.2.11

This release makes restored terminals deterministic at the rendering boundary and makes the application itself reproducibly releasable, atomically installable, independently verifiable, and rollback-capable without sacrificing tmux persistence or per-tab Codex isolation.

### Codex sessions now become visible and interactive deterministically

- A selected tab keeps attachment priority even when GTK selection happens before its delayed pane controller exists.
- A transient setup exception cannot strand a connected blank tab; pre-ready setup retries with bounded backoff, while post-ready failures never replay an already-visible pane.
- VTE reset and tmux replay now cross the GTK boundary as one ordered operation, eliminating the race where a late CONNECTED idle cleared a screen that had already loaded.
- tmux mode flags are replayed with their real truth values. Ordinary shells no longer start with all mouse protocols enabled, so their wheel and scrollbar use VTE-native history; alternate-screen applications still receive fallback arrows or their negotiated mouse protocol.

### Dark terminal content remains readable

- Modern colon-form SGR colours such as `38:2::16:16:16` and `38:5:236` pass through the same 4.5:1 minimum-contrast guard as semicolon-form RGB/indexed colours.
- Underline styles, explicit backgrounds, reverse video, split escape sequences, and unknown colour spaces retain their original semantics; only supported low-contrast foreground ink is rewritten.
- A real VTE pixel smoke now proves the formerly invisible truecolour, faint, and indexed-grey cases are readable on the default `#0a0d13` surface.

### Bounded transport and stream ownership

- Every tmux control transport now closes its write side, terminates with a bounded grace period, uses a kill fallback when needed, closes all streams, reaps the child, and joins its reader without self-join races.
- Setup owns at most one retry timer per stream generation. Pre-ready input/output and GTK delivery queues have byte and item caps; overflow reconnects only the disposable control path and replays the authoritative persistent pane.
- Scheduler failures clear their latch instead of stranding output, rejected input is reported once, and disposable smokes destroy stream controllers before terminating isolated tmux sessions.
- Auth recreation uses the complete prior session/pane/process identity, avoiding false same-second rejection and PID-reuse ambiguity while preserving private `CODEX_HOME` scope.

### Total, semantics-preserving terminal colour filtering

- Complete and partial escape sequences, decimal parameters, and retained grammar state are explicitly bounded. Arbitrary byte streams terminate without parser exceptions and produce the same result under arbitrary chunking.
- RGB and indexed values outside `0..255` remain byte-identical VTE no-ops instead of corrupting tracked colour state. Structurally unknowable SGR taints inference until a full reset; unknown colour spaces remain fail-open.
- Background colours, reverse video, C1 bytes, underline styles, and unrelated SGR attributes retain their emulator semantics.
- Contrast lifting now tests both black and white endpoints, closing the saturated-background case where white was unreachable but black satisfied the body-text floor.

### Reproducible, content-addressed production releases

- `t1000 release build` snapshots a clean Git tree, exports it without following links, injects a canonical build record, builds twice offline with the commit timestamp as `SOURCE_DATE_EPOCH`, and rejects unequal wheels.
- Verification requires the exact distribution/version filename and dist-info directory, one pure-Python `py3-none-any` tag, one `t1000 = termdeck.cli:main` entry point, matching package/metadata/build versions, exact RECORD coverage and hashes, safe paths, and bounded archive sizes.
- Managed installation publishes a read-only, single-link, content-addressed release tree and atomically binds it to the exact interpreter, self-verifying bootstrap, stable launcher, canonical state, manifest, and artifact. Any write or postcondition failure restores every preimage.
- Managed and legacy rollback use the same transaction model. The bootstrap verifies all identities, modes, paths, hashes, RECORD members, and the selected state before importing TermDeck; a concurrent state change triggers a bounded re-exec rather than mixing releases.
- Existing `doctor`, diagnostics, desktop integration, shutdown persistence, tmux task ownership, and Codex auth boundaries are extended rather than replaced.

### Verification

- Full inherited suite: 1,153 passed, 1 skipped, 98 subtests passed.
- All 16 strict runtime smokes passed with empty stderr and unchanged Xvfb, reader-thread, candidate-file, and semantic runtime identities.
- Deterministic parser checks covered 100,000 arbitrary-byte streams, 20,000 reference-model streams, mutation boundaries, and real VTE pixels.
- The isolated release workflow proved double-build reproducibility, outside-checkout invocation, doctor/status identity, member/bootstrap/launcher tamper rejection before import, repair, rollback, and unchanged 281-row persistent semantic state.

## 0.2.10

TermDeck is leaner on screen and much harder to stall. The rendering stack remains GTK/VTE; this release removes avoidable synchronous Python-side work instead of replacing the native terminal engine.

### Responsive interaction

- Clipboard paste requests are asynchronous, so a slow or unavailable clipboard owner cannot block GTK input.
- Clipboard payloads use VTE's bracketed-paste-aware path, so the paste shortcut does not synthesize Enter.
- Otherwise-unhandled keys pressed on window chrome return focus and deliver that first key directly to the active terminal; text-entry controls retain focus.
- Every tmux subprocess has a deadline.
- Startup reconciliation and restore discover all sessions and pane PIDs in bounded batches instead of issuing per-tab queries.
- If an all-pane query times out, TermDeck uses recent recorded PIDs and does not fan out into more blocking calls.
- The optional Tabs pane performs no database, tmux, process-sampling, or widget-rebuild work while hidden.
- A single click on a Tabs-pane card opens and focuses that tab; `Ctrl+left-click` remains reserved for bulk selection.
- Right-clicking any Tab Manager row exposes **Move to window** with every other open TermDeck window plus **New window…**. A new destination receives only the moved tab, without an extra shell. Moving a visible tab reparents its existing VTE widget and tmux client; hidden rows never recreate a missing session.
- Tab Manager now separates current **Tabs** from persistent **Killed history**. **Remove** is enabled only for unpinned killed tabs, and **Focus** navigates to the owning window without moving or restarting the tab.
- The file-backed session registry uses SQLite WAL/NORMAL mode and waits at most one second for a competing writer.
- Terminal clients use `start_new_session=True`, avoiding Python work between fork and exec.

### Scrollback that works through tmux

- The persistent Terminator-style vertical scrollbar uses its own tmux-history adjustment, so GTK always renders its opaque trough and thumb even though VTE's full-screen alternate buffer reports no scroll range.
- Wheel and scrollbar-drag events follow the history owner behind each real tmux attachment; large Codex scrollbar jumps use page navigation rather than flooding its composer with arrow keys.
- The pager caret is hidden while history is moving, so scrolling cannot leave a cursor roaming through transcript content; live input restores the composer cursor first.
- Ordinary shell panes scroll through tmux copy history; a foreground Codex pane opens and scrolls its built-in transcript before any navigation key is sent.
- Codex's composer never receives wheel-generated Up/Down input, so prompt history does not move.
- The first typed key, clipboard paste, or normal terminal click closes the historical view before input reaches the live application.
- tmux mouse capture stays off, preserving ordinary VTE pointer selection instead of turning clicks into tmux mouse commands.
- An ordinary pointer drag selects and auto-copies terminal text to CLIPBOARD and PRIMARY while VTE retains the visible selection highlight.
- VTE, the attached PTY client, and tmux now converge on the same grid after window or Tabs-pane changes, preventing right-edge text truncation; a hidden pane relinquishes its full allocation.
- The Tabs pane measures its scaled controls and reflows after GTK applies CSS, keeping both left and right placements inside the window at every supported UI scale.
- `Shift` keeps a local click from returning to the live viewport while history is being reviewed.
- Active output does not snap the view to the bottom while history is being reviewed.

### Default 2,000-session control plane

- Startup recreates every remembered window while attaching no more than 24 terminal widgets. It seeds one tab per prioritized window across up to 24 windows before filling additional tabs, with at most 12 tabs per window. Deferred tmux sessions stay alive and openable.
- UI refreshes rotate through at most 128 process trees; pending rows remain live and display `sampling` rather than being misclassified as missing.
- The Tabs pane and Tab Manager materialise at most 100 cards and 500 rows respectively, while filtering still considers the registry.
- PID caches are capped at 4,096 entries, capture requests at 10,000 lines, and tmux pane history at 2,000 lines.
- Large tmux snapshots receive adaptive bounded deadlines. Failures back off, retain stale state, and do not become thousands of per-session retries.
- Reconciliation writes all changed runtime states in one transaction. No timeout, cache pressure, or health classification pauses or stops a session automatically.

Frankenterm informed the passive, bounded, stale-not-dead control-plane design. TermDeck remains a GTK/VTE/tmux application; Frankenterm's tiered transcript storage and delta indexing are candidates for an optional future history service, not a runtime dependency.

### Leaner terminal-first UI

- Tab labels reserve readable natural width and cap long names at 20 characters; short names no longer collapse to a row of ellipses.
- Default directory-derived tab names follow the live tmux pane cwd; explicit renames remain fixed overrides.
- The New-tab action is a compact accent control rather than an oversized glowing circle.
- The HeaderBar and notebook frame use less padding, leaving more room for the terminal.
- Persistent controls and surfaces no longer use broad shadows or CSS transitions.
- New-tab, More, and close-tab controls include shortcut-aware tooltips and accessible names.

### Why no Rust or C rewrite

VTE already handles terminal parsing and drawing in native code, while GTK owns the native widget stack. The observed stalls were synchronous clipboard, tmux, process-sampling, and widget-rebuild paths on GTK's main thread. Removing those paths delivers the useful performance gain without adding a second implementation or toolchain. A native helper remains an option only if profiling later identifies CPU-heavy Python logic outside VTE.

### Verification

- Full test suite: 435 passed, 23 subtests passed.
- Synthetic fleet test: 2,000 sessions with a 128-process refresh budget and one batched reconciliation write.
- Real GTK/tmux interaction checks cover Tab Manager right-click relocation into a new window with no extra shell and unchanged VTE/client/pane identity, five-window startup seeding, exact ordinary-drag select-to-copy with a retained highlight, the rendered scrollbar, live-viewport clicks, and exact first-key delivery after header chrome owns focus.
- The visual smoke renders an `18x710` scrollbar with a usable `0..2000` range. The interaction smoke advances shell history by three wheel lines and another 30 lines by dragging the bar; its Codex fixture records transcript open, three transcript-up events, transcript close, and live composer input with zero composer-history movement.
- Real GTK/tmux lifecycle smokes passed, including maximum-UI-scale sidebar bounds and VTE/PTY/tmux resize convergence.
- Verification-run first attachment: 82.74 ms; immediate Ctrl+T handler: 23.38 ms.
- Desktop render: `1290x846`, zero layout errors.
- Compact render: requested and captured `640x520`, zero layout errors.

## 0.2.9

New tabs now open immediately. TermDeck no longer asks you to select a directory before creating a shell.

### Immediate New tab

- `Ctrl+T` opens a shell without displaying any dialog.
- The header New tab button, command palette, Tabs pane empty state and secondary New tab actions use the same direct path.
- Ctrl+T is handled before VTE, preventing the terminal from consuming the browser-style shortcut.
- The compatibility method `new_directory_tab()` remains internally available but now delegates to the non-modal New tab action.

### Starting directory

TermDeck chooses the initial directory in this order:

1. The active tmux pane's live `#{pane_current_path}`, including a path reached by typing `cd` in the shell.
2. The active tab's stored starting directory if tmux cannot report a live path.
3. The user's home directory when no tab is active.

The tab's initial name remains the basename of that starting directory. You can then use `cd` manually inside the terminal, as in any ordinary shell.

### Window creation

- First launch asks only for the TermDeck window name, then opens the first shell in the user's home directory.
- **New TermDeck window** asks only for the new window name, then opens its first shell in the current window's active live directory; home is the fallback.
- There is no folder-selection popup in either flow.

### Verification

- Full test suite: 347 passed, 15 subtests passed.
- A real GTK/tmux smoke changed the active shell from `starting` to `live-work` with `cd`, pressed Ctrl+T, and verified the new tab directory and name were both `live-work`.
- The same smoke confirmed zero chooser windows and a running tmux session for the new tab.
- Desktop render: `1290x858`, zero application or layout errors.
- Compact render: requested and captured `640x520`, zero application or layout errors.

## 0.2.8

`Ctrl+Shift+V` now always pastes the system clipboard into the active terminal.

### Guaranteed clipboard behavior

- The shortcut is handled explicitly before configurable or imported keybindings.
- It reads `Gdk.SELECTION_CLIPBOARD`, not the primary-selection buffer.
- Imported Terminator paste bindings can no longer replace `Ctrl+Shift+V` or cause the shortcut report to advertise a different primary paste key.
- `Ctrl+V`, `Shift+Insert`, and any additional configured paste binding continue to work as secondary paths.

### Verification

- Unit coverage verifies chord recognition even when the keymap reports no paste match.
- Handler coverage verifies the active terminal receives exactly one clipboard-paste call.
- A real Xvfb/GTK smoke sets the system clipboard to `termdeck-clipboard-paste-marker`, triggers `Ctrl+Shift+V`, and verifies that exact text is fed once into the active VTE.
- The clipboard smoke runs in GitHub Actions before visual rendering.
- Full test suite: 341 passed, 15 subtests passed.
- Desktop GTK render: `1290x858`, zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, zero application or layout errors.

## 0.2.7

TermDeck now has a cohesive visual identity rather than relying on lightly styled GTK defaults. The product model remains unchanged: named windows, directory-backed tabs and the terminal-first layout.

### Layered visual system

- Refined the default dark palette to a neutral midnight base with a softer violet-blue accent, cleaner muted text and less saturated borders.
- The terminal notebook is now an inset, rounded surface with its own shadow and breathing room from the window edges.
- Header, terminal frame, optional Tabs pane and auxiliary windows use distinct elevation levels instead of sharing nearly identical flat backgrounds.
- Comfortable mode uses a `10px / 12px / 12px / 12px` notebook inset; compact mode reduces it to `6px / 8px / 8px / 8px`.

### Tabs and primary action

- Tabs are rounded, separated controls rather than connected legacy notebook tabs.
- Inactive tabs stay quiet; hover introduces a subtle surface and border.
- The active tab uses an accent-tinted fill, focused border, bottom accent rail and soft shadow.
- Close controls remain present but visually recede until the tab is active or the pointer hovers them.
- New tab is now a circular accented action with a matching glow while the More action remains restrained.

### Unified controls and auxiliary views

- Plain GTK buttons now share consistent radius, border, depth, hover, pressed and keyboard-focus states.
- Menus have roomier rows and separators; scrolled areas and text previews use the same sunken surface treatment.
- Tab Manager and Window Overview explanatory text uses a quieter section-description style.
- The optional Tabs pane and its cards use increased radius, cleaner borders and more deliberate elevation.

### Verification

- Full test suite: 338 passed, 15 subtests passed.
- Real visual-polish smoke verified semantic surface classes, computed notebook margins, one circular primary action and the required active-tab/focus selectors.
- All prior GTK smokes remained green: startup geometry, Ctrl+W/Ctrl+Q preservation, Tabs-pane lifecycle, header, tab strip and typo-tolerant Search Tabs.
- Compact render: requested and captured `640x520`, zero application or layout errors.
- Desktop render: `1290x858`, zero application or layout errors.
- Compared with the verified 0.2.6 compact frame, 107,290 of 332,800 pixels changed (`32.24%`), confirming a material visual redesign rather than a token-only adjustment.

## 0.2.6

This release fixes the startup rendering artifact shown as dotted tmux padding and defines clear close-versus-kill behavior.

### Startup rendering fix

- TermDeck no longer attaches the tmux client at the PTY default `80x24` before GTK finishes sizing the terminal.
- VTE allocation is debounced briefly, then the PTY and tmux client are created using the final terminal row/column grid.
- Later resize allocations continue synchronising the PTY geometry.
- The terminal container, notebook, paned layout and root overlay explicitly expand across the full client area.
- A real Xvfb/tmux smoke maximises the window and verifies that the first tmux client grid and tmux window grid exactly match the VTE grid, with no initial `80x24` mismatch.

### Close, quit and kill

- `Ctrl+W` closes the active tab in the UI and marks it hidden, but leaves its tmux session and process running.
- `Ctrl+Shift+T` reopens the most recently closed tab.
- `Ctrl+Q` closes all TermDeck windows and exits the application while leaving all tmux sessions running and preserving normal restore visibility.
- `Ctrl+Shift+W` remains the destructive action that kills the active session.
- The tab `×` control follows the same non-destructive close rule, including for pinned tabs.
- **More → Quit TermDeck** provides the same session-preserving quit operation.

### Management views

- **Tab Manager · per-tab details** shows one row per tab/process with PID, state, health, CPU/RAM, command, directory, captured output and control actions.
- **Window Overview · window totals** shows one read-only aggregate row per named window with tab counts, resource load and health.

### Runtime verification

- First attachment: VTE, tmux client and tmux window all used the same final grid.
- The main paned surface covered the complete GTK client allocation.
- Real `Ctrl+W` closed one tab while its tmux session remained alive.
- Real `Ctrl+Q` closed every TermDeck window while all three test sessions remained alive.
- Full test suite: 337 passed, 15 subtests passed.
- Desktop GTK render: `1290x856`, zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, zero application or layout errors.

## 0.2.5

Search Tabs now tolerates ordinary typing mistakes while remaining predictable and local.

### Typo-tolerant matching

- `pytes` finds `pytest`.
- Adjacent transpositions such as `pyetst` also find `pytest`.
- Insertions, deletions, substitutions, and adjacent transpositions are supported through bounded Damerau-Levenshtein distance.
- Query terms shorter than four characters remain literal, so searches such as `api` do not fuzzily match unrelated `app` tabs.
- One edit is allowed for ordinary words; long terms may use two edits.
- Every query term must still match, preserving the existing multi-word AND behavior.
- Literal matches rank ahead of fuzzy alternatives.

### Recent terminal output

- **Include recent output** applies the same typo tolerance to the latest 120 captured terminal lines.
- Output is redacted before matching and before snippets are displayed.
- For example, `compilaton` can find a recent line containing `compilation failed`.

### Verified

- Unit coverage includes metadata typos, transposition, multi-term matching, literal-result priority, short-query noise suppression, and fuzzy recent-output snippets.
- A real GTK Search Tabs smoke verifies `pytes`, `pyetst`, `api`, and `compilaton` through the actual window model.
- The fuzzy-search smoke runs in GitHub Actions before desktop and compact rendering.
- Full test suite: 336 passed, 15 subtests passed.
- Desktop GTK render: `1290x856`, zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, zero application or layout errors.

## 0.2.4

TermDeck's main tab strip now follows the same reduction as its header: **tab name, close control, nothing else**.

### Minimal tab labels

- Each notebook tab displays exactly its stored name.
- Directory-backed defaults therefore remain plain names such as `client-api`.
- Explicitly renamed tabs display the new name without an added prefix or icon.
- Long names use end ellipsis and a 24-character visual cap; the underlying stored name is unchanged.

### Removed visual decoration

- Removed terminal/task category glyphs from the main tab strip.
- Removed the pin emoji from visible tab titles.
- Removed live health and attention dots from visible tab titles.
- These states remain available where they are actionable: pin/unpin in the tab context menu and tooltips, and health/resource information in the optional Tabs pane and Tab Manager.

### Runtime verification

- Added a real GTK tab-strip smoke using a pinned `client-api` tab.
- Verified that its visible label remains exactly `client-api`.
- Renamed the same tab to `api-renamed` and verified the visible label updates exactly.
- Verified one label, one `×` close control, ellipsis behavior, pinned tooltip guidance, and no visible semantic decoration.
- Wired the smoke into GitHub Actions before desktop and compact rendering.

### Verified

- Full test suite: 330 passed, 11 subtests passed.
- Minimal tab-strip smoke: passed.
- Desktop GTK render: `1290x856`, zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, zero application or layout errors.

## 0.2.3

TermDeck's default chrome now matches the product model literally: **one named window, terminal tabs, New tab, and one More menu**.

### Reduced window header

- The in-app title shows only the user-defined window name, such as `Backend`.
- Removed the decorative green status dot and the `Directory tabs` subtitle.
- Removed the repeated `TermDeck ·` prefix from the in-app label.
- The operating-system/window-manager title still uses `TermDeck · Backend` for identification outside the app.
- New tab remains the only directly visible action.
- More is the only secondary-action entry point.

### Consolidated discovery

- Search tabs moved into More.
- Command palette moved into More.
- `Ctrl+Shift+P` still opens the command palette directly.
- Search, Tab manager, Window overview, shortcuts, appearance, reopen, pane visibility, window rename, and new-window creation remain available without crowding the header.

### Runtime hardening

- Fixed initial native-title assignment by setting it after the custom GTK HeaderBar is installed.
- Added a dedicated Xvfb header smoke check that requires exactly `New tab` and `More actions` as accessible clickable actions.
- The header smoke also verifies the complete ordered More menu and distinct in-app/native titles.

### Verified

- Full test suite: 330 passed, 11 subtests passed.
- Reduced-header and Tabs-pane lifecycle smoke: passed.
- Desktop GTK render: `1290x856`, zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, zero application or layout errors.

## 0.2.2

TermDeck now opens as the simpler application model requested: **a named window with terminal tabs, without a duplicate control pane occupying the default layout**.

### Terminal-first main window

- Fresh profiles and default settings show only the native tab strip and active terminal.
- The advanced Tabs pane remains available for bulk selection, prompting, resource controls, and lifecycle actions.
- Toggle it with `F9`, **More → Show/hide tabs pane**, or the command palette.
- Visibility persists across launches, synchronises across all open windows, and is inherited by newly created windows.

### Preference migration

- Added canonical `appearance.tabs_pane_visible`, which defaults to `false`.
- Existing `appearance.focus_mode: true` is interpreted as a hidden Tabs pane.
- Existing `appearance.focus_mode: false` is interpreted as a visible Tabs pane.
- The next pane toggle saves only the canonical key and removes legacy focus keys.
- Removed the duplicate command-palette focus action and the obsolete focus-mode CSS hook.

### Runtime hardening

- Removed redundant application-level `show_all()` calls that could remap a pane after the window constructor had hidden it.
- Added a real GTK smoke script that verifies the default state, actual `F9` handling, persistence, new-window inheritance, and cross-window synchronisation.
- Wired that smoke script into the GitHub Actions UI job before desktop and compact rendering.

### Verified

- Full test suite: 330 passed, 11 subtests passed.
- Real Tabs-pane lifecycle smoke: all six assertions passed.
- Desktop GTK render: `1290x856`, with zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, with zero application or layout errors.

## 0.2.1

TermDeck now starts with the same model it presents everywhere else: **create a named window, then choose its first directory-backed tab**.

### First launch

- A fresh profile no longer opens a generic `Main` window.
- TermDeck asks for the window name first, with examples such as Client work, Backend, and Research.
- After the name is confirmed, TermDeck opens the directory chooser for the first tab.
- The chosen directory still supplies the default tab title through its basename.
- Cancelling the naming dialog creates no window and starts no process.
- Cancelling only the directory chooser leaves the named window open with its empty-state New tab action.

### One creation path

- First launch and **More → New TermDeck window** now use the same application-level creation routine.
- The old `open_initial_shell` constructor option was deleted.
- No GTK window constructor silently opens the process working directory anymore.
- Saved windows and saved layouts continue restoring without an interactive directory prompt.

### Verified

- Full test suite: 325 passed, 11 subtests passed.
- Real Xvfb first-run flow: entered `Client work`, selected a temporary `client-api` directory, and verified both the persisted window title and basename-derived running tab.
- Existing saved-layout visual smoke remains independent of first-run onboarding.

## 0.2.0

TermDeck now has one clear product model: **a named window groups directory-backed terminal tabs**.

### Named windows

- Users can rename the current TermDeck window from **More → Rename window**.
- The window name is persisted across every tab in that logical window without changing tab order or last-opened timestamps.
- **More → New TermDeck window** asks for the group name, creates the window, and then asks for its first tab directory.
- The native window title and TermDeck header update immediately after a rename.

### Directory-backed tabs

- **New tab** and `Ctrl+T` now open a folder chooser instead of a task launcher.
- TermDeck opens the user's default shell in the selected directory.
- The default tab title is the directory's final component. For example, `/home/user/work/client-api` becomes `client-api`, never the absolute path.
- Tabs inside one window can use unrelated directories; the window name supplies the grouping context.
- Explicitly renamed tabs and names stored in existing saved layouts remain unchanged.

### Removed concepts

- Removed the GTK task launcher and editable task-template catalogue.
- Removed task-template Python modules and the `termdeck templates` command.
- Removed the custom-task creation path and its shortcuts.
- Removed project detection from the GUI, command palette, quickstart report, CLI, and package. The `project-detect` and `project-init` commands no longer exist.
- Simplified `termdeck new`: `--tag` was removed, its JSON result now uses a `tabs` array, and normal creation output omits internal tag and tmux identifiers.
- Removed tag chips, Retag menu items, and visible Tag columns from the primary GUI.
- Search and Tab Manager now present tabs by window, name, directory, command, state, and resource information.

### Compatibility

- Existing SQLite data remains readable; internal session and tag fields are retained so upgrades do not discard remembered processes or metadata.
- Existing visible tabs, hidden tabs, snapshots, saved window/tab recipes, Terminator-layout imports, and adopted tmux processes continue to restore.
- JSON workspace commands remain as compatibility and automation tools for serialising windows and tabs; they are no longer presented as a separate GUI concept.
- Advanced process-management CLI commands remain available for scripts and migration workflows.

### Verified

- Full test suite: 319 passed, 11 subtests passed.
- Desktop GTK render: `1290x856`, with zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, with zero application or layout errors.
- Task-launcher, template, and project-detector symbols are absent from executable package code.
- GitHub repository visibility: private.

## 0.1.32

This release aligns Sessions-pane multi-selection with standard desktop interaction conventions.

### Multi-selection

- Ctrl+left-click now toggles a session from anywhere on its card, including the checkbox, tag, Rename button, and Open button.
- Existing selected sessions remain selected while another card is added, enabling natural multi-selection.
- Ctrl+left-click on a selected card removes it from the selection.

### Context menus

- Right-click now opens the session context menu without silently changing the active selection.
- Holding Ctrl while right-clicking has no selection side effect.
- Bulk-aware context actions still apply when the right-clicked session is already part of the current selection.

### Verified

- Full test suite: 323 passed, 11 subtests passed.
- Desktop GTK render: `1290x856`, with zero application or layout errors.
- Compact GTK render: requested and captured `640x520`, with zero application or layout errors.
- GitHub repository visibility: private.

## 0.1.31

This release makes the redesigned interface genuinely responsive rather than merely desktop-sized.

### Compact titlebar

- Converted all titlebar actions to compact icon controls while retaining clear tooltips and accessibility names.
- Bounded and ellipsised the workspace title and subtitle so long names cannot dictate the application minimum width.
- Preserved the existing action hierarchy: New remains the accent action, while Shell, Search, Palette, and More remain visually secondary.

### Flexible workspace split

- Enabled shrinking for both children of the horizontal `Gtk.Paned` layout. The Sessions pane still opens at its preferred desktop width, but it no longer prevents the terminal workspace from fitting a compact window.
- The same behavior applies whether the Sessions pane is placed on the left or right.

### Enforced responsive verification

- Added `--window-width` and `--window-height` to `scripts/render_ui_smoke.py`.
- The harness now reports requested and actual frame sizes and fails when GTK or the window manager clamps the requested viewport.
- CI now renders and uploads both the normal desktop frame and a compact `640x520` frame.

### Verified

- Desktop render: `1290x856`, with zero application or layout errors.
- Compact render: requested and captured `640x520`, with zero application or layout errors.
- Full test suite: 321 passed, 11 subtests passed.
- GitHub repository visibility: private.

## 0.1.30

This build validates the modern UI through the real GTK runtime and fixes two defects that source-level tests did not expose.

### Runtime fixes

- Replaced unsupported PyGObject `set_data` / `get_data` calls in Sessions-card selection styling with normal Python widget attributes. The previous path crashed when active session cards were rendered.
- Stopped forwarding argparse-consumed `--db` and `--workspace` options into `Gtk.Application`, which previously made the real `termdeck gui` entry point exit with `Unknown option --db`.

### Visual verification

- Added `scripts/render_ui_smoke.py`, which launches the public GUI command inside isolated Xvfb, D-Bus, HOME, SQLite, and tmux environments.
- The harness captures the mapped TermDeck window, reports its dimensions and image size, separates application errors from expected headless portal warnings, and cleans up every temporary process.
- Added a dedicated GitHub Actions `ui-smoke` job that provisions system GTK/VTE bindings, runs the harness, and uploads the PNG frame for review.
- Split CI into Python 3.10–3.12 compatibility checks and a system-Python GTK test job, ensuring PyGObject-dependent tests run with the bindings they require.
- Updated the workflow to `actions/checkout@v7`, `actions/setup-python@v6`, and `actions/upload-artifact@v7`.
- Verified a nonblank `1290x856` PNG frame from the real CLI path with zero TermDeck application error lines.

### Verified

- Full unit test suite: 318 passed, 11 subtests passed.
- Python syntax compilation for `termdeck`, `tests`, and `scripts`.
- Real-window visual smoke render through `termdeck gui --db ... --workspace ...`.
- GitHub repository visibility: private.

## 0.1.29

This release gives TermDeck a cohesive modern visual system while preserving its existing terminal, session, and safety behaviour.

### Visual and interaction redesign

- Replaced the flat chrome with layered dark, noir, and system surfaces, softer borders, modern rounded geometry, restrained shadows, and consistent hover/focus states.
- Reduced header clutter to the most frequent actions and moved secondary workspace, window, shortcut, and appearance controls into a single overflow menu.
- Redesigned the Sessions pane with clearer hierarchy, active/selected counts, stronger search and prompt surfaces, semantic Send/Pause/Resume/Kill actions, and actionable empty-state guidance.
- Added persistent visual emphasis for selected sessions and refined cards, badges, tabs, close buttons, tables, menus, dialogs, tooltips, scrollbars, and toasts.
- Split the generated GTK stylesheet into a dedicated chrome module to keep future visual changes isolated and testable.

### Verified

- Full unit test suite: 316 passed, 11 subtests passed.
- GTK CSS parser accepted dark, noir, and system themes.
- Python syntax compilation for `termdeck` and `tests`.
- GitHub repository visibility: private.

## 0.1.28

This build fixes two direct interaction failures in the Sessions pane and detected-project launcher.

### Added or stabilised

- Ctrl-right-click now adds a session to the current multi-selection from the card body, checkbox, tag, Rename button, or Open button before showing context actions.
- Existing selected sessions remain selected, allowing ergonomic multi-session context actions.
- GTK project-pane creation now materialises `{shell}` and other supported placeholders at launch time.
- Detected project shells no longer execute the literal `{shell}` token or produce a dead tmux pane with exit status 127.

### Verified

- Focused pointer-selection and project-pane runtime regressions.
- Full unit test suite: 312 passed, 11 subtests passed.
- Python syntax compilation for `termdeck` and `tests`.
- GitHub repository visibility: private.

## 0.1.27

This build fixes clipboard ownership, session lifecycle, auxiliary-window closing, and responsiveness at their integration boundaries.

### Added or stabilised

- Installed durable GTK clipboard text after VTE updates its internal selections.
- Reused one cached tmux all-pane snapshot for repeated single-session PID lookups.
- Aligned tmux and process-resource cache lifetimes with the configured UI refresh interval.
- Reduced unchanged Sessions-pane resource rebuilds while preserving periodic health updates.
- Removed killed session tabs from every open TermDeck window and cleared stale selections.
- Explicitly enabled window-manager close controls for auxiliary windows.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Full unit test suite: 309 passed, 11 subtests passed.
- GitHub repository visibility: private.

## 0.1.26

This build focuses on browser-like tab ergonomics and verified UI responsiveness. Main tabs now have a full right-click action menu, pinned tabs have an explicit close path, Sessions row menus understand multi-selection, and expensive refresh/capture paths have behavioural regression tests rather than only static guards.

### Added or stabilised

- Added a main-tab context menu with rename, retag, pin/unpin, hide, kill, Terminal options, Master tasks, hide-other-tabs, and hide-tabs-to-the-right actions.
- Added explicit pinned-tab close guidance and **Unpin and hide tab** so pinned tabs no longer look uncloseable.
- Made Sessions row right-click actions bulk-aware when Ctrl-click selection is active.
- Debounced heavy search/filter refresh callbacks to avoid per-keystroke row rebuilds.
- Added short-lived Master captured-output preview caching to reduce repeated tmux capture work during selection and refresh events.
- Added behavioural tests for debounce coalescing, Master preview caching, and pinned-tab close handling.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Focused UI regression tests, debounce tests, Master preview cache tests, and pinned-tab close behaviour tests.
- Full unit test suite in the actual repository checkout.

## 0.1.25

This build makes refresh timing configurable. Users on slower machines can now reduce background refresh churn without changing source code, while the default behaviour remains the same.

### Added or stabilised

- Added `appearance.refresh_interval_seconds` with aliases `ui_refresh_interval_seconds` and `refresh_seconds`.
- Clamped configured refresh intervals to 1–30 seconds.
- Applied the setting to the side pane, master task window, and window overview timers.
- Reused the same interval for the side-pane resource refresh bucket, keeping CPU/RAM/health updates periodic but configurable.
- Added settings and static UI coverage for configurable refresh timing.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted settings and UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.24

This build tightens the responsiveness work by cleaning up refresh timers. Helper windows and side-pane widgets now mark their refresh timers dead on destroy, allowing GLib to stop calling those timers after the UI surface has closed.

### Added or stabilised

- Added `_refresh_alive` flags to overview, master, and side-pane refresh surfaces.
- Wired destroy handlers that set `_refresh_alive` to `False`.
- Updated refresh timer callbacks to return `False` after teardown so GLib self-cancels them.
- Added static regression coverage for timer cleanup behaviour.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.23

This build completes the side-pane refresh fix by avoiding stale resource badges. The unchanged-signature fast path still prevents redundant row rebuilds, but the signature now includes a timed refresh bucket so CPU, RAM, and health cues are refreshed periodically even when session metadata has not changed.

### Added or stabilised

- Added a side-pane `_refresh_bucket_seconds` value for timed dynamic refreshes.
- Included a `time.monotonic()` bucket in the side-pane refresh signature.
- Kept the previous blank-pane fix: rows are cleared only after the cache check decides a rebuild is needed.
- Extended static tests to guard against both blanking and stale-resource regressions.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted UI static tests.
- Unit test suite in the actual repository checkout.

## 0.1.22

This build fixes a regression introduced by the responsiveness cache. The Sessions pane now checks its refresh signature before clearing/rebuilding widgets; if nothing changed, it leaves the existing visible rows in place instead of briefly or permanently blanking the pane.

### Added or stabilised

- Moved side-pane widget clearing after the unchanged-signature fast path.
- Preserved stopped-session filtering and selected-id pruning while avoiding unnecessary row rebuilds.
- Added a static regression test proving the cache check happens before `session_box.remove(...)`.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.21

This build continues the responsiveness work by caching resource snapshots. After tmux pane metadata is cached, the next expensive repeated operation is `/proc` sampling: walking child process trees, reading process stat data, and summing RSS. TermDeck now reuses a short-lived per-PID resource snapshot across adjacent UI refreshes.

### Added or stabilised

- Added `ResourceSampler(..., snapshot_cache_ttl=...)` with a default short-lived snapshot cache.
- Cached per-PID resource snapshots so side pane, master task view, and overview refreshes can reuse recent process-tree/RSS samples.
- Added `ResourceSampler.invalidate_cache()` for full or per-PID cache invalidation.
- Added tests proving snapshot reuse and explicit invalidation resampling.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Targeted resource sampler tests.
- Unit test suite in the actual repository checkout.

## 0.1.20

This build targets responsiveness. The hidden tmux backend now caches the expensive `list-panes -a` pane/PID snapshot for a short interval, allowing the side pane, master task view, and window overview to share one tmux query instead of spawning repeated tmux processes during adjacent refreshes.

### Added or stabilised

- Added `TmuxBackend(..., pane_cache_ttl=...)` with a default one-second pane metadata cache.
- Reused cached pane maps across `pane_pids()` calls while keeping direct `pane_pid()` behaviour unchanged for explicit process controls.
- Invalidated cached pane metadata after session creation, rename, and termination.
- Added tests for cache reuse and explicit invalidation refresh.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.19

This build fixes the most visible interaction papercuts: selected terminal text now goes to the real system clipboard for pasting outside TermDeck, cursor blinking can be disabled, the Sessions pane can move left/right, killed tabs disappear from the Sessions pane, Ctrl-clicking tags multi-selects sessions, helper windows have explicit Close/Esc exits, and periodic UI refresh work is reduced.

### Added or stabilised

- System clipboard writes now use `Gtk.Clipboard.get_for_display` first, with PRIMARY and VTE copy fallbacks retained.
- Added terminal right-click menu actions for copy, paste, paste primary, select all, and clear selection.
- Added `appearance.terminal.cursor_blink` and a Terminal options checkbox to stop cursor blinking.
- Added persistent `appearance.side_pane_position` plus right-click Sessions header actions to move the pane left/right.
- Sessions pane filters out stopped/killed sessions and drops them from current multi-selection.
- Ctrl-click on a session tag toggles that session in the multi-selection.
- Master, search, and overview helper windows now expose explicit Close buttons and Esc close behaviour.
- Side-pane refresh now skips unchanged rebuilds and background timers run less aggressively.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.18

This build strengthens terminal select-to-copy behaviour. Text selected with the cursor is copied into the system clipboard and PRIMARY selection so it can be pasted into other applications immediately, while Ctrl+Shift+C remains available as an explicit fallback.

### Added or stabilised

- Debounced VTE selection-change handling so double-click, triple-click, keyboard selection, and drag selection all synchronise selected text after VTE settles its selection buffer.
- Immediate left-button-release copying remains in place for browser-like mouse selection.
- System clipboard and PRIMARY selection are both updated, with VTE clipboard APIs retained as a fallback for different GTK/VTE backends.
- Static GUI tests now cover the delayed select-to-copy path and cleanup of scheduled clipboard syncs.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- Unit test suite in the actual repository checkout.

## 0.1.17

This build completes the next visible browser-usability step: global remembered-tab search now has a GTK surface. The feature was already available headlessly in `termdeck search`; it is now reachable from the header and the command palette so users can find terminal tasks without remembering commands.

### Added or stabilised

- Added a GTK `SessionSearchWindow` for remembered terminal tabs.
- Added header and command-palette access to search all tabs.
- Kept captured-output search opt-in and redacted by default.
- Fixed GTK pin/unpin import wiring discovered during static verification.
- Expanded static GUI tests so imported session lifecycle/search helpers are checked without loading GI/VTE.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 169 unit tests covering CLI controls, session search, output search, activity classification, redaction, reconcile, recently closed/reopen flows, pinned sessions, GUI search/static wiring, GUI safety-plan wiring, browser snapshots, diagnostics, desktop integration, settings, resource budgets, window snapshots, master task rows, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `search`, `palette`, and `recently-closed` with an in-memory session database.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.16

This build completes the next browser-control step: remembered terminal tabs can now be searched, inspected for activity, reconciled against the hidden tmux backend, pinned, and reopened from a recently closed list without making users type tmux commands.

### Added or stabilised

- Added `termdeck search` for browser-style lookup across remembered tab metadata, with optional redacted captured-output matching.
- Added `termdeck search-output` for cross-session terminal-output search with redacted snippets by default and `--allow-empty` for scripts.
- Added `termdeck activity` to classify captured output as needing input, error, warning, ready/success, active, silent, not started, unavailable, stopped, or archived.
- Added `termdeck reconcile` to repair stale remembered sessions before restore; missing running sessions are marked stopped/hidden while never-started tabs remain visible as new tabs.
- Added `recently-closed` and `reopen-closed` for browser-like hidden-tab recovery without restarting the process.
- Added `pin` and `unpin` lifecycle controls, with pinned sessions surfaced in task/search rows and protected from hot-session cleanup flows.
- Routed GTK bulk controls through the same `ControlPlan` text renderer used by the CLI, so GUI Send+Enter, pause-hot, and kill prompts share the confirmed safety model.
- Fixed schema/model drift around pinned state and aligned the master task row, task snapshot, search, activity, lifecycle, and store migration paths.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 167 unit tests covering CLI controls, search, output search, activity classification, redaction, reconcile, recently closed/reopen flows, pinned sessions, GUI safety-plan wiring, browser snapshots, diagnostics, desktop integration, settings, resource budgets, window snapshots, master task rows, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `search`, `activity`, `recently-closed`, `reconcile`, and `search-output --allow-empty` using an in-memory session database.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.15

This build completes the next usability step: first-run guidance and keyboard predictability. TermDeck now gives new users a browser-like setup report before they launch the GUI, and the shortcut layer explains which keys come from TermDeck defaults, Terminator imports, generated tab controls, and protected terminal/editing behaviour.

### Added or stabilised

- Added `termdeck quickstart`, a JSON readiness report covering tmux, GTK, VTE, Terminator theme import, desktop launcher status, detected project tabs, and recommended next actions.
- Added shortcut profiles: `browser` for familiar tab controls and `emacs-safe` for users who do not want app shortcuts to steal common readline/Emacs-style terminal keys.
- Added `termdeck shortcuts` and `termdeck shortcuts --json` for discoverable keyboard controls, duplicate accelerator reporting, Terminator keybinding import reporting, and terminal-conflict reporting.
- Added GTK shortcut help and command-palette discovery for the shortcut report.
- Cleaned the shortcut model so the CLI, GTK help window, tests, and Terminator-import reporting use one stable shortcut backend.
- Added static GUI coverage for the shortcut help surface to prevent fresh-process shortcut integration regressions.

### Verified

- Python syntax compilation for `termdeck` and `tests`.
- 139 unit tests covering browser snapshots, CLI commands, quickstart, shortcuts, diagnostics, desktop integration, settings, project detection, resource budgets, window snapshots, master task rows, health badges, control plans, redaction, templates, lifecycle actions, session launching, session storage, tmux discovery/adoption, workspace recipes, and workspace launcher behaviour.
- CLI smoke checks for `--version`, `quickstart`, `shortcuts --json`, `shortcuts --profile emacs-safe --json`, `project-detect --recipe`, `budget-plan`, `pause-over-budget`, `tasks --db :memory: --json`, `diagnostics --db :memory:`, `windows --db :memory: --json`, `workspace-plan --sample`, and no-tmux `adopt-tmux --list` JSON error handling.

### Known runtime limitation in sandbox verification

The development sandbox does not provide the full GTK/VTE/tmux GUI runtime, so GUI launch and live tmux adoption still need visual verification on a Linux desktop with `tmux`, `python3-gi`, GTK 3, and VTE installed.

## 0.1.14

This build stabilised the supportability layer and carried the browser-tab lifecycle model into the visible UI.

- Added local settings support for global and per-tag health thresholds.
- Added `termdeck settings-sample` and `examples/sample.settings.json`.
- Fixed the fresh-process diagnostics import path by restoring the settings backend.
- Exposed side-pane and master-window Hide/Archive actions without stopping the underlying process.
- Added command-palette discovery for hide/archive lifecycle actions.

## 0.1.13

This build added a migration path for users who already have tmux sessions running outside TermDeck.

- Added `termdeck adopt-tmux --list`, `--dry-run`, and adoption into external browser-like TermDeck tabs.
- Added active-pane targeting for adopted external tmux sessions.
- Added `pane_target` and `external` session metadata with SQLite migration support.

## 0.1.12

This build completed browser-tab lifecycle controls in the headless/controller layer.

- Added `hide`, `show`, `move`, `rename`, `retag`, and `archive`.
- Added redaction-safe `diagnostics` and Terminator layout conversion through `terminator-workspace`.
- Kept tab organisation separate from process control, so hiding or moving a tab does not kill its process.

## 0.1.11

This build completed configurable per-tag resource budgets for browser-like terminal task sessions.

- Added `termdeck budget-plan --sample` and `examples/sample.budgets.json`.
- Added `termdeck budget-plan` for CPU, RAM, process-count, and runtime budget evaluation.
- Added confirmation-gated `termdeck pause-over-budget` for budgets whose action is `pause`.

## 0.1.10

This build improved onboarding and multi-window visibility.

- Added dependency-free `project-detect` and `project-init`.
- Added `termdeck windows` and GTK window overview.
- Added project-to-workspace detection for Node, Python, tests, Makefile, Docker, and logs.

## 0.1.9

This build added user-local desktop/app-menu integration.

- Added `install-desktop`, `install-desktop --dry-run`, and `uninstall-desktop`.
- Added a small SVG icon and freedesktop launcher generation.

## 0.1.8

This build finished the selected-session safety and redaction step.

- Added structured control plans for bulk send, Send+Enter, kill, pause-hot, and destructive-looking text.
- Added default redaction for captured terminal output and master previews.
- Added headless `new`, `workspace-open`, and `tasks` flows so browser-like behaviour can be tested without GTK.

## 0.1.5

This build added scriptable pause/resume/kill/send/capture controls that hide tmux behind browser-like selectors.

## 0.1.0

Initial GTK/VTE MVP with hidden tmux sessions, Terminator theme import, browser-like tabs, side pane, restore model, and master task window skeleton.
