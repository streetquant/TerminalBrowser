# Changelog

## Unreleased

- Qualified the Warp Drives source relocation with canonical deployment references to `/var/lib/codexpro-exec/termdeck-runtime`. The historical `/8tb/projects/softwares/termdeck/.runtime` bind remains an explicitly approved cutover compatibility path and is obsolete after activation.
- Aligned clean-exit recovery coverage with dead-pane reuse: `respawn-pane` omits `-k` when the pane is already dead, while live in-place replacement continues to use `-k`.

## 0.2.20

- Added the Warp Drives managed release boundary. A managed launch uses an absolute, release-owned `T1000_MANAGED_ROOT`, verifies the launcher/bootstrap and root are on the expected non-rotational SSD cell, and fails closed when that boundary is unavailable. The tuple `CODEX_STORAGE_FABRIC=warp-drives-v1`, `CODEX_WORKSPACE_STORAGE_CLASS=ssd-cell`, and `CODEX_BULK_STORAGE_MASKED=1` is reported as advisory TermDeck evidence and remains mandatory for managed Codex workspace processes.
- Preserved the exact logical working directory recorded by each recipe. Recovery validates that directory itself and no longer falls back to a parent, home directory, or `/`; an unavailable cwd is an explicit failed recovery.
- Restricted legacy database import to the primary GUI and the single `sessions.sqlite3` registry. The copy is integrity-checked, atomic, retry-safe, and leaves `terminal-auth`, Codex configuration, and other legacy state untouched.
- Added bounded concurrent recovery with durable per-target claims and single-flight behavior. Different recovery targets may proceed concurrently, while the same session, tmux pane, auth scope, or resume target cannot be recovered twice at once.
- Added explicit durable recovery intent. An active intent matching the exact session fingerprint, name, and logical cwd authorizes stopped or start-error recovery; inactive or malformed intents block recovery, while legacy active-status rows retain their narrow compatibility path. New, remembered, external, and archived records remain non-runnable by default.
- Added guarded SSD rebinding for pre-existing native Codex resume scopes. A durable recovery claim plus missing/dead/quiescent backend proof is required; the exact selected account and canonical session roots are preserved, the registry update is compare-and-swap protected, and the old scope remains untouched. External, OpenCode, ambiguous, or incompletely proven sessions fail closed without an implicit auth copy or path move.
- Split validation runtime policy by explicit profile. Production smokes require `T1000_VALIDATION_RUNTIME_BASE` to name an existing private mode-`0700` directory verified as Warp Drives SSD storage; the CI profile creates a dedicated private portable runtime and does not claim Warp Drives qualification. Both remain subject to the short-path and authenticated cleanup gates.

## 0.2.19

- Made terminal history use one truthful viewport. Ordinary wheel, trackpad, selection autoscroll, Page Up/Page Down, and the persistent scrollbar now move VTE's visible normal-screen content; alternate-screen TUIs receive their negotiated mouse protocol without a thumb-only pseudo-scroll.
- Rebuilt tmux replay as a deterministic frame transaction. Captured rows are cleared and style-reset independently with wrapping disabled, application modes and the cursor are restored last, and opening or closing the Tabs pane waits for the settled VTE/tmux grid, eliminating stale rectangles and overlapping Codex composers.
- Kept open terminal tabs visible when their Codex child exits. Tabs-pane membership and counts now follow the notebook/tmux pane rather than transient foreground-process sampling, while hidden, archived, and explicitly removed records retain their existing semantics.
- Made Codex profile switching an atomic same-pane handoff for running sessions. The exact conversation UUID, pane, cwd, model, and tab survive the switch; failed activation restores the previous account and payload instead of leaving a partial selection or a “next launch” state.
- Unified authentication around the canonical system-wide Codex account source. Private per-tab scopes keep only their selection boundary, observe external token/profile replacement through canonical fingerprints, and apply it immediately through the same verified handoff without stale credential copies.
- Removed the second synchronous terminfo inspection from the loaded-host fallback path so temporary `infocmp`/`tic` starvation cannot prevent a terminal from opening.
- Made exact recovery observation preserve the canonical `codex-opencode` model and fail closed when Codex has closed its rollout descriptor. Only one owner-only, non-symlinked CLI rollout matching the process cwd and bounded start window is accepted; multiple candidates remain explicitly recoverable instead of selecting the nearest timestamp.
- Added real GTK/VTE/tmux regressions for sidebar-open pixel equivalence to direct `Ctrl+L`, stable pane identity and modes, scrolling pixels, hidden-start sidebar mapping, open-tab lifecycle, immediate auth switching, exact conversation continuity, rollback, and external canonical-token ingestion.

## 0.2.18

- Restored exactly-once terminal input for OpenCode and other full-screen TUIs. Focused VTE keystrokes are no longer replayed through the parent window, printable text cannot acquire a synthetic Enter, and plain `Ctrl+L` is again application-owned.
- Repaired alternate-screen scrolling in both directions. Mouse-aware TUIs receive their negotiated SGR wheel protocol from physical wheel events and from the persistent per-tab scrollbar; a downward gesture can no longer be mistaken for a primary-screen live-edge no-op.
- Made the child-owned scrollbar deterministic and thread-safe. Its virtual depth now follows accepted application scroll input, returns to the live edge, and never calls the GTK-owned SQLite connection from the bounded history worker.
- Preserved complete composer state across attach, replay, resize, scroll, tab switch, and GUI handoff. Pane replay restores tmux-supported bracketed-paste mode, keeps multiline paste inside one protected transaction, and no longer requests the unsupported empty `focus_flag` format that could strand stream setup in retries.
- Corrected false red recovery state without replacing live payloads. A stale pane-dead notification now revalidates the same live pane and PID as a no-op, while startup reconciliation converts contradictory `recovery-pending`, `missing`, or `start-error` rows back to `running` and clears stale errors.
- Preserved all existing session semantics: clean payload exit still returns to a shell in the same tab, explicit Kill remains terminal, killed history survives until Remove, generic non-Codex recipes remain digest-bound, and existing OpenCode model selections are never rewritten or mass-refreshed.
- Added failing-first unit and real GTK/VTE/tmux coverage for auto-submit, `Ctrl+L`, wheel up/down, scrollbar drag and return-to-bottom, bracketed paste, complete composer redraw, false pane-dead events, live status repair, and the SQLite worker-thread boundary.
- Hardened production validation receipts against inherited output descriptors. Short pytest output is drained before hashing, lingering same-group writers are terminated, escaped writers fail closed, and a published receipt can no longer precede or later diverge from its output artifact.

## 0.2.17

- Made model and account transitions no-loss pane transactions. TermDeck now retires the old control stream before any target mutation, keeps the tab protected until the replacement stream proves stable, and always ends in either a live reattached pane or an explicit recoverable error rather than a keypress-dead VTE surface.
- Added exact in-place gateway-model refresh for `cc-go-ulw`: the live pane must still own the saved resume UUID and directory, input and the process tree are quiesced, the same pane is atomically respawned, and a new PID with the same exact conversation is proven before input returns. A highlighted header action appears only when the validated 43-model cache is newer than that tab's process.
- Added automatic dead-payload recovery. Non-zero failures resume the durable application recipe; successful or Ctrl-C exits from pre-upgrade, unwrapped panes become an interactive shell in the same pane and are persisted as a shell-only recipe. New launches already include this shell supervisor, while explicit `always`, `manual`, and `never` policies retain their declared semantics.
- Replaced the floating new-tab overlay with a real `GtkNotebook` end action. The `+` remains visible and reachable when dozens of tabs overflow a narrow window and can no longer hide itself because the last tab is off-screen.
- Restored application-aware per-tab history ownership, bounded asynchronous scrollbar navigation, native selection extension, and backend-evidenced live-edge decisions without coupling one tab's reading position to another tab or to window selection.
- Added tool-independent canonical launch recipes, digest verification, additive SQLite migration, unlimited multi-window restoration, durable killed-terminal history, generic non-Codex recovery, and fail-closed external/manual/corrupt boundaries.
- Hardened gateway-model cache parsing against symlinks, partial or racing reads, inode/mtime replacement, malformed JSON, oversized fields, invalid timestamps, duplicate aliases, and non-canonical fingerprints.
- Added failing-first unit, GTK overflow, account-ordering, private/non-private exact-restart, real disposable tmux, legacy clean-exit shell, input-echo, live model-census, and no-regression validation coverage. Existing live gateway tabs are not mass-restarted because captured output cannot prove that a composer has no unsent draft; each stale tab exposes its exact in-place refresh action instead.

## 0.2.16

- Isolated the tmux server from the graphical application lifecycle. A first server now starts in its own delegated user-systemd scope when the socket mount is shared, or is moved immediately into a delegated anchor when mount namespaces differ, so stopping or replacing the GUI cannot terminate the server or its terminal payloads.
- Added bounded migration for legacy live servers that inherited a GUI service cgroup. Migration binds to the exact tmux PID and Linux start time, creates a delegated process-only anchor, moves only that process through systemd's `AttachProcessesToUnit`, and revalidates PID, start time, cgroup, and unit policy before success.
- Added a GApplication `quit-preserving-sessions` action for graceful, remotely triggered GUI-only release handoff. The action persists live session metadata and closes application windows without terminating tmux sessions.
- Retained the 0.2.15 generation-safe recovery corrections: managed records cannot persist or obey stale server-local `%pane` IDs; trusted plain shells may be recreated only without recovery metadata; Codex, Claude, and non-exact agent state remain fail-closed.
- Added failing-first unit coverage, PID-reuse and unavailable-systemd boundaries, a real disposable first-server scope-or-anchor smoke, and live no-op verification against an already anchored server.

## 0.2.15

- Fixed Codex composer startup corruption at its root. GUI-owned and restored tmux payloads now start only after VTE has a settled character grid, and `new-session -x/-y`, the tmux pane/window, control-mode stream, and the payload's first full-screen frame share that exact geometry. Hidden and all-hidden workspace recipes use the same mapped-window grid.
- Made the initial replay path preserve valid UTF-8 and control bodies while preventing a pre-controller focus event from poisoning the live-input retry window or consuming the user's first key.
- Bound contained-resize recovery to the exact TermDeck process graph and private launch tuple: pane root, same-domain outer `bwrap`, direct inner `bwrap` session leader, and one private Codex witness carrying a unique and internally consistent `CODEX_HOME`, `TERMDECK_AUTH_SCOPE_ID`, and `CODEX_TERMDECK_GATE_ROOT` tuple.
- Pinned the final resize signal to the exact private Codex witness with Linux pidfds on capable kernels. Lookup, permission, liveness, identity, topology, tuple, and signal failures remain fail-closed; the fully revalidated process-group compatibility path is used only when pidfds are genuinely unavailable.
- Moved procfs and pidfd work off the ordered pane setup/resize/input worker. One bounded coalescing dispatcher handles ordinary resize bursts, while setup nudge/final notifications retain finite completion barriers before authoritative capture and replay.
- Added one canonical bounded validation contract covering every discovered test and all 21 GTK/VTE/tmux smokes. A selected-package contained-composer smoke proves alternate-screen recovery before readiness, exact grid preservation, no collapsed-frame replay, no terminal input, no payload restart, MCP-sibling isolation, pidfd signaling, and package-site import binding.
- Hardened managed-GUI observation and cross-process evidence publication with exact release/source/state/UID/boot/namespace identity, single-flight coordination, aggregate result-capacity enforcement, protocol-typed bounded deletion, canonical private receipts, and fail-closed incomplete postures.
- Preserved terminal byte fidelity, scrolling, selection, clipboard, tab/window restoration, persistent tmux and Codex payloads, per-tab authentication isolation, SQLite semantics, immutable managed releases, and exact historical rollback.

## 0.2.14

- Canonicalized wheel central-directory metadata before the reproducibility comparison. Every newly built regular member is now exact Unix `0644` with zero internal/low external attributes, independent of the caller's `umask`; content-addressed wheel, manifest, and release IDs therefore remain identical across `umask 022` and `077`.
- Kept the build controller's process-wide `umask` untouched. The isolated child build runner uses the configured interpreter with `-I -B`, applies `022` only inside that child, and retains the existing offline, no-isolation, no-bytecode, fixed-epoch, bounded build contract.
- Added a bounded single-disk/non-ZIP64 central-directory normalizer that changes only creator/internal/external metadata, proves compressed payloads, local headers, offsets, CRCs, timestamps, order, names, and RECORD content are unchanged, and atomically publishes only after the proof passes.
- Made verification require the canonical archive policy for 0.2.14 and future/unknown version syntax. Verified 0.2.11–0.2.13 rollback artifacts remain accepted through an explicit legacy exception for their generated RECORD member's historical `0664` mode.
- Added failing-first cross-umask builds, mode-only/idempotence properties, malformed EOCD/central-directory rejection, strict metadata negatives, legacy compatibility, and parent-umask non-interference coverage. No GTK/VTE, tmux framing, terminal interaction, session persistence, recovery, or authentication path changed.

## 0.2.13

- Enforced release-directory immutability for newly built releases. Version 0.2.13 and later publish every release directory as mode `0555`, while files remain read-only and single-link, so a same-UID process cannot add `__pycache__`, bytecode, or arbitrary members after verification.
- Preserved verified rollback compatibility for 0.2.11 and 0.2.12 through an explicit version-gated directory policy; their historical `0755` directories remain verifiable, while any writable directory in a 0.2.13+ tree fails both source-side and bootstrap verification.
- Added safe cleanup for a uniquely owned staging tree that fails after directory freezing. Cleanup thaws only that `.staging-<release-id>-…` path, refuses unowned paths, never follows directory symlinks, and cannot remove a published final release.
- Kept Python isolation and bytecode suppression across every launch boundary: the stable launcher, staged-import verifier, and bootstrap state-race re-exec now use `-I -B`, while the bootstrap still sets `sys.dont_write_bytecode` and `PYTHONDONTWRITEBYTECODE` before importing installed code.
- Extended managed-GUI identity to recognize both the legacy `[python, -I, bootstrap]` prefix and the canonical `[python, -I, -B, bootstrap]` prefix, reporting the observed launch profile without exposing private argv values.
- Authenticated launcher and bootstrap upgrades against executable code rather than trusting hashes recorded in state. Current templates are accepted directly; historical templates must match deterministic bytes derived under `-I -B` from the selected immutable wheel. Arbitrary state-recorded activation hashes remain outside tampering and are rejected.
- Made managed rollback publish the selected release tree, target-version bootstrap, target-version launcher, interpreter identity, and state pointer as one transaction. Any bootstrap, state, launcher, or post-publication verification failure restores every preimage; reinstalling a newer release re-authenticates the historical activation and upgrades it back to current templates.
- Bound historical activation derivation to an `openat`/`O_NOFOLLOW` directory chain and a private verified wheel copy. Symlinked release/artifact containers, writable or hard-linked artifacts, malformed or oversized probe output, output-retaining descendants, and timeout/overflow conditions fail closed with process-group cleanup.
- Preserved custom managed launcher directories across runtime self-inspection. An explicit `bin_dir` remains authoritative; otherwise release commands use `T1000_MANAGED_BIN_DIR` before the standard `~/.local/bin` fallback. The bootstrap overwrites that environment value with the parent of the already verified launcher before importing the CLI, so stale inherited values cannot redirect a managed process and custom installs can run `release info --verify` without repeating their bin path.
- Corrected the installed-runtime investigation: the 0.2.12 GUI did not create bytecode. An adversarial verifier bypassed the managed bootstrap and direct-imported the installed site without `-B`, exposing that the release directories were owner-writable. The bootstrap correctly rejected the resulting contamination; 0.2.13 closes the underlying write capability rather than weakening that check.

## 0.2.12

- Fixed restored and newly attached terminal panes corrupting valid UTF-8 Codex glyphs during initial replay. Tmux command replies now expose an explicit immutable byte view, and pane history plus visible-screen replay remain bytes until they enter VTE.
- Preserved arbitrary command-reply content beginning with `%output`, `%message`, `%window-add`, `%begin`, `%end`, or `%error`. Control framing now requires an exact three-decimal-field marker with matching identity; every other line inside a reply remains body data.
- Corrected tmux error reporting to retain exact body bytes and use the human-readable body as the caller-facing diagnostic rather than exposing numeric frame metadata.
- Added real tmux/VTE regressions proving Unicode history and screen replay remain byte-faithful, common mojibake is absent, percent-prefixed pane text is preserved, and command bodies cannot manufacture asynchronous events.
- Added a read-only, redaction-safe managed-GUI inventory based on exact interpreter/`-I`/bootstrap argv elements, canonical executable, Linux boot ID, PID start ticks, UID, and repeated cmdline observations. Substring lookalikes and CLI invocations are excluded; incomplete or multiple-GUI evidence fails activation closed without conflating runtime posture with release integrity.
- Extended release verification with stable process posture while preserving all tmux/Codex sessions, per-tab `CODEX_HOME` scopes, scrolling, selection, contrast correction, restoration, and managed rollback behavior.
- Verified 1,182 tests plus 98 subtests, the complete reproducible build/install/tamper/rollback workflow, and all 19 strict GTK/VTE/tmux runtime smokes with empty stderr, unchanged 281-row semantic state, stable source/Git/process identity, and no leaked smoke process.

## 0.2.11

- Fixed restored or newly selected Codex tabs that could remain connected but blank. Visible-tab setup priority is now latched before the delayed controller exists, setup exceptions retry until the pre-ready transaction commits, and CONNECTED reset plus pane replay are serialized through one GTK output drain so a late idle cannot erase a complete screen.
- Corrected tmux pane-mode replay: `*_flag=1` now means enabled, and every cursor, keypad, wrap, insert, origin, and mouse mode receives an explicit set or reset sequence. Normal shells therefore keep VTE-native scrollback instead of accidentally enabling all mouse protocols and forwarding wheel events into the pane.
- Added ISO colon-form SGR contrast handling for direct RGB and indexed foregrounds, including the forms used by modern TUIs. Supported low-contrast ink is lifted to the 4.5:1 stream floor; unrelated colon attributes, explicit backgrounds, reverse video, and unknown colour spaces retain fail-open semantics.
- Replaced the obsolete tmux-copy-mode scroll smoke with a real GTK/VTE contract test covering native shell history, incremental wheel movement, alternate-screen fallback arrows, negotiated SGR mouse events, and zero tmux copy-mode entry.
- Added a real VTE pixel smoke proving formerly invisible colon truecolour, faint truecolour, and indexed-grey text remains readable on the default dark surface.
- Made tmux control transports, setup retries, pre-ready input/output, GTK delivery, and teardown explicitly bounded and deterministically owned. Stuck or failed clients are closed, killed when necessary, reaped, and prevented from leaking timers, threads, buffers, or BrokenPipe finalizers.
- Made SGR filtering total over arbitrary pane bytes with bounded decimal/sequence parsing, chunk-equivalent streaming, state taint/recovery, exact VTE no-op semantics for invalid RGB/indexed values, and fail-open unknown colour spaces.
- Fixed minimum-contrast endpoint selection on saturated mid-tone backgrounds by considering both black and white rather than assuming luminance alone determines the reachable direction.
- Added deterministic, offline, double-built wheel releases with commit/tree/build identity, strict metadata/RECORD/path verification, canonical manifests, and content-addressed release IDs.
- Added an atomic managed installer and rollback path that binds Python, bootstrap, launcher, state, manifest, artifact, and installed file set; rejects symlinks, writable or hard-linked payloads, tamper, races, and partial publication; and never restarts persistent tmux/Codex tasks.
- Extended the existing doctor and redaction-safe diagnostics with managed-release posture, plus a read-only semantic runtime receipt for install/restart non-interference.
- Verified 1,153 tests plus 98 subtests, deterministic parser and release-property gates, a complete isolated build/install/tamper/rollback workflow, and all 16 strict GTK/VTE/tmux runtime smokes with empty stderr and unchanged persistent semantic state.

## 0.2.10

- Added fail-closed legacy Codex archive recovery for tabs saved before exact UUID persistence. Startup reads only bounded primary `source="cli"` metadata, matches exact project directories or unique near-time sessions, prevents duplicate UUID resumes, and restores unprovable shell-backed tabs as shells without calling them exact conversations.
- Fixed rebooted Codex tabs being launched below DMTCP, whose default checkpoint signal conflicts with Codex. New and recovered Codex commands now bypass DMTCP completely and resume directly by their exact UUID.
- Kept exact Codex recovery metadata durable through both containment and shutdown races: a primary `source="cli"` rollout remains authoritative when bubblewrap gives the native process no host controlling TTY, and late shutdown observations can no longer erase an already persisted UUID after tmux teardown begins.
- Added reboot-safe exact Codex recovery: TermDeck identifies the primary foreground Codex rollout UUID, excludes subagent rollouts, stores only a validated `codex ... resume <UUID>` command, prefers the native Codex process over launcher wrappers, and prevents weaker periodic observations from downgrading an exact saved UUID.
- Disabled automatic DMTCP wrapping and restart for production tmux tabs after real PTY validation exposed repeated application-signal conflicts, Bash fork-path assertions under alternative signals, and indefinite drains on external HTTPS sockets. Legacy checkpoint manifests are rejected by a new schema instead of being treated as trusted reboot state.
- Made crash recovery fail closed. A previously running tab becomes `recovery-pending`; production startup restores an exact Codex UUID when available, otherwise it remains an explicit recovery error and never starts a substitute/random process. Deliberately new tabs still launch normally.
- Added `t1000 checkpoint --all` plus user- and system-level checkpoint-service installers. The strict system service is ordered after `user@UID.service` while active, so inverse shutdown ordering performs the final Codex UUID scan before the user manager, tmux scopes, required mounts, and tab processes are stopped.
- Retained the DMTCP image manager only as a disabled experimental component for isolated or dependency-injected research tests. Non-Codex reboot continuity now requires an application-level checkpoint and idempotent resume path rather than a false generic process-memory guarantee.
- Restored real scrolling behind the tmux attachment: shell panes use tmux copy history, while foreground Codex panes open and navigate Codex's transcript before any Up/Down input is emitted.
- Prevented wheel movement from cycling Codex prompt history, hid the pager caret while scrollback is being reviewed, and made the first typed key, paste, or ordinary click restore the live composer cursor before delivering input. The Terminator-style scrollbar now has an independent tmux-history range, an opaque trough and thumb, and drag navigation instead of inheriting VTE's empty alternate-screen range.
- Restored ordinary drag-to-select and automatic copying to both the system clipboard and PRIMARY selection without destroying VTE's visible selection highlight.
- Routed clipboard text through VTE's bracketed-paste-aware API so `Ctrl+Shift+V` pastes without synthesizing Enter, and explicitly replayed otherwise-unhandled window keys into the active terminal without stealing focus from text fields or losing the first character.
- Disabled tmux mouse capture for TermDeck sessions so wheel/click activity cannot strand full-screen TUIs such as Codex in tmux copy mode away from their composer.
- Kept VTE, its PTY client, and the tmux window on the same live character grid after window or Tabs-pane layout changes; hiding the pane now removes it from layout so the terminal reclaims the entire width instead of clipping right-side columns.
- Sized left- and right-positioned Tabs panes from their scaled minimum width and reflowed them after GTK applies appearance CSS, preventing high UI scales from pushing leading controls off the window edge.
- Kept default tab names synchronized with each tmux pane's live working-directory basename through the shared batched pane snapshot; an explicit tab rename remains a stable override.
- Made each Tabs-pane card a single-click navigation target while preserving `Ctrl+left-click` bulk selection and the card controls.
- Added **Move to window** to every Tab Manager row's right-click menu, including **New window…**, which creates no extra shell; live tabs are reparented with the same VTE widget and tmux client while hidden rows attach only to an already-running session.
- Added explicit large-registry control-plane defaults for 128 process-tree samples per refresh, 100 Tabs-pane cards, and 500 Tab Manager rows while restoring every visible remembered terminal at startup.
- Recreated every remembered named window and every visible tab on relaunch instead of deferring tabs behind a startup attachment cap.
- Persisted a separate crash-recovery snapshot for a live foreground Codex invocation and its exact process working directory, then relaunch it there with restart-safe flags such as `--yolo` before returning to the tab's saved shell; command and cwd updates are atomic, a deleted recovery directory falls back to the saved tab directory, and terminal output, prompts, config overrides, background jobs, and adopted sessions are never auto-replayed.
- Restricted Tab Manager **Remove** to unpinned tabs already marked stopped; live, paused, new, and missing selections remain intact.
- Standardized the destructive action label as **Kill** across Tab Manager, tab menus, the command palette, shortcuts, and documentation; `stopped` remains the internal lifecycle state.
- Bounded PID churn caches and terminal captures, added adaptive fleet-query deadlines with failure backoff, and made failed snapshots preserve stale lifecycle/PID state without N-way retry fan-out.
- Batched reconciliation state writes into one SQLite transaction and kept pressure/timeout paths strictly non-destructive; only explicit lifecycle actions can stop sessions.
- Added synthetic 2,000-session coverage plus real GTK checks for Tab Manager window relocation, select-to-copy, the rendered and draggable scrollbar, live-viewport clicks, and first-key delivery after focus leaves the terminal.
- Replaced blocking clipboard reads with GTK's asynchronous text-request API, including safe late-callback handling after a terminal detaches.
- Added strict tmux command deadlines and batched session/PID discovery for startup reconciliation and tab restoration.
- Switched the file-backed session registry to SQLite WAL/NORMAL mode with a one-second lock deadline, retaining the default journal as a compatibility fallback.
- Prevented an all-pane timeout from expanding into one synchronous tmux lookup per tab, while preserving the last known live state during transient backend stalls.
- Stopped the hidden-by-default Tabs pane from querying SQLite, tmux, `/proc`, or rebuilding widgets until it is visible.
- Replaced the terminal client's Python `preexec_fn` path with `start_new_session=True` and focused newly opened VTE surfaces immediately.
- Tightened the header and terminal frame, replaced the oversized circular New-tab action with a compact semantic control, removed persistent-surface transitions and broad shadows, and restored readable tab-name widths.
- Added shortcut hints and accessible close names without adding visible chrome.
- Verified 524 tests plus real GTK/tmux recovery, including a 30-tab empty-backend recovery that resumes an exact Codex UUID in its saved process cwd, legacy archive reconstruction, explicit rejection of automatic DMTCP for production tabs, select-to-copy, cross-window tab relocation, remembered-window startup seeding, killed-tab history, first-key delivery into a waiting PTY, live cwd labels, high-scale Tabs-pane bounds/navigation, tmux/Codex wheel and thumb-drag routing without a roaming pager cursor or composer history input, and synchronized resize grids.

## 0.2.9

- Removed the GTK directory chooser from New tab, first-run window creation, and New TermDeck window flows.
- `Ctrl+T`, the header New tab button, the command palette, the Tabs pane empty state, and secondary New tab actions now open a shell immediately.
- Added live working-directory inheritance through tmux `#{pane_current_path}` so a new tab follows a `cd` performed inside the active shell rather than relying only on the directory stored at tab creation.
- Added deterministic fallbacks from live pane cwd to the stored tab directory and then the user's home directory when no tab is active.
- Made Ctrl+T an explicit terminal-level shortcut so VTE cannot consume it before TermDeck creates the tab.
- Updated first-run and additional-window creation to ask only for the window name; the first shell opens immediately in home or the parent window's active directory.
- Added unit and real GTK/tmux coverage for live-cwd lookup, home fallback, immediate tab creation, absence of chooser windows, basename naming, and running-session creation.
- Verified 347 tests and 15 subtests; real Ctrl+T created a `live-work` tab in the shell's live directory with zero chooser windows, and desktop/compact renders completed with zero application or layout errors.

## 0.2.8

- Made `Ctrl+Shift+V` an unconditional system-clipboard paste shortcut in the active terminal.
- Prevented imported Terminator keybindings from displacing the standard paste accelerator or making the shortcut report disagree with runtime behavior.
- Preserved `Ctrl+V`, `Shift+Insert`, and explicitly configured additional paste bindings as secondary paste paths.
- Added focused shortcut and pipeline tests plus a real GTK clipboard smoke that owns `Gdk.SELECTION_CLIPBOARD`, triggers `Ctrl+Shift+V`, and verifies the exact clipboard text is fed once into the active VTE.
- Verified 341 tests and 15 subtests, exact one-shot clipboard delivery, and desktop/compact renders with zero application or layout errors.

## 0.2.7

- Reworked the GTK visual system around a softer layered dark palette with clearer separation between the app background, raised chrome, sunken terminal surface, borders and accent states.
- Framed the terminal notebook as an inset 16px-radius surface with a dedicated tab bar, deeper shadow and compact-mode spacing overrides.
- Replaced flat connected tabs with rounded floating tabs; the active tab now uses an accent-tinted surface, focused border, lower accent rail and restrained shadow.
- Standardised buttons, hover/active/focus states, menus, scrolled views, text previews, side-pane cards and descriptive text so primary and auxiliary windows share one design language.
- Changed the New tab action into a circular accented control and reduced inactive tab-close prominence until hover or selection.
- Added semantic `td-main-root`, `td-terminal-notebook` and `td-section-description` hooks plus a real GTK visual-polish smoke in CI.
- Verified 338 tests and 15 subtests, all existing GTK lifecycle checks, exact `640x520` compact rendering, and a 32.24% compact-frame pixel change from 0.2.6 without application or layout errors.

## 0.2.6

- Fixed the startup dotted-padding glitch by delaying tmux client attachment until VTE has a stable final grid and seeding the PTY with that exact geometry before launch.
- Added explicit horizontal and vertical expansion to the terminal container, notebook, paned layout and root overlay so the terminal surface covers the complete client area.
- Defined browser-style lifecycle shortcuts: `Ctrl+W` closes the active tab while keeping its tmux session alive; `Ctrl+Q` quits TermDeck while keeping every tmux session alive; `Ctrl+Shift+W` remains the destructive kill action.
- Explicit tab-close actions now close pinned tabs as well; pinning still protects non-explicit cleanup workflows and the pinned state remains stored.
- Clarified the management views in the UI: Tab Manager provides per-tab/process details and controls, while Window Overview provides read-only totals by named window.
- Added real GTK/tmux smokes for first-attachment geometry, full client-area coverage, Ctrl+W recovery and Ctrl+Q session preservation.
- Verified 337 tests and 15 subtests; first VTE/tmux attachment grids matched, all sessions survived close/quit checks, and desktop/compact renders completed with zero application or layout errors.

## 0.2.5

- Added conservative typo-tolerant matching to Search Tabs and the `termdeck search` CLI.
- Terms of four or more characters now support bounded Damerau-Levenshtein matching for insertion, deletion, substitution, and adjacent transposition; short terms remain literal to suppress noisy results.
- Applied the same fuzzy matching to opt-in recent terminal-output search after redaction.
- Kept exact/literal metadata matches ranked above fuzzy alternatives and preserved the all-query-terms-required rule.
- Updated Search Tabs labels, placeholder, preview guidance, and CLI help to describe typo tolerance and the latest-120-lines output boundary.
- Added unit and real-GTK coverage for `pytes`, `pyetst`, literal-over-fuzzy ranking, short-query suppression, and `compilaton` matching captured `compilation` output.
- Verified 336 tests and 15 subtests, all four GTK lifecycle smokes, and desktop/compact renders with zero application or layout errors.

## 0.2.4

- Simplified the visible notebook tab to exactly the user-defined tab name and one close control.
- Removed type/category glyphs, pin emoji, and live health/attention markers from the main tab strip.
- Added end ellipsis and a 24-character width cap so long directory-derived names remain compact without changing their stored value.
- Kept pinned-state guidance in tooltips/context actions and retained health/resource detail in the optional Tabs pane and Tab Manager.
- Added static and real-GTK coverage for exact pinned and renamed tab labels, close-control stability, and absence of visible decoration.
- Added the minimal tab-strip smoke to the GitHub Actions UI job before desktop and compact rendering.
- Verified 330 tests and 11 subtests, the tab-strip smoke, and desktop/compact renders with zero application or layout errors.

## 0.2.3

- Reduced the main HeaderBar to exactly three concepts: the user-defined window name, New tab, and More.
- Removed the decorative status dot, the `Directory tabs` subtitle, and the duplicated `TermDeck ·` prefix from the in-app title.
- Moved Search tabs and Command palette into More while retaining `Ctrl+Shift+P` and all existing keyboard access.
- Kept the native window-manager title as `TermDeck · <window name>` and fixed its initial assignment order after the custom HeaderBar is installed.
- Added a dedicated real-GTK header smoke check for exact visible actions, accessible names, native title, and ordered More-menu contents.
- Verified 330 tests and 11 subtests, the reduced-header lifecycle smoke, and desktop/compact renders with zero application or layout errors.

## 0.2.2

- Changed the default main layout to terminal-first: the tab strip and active terminal are shown, while the advanced Tabs pane starts hidden.
- Replaced the legacy `focus_mode` preference with canonical `appearance.tabs_pane_visible`, defaulting to `false`, while preserving inverse compatibility for existing focus-mode configs.
- Consolidated duplicate pane controls into one persisted **Show/hide Tabs pane** action available from More, the command palette, and `F9`.
- Synchronised the Tabs-pane preference across every open window and ensured newly created windows inherit the current state.
- Removed redundant post-construction `show_all()` calls that remapped hidden panes during saved-layout and restored-window creation.
- Added unit and real-Xvfb smoke coverage for default hiding, `F9`, persistence, legacy migration, cross-window inheritance, and synchronisation.
- Verified 330 tests and 11 subtests, plus desktop and compact terminal-first GTK renders with zero application or layout errors.

## 0.2.1

- Replaced the fresh-launch `Main` window and implicit current-directory shell with an explicit first-run flow: name the window, then choose its first tab directory.
- Centralised first-run and additional-window creation through one application-level path so both flows use the same naming and directory-selection behavior.
- Removed the `open_initial_shell` constructor switch and all automatic current-working-directory tab creation.
- Cancelling the first-run naming dialog now leaves no generic window or silently launched process; cancelling the directory chooser leaves a valid named empty window with a visible New tab action.
- Added behavioral coverage for first-run success, cancellation, additional-window reuse, noninteractive window creation, and activation routing.
- Verified 325 tests and 11 subtests, plus a real Xvfb first-run flow that persisted `Client work` and created a `client-api` tab from the selected directory.

## 0.2.0

- Simplified the product to one visible hierarchy: named TermDeck windows containing directory-backed tabs.
- Replaced task creation with a directory chooser; each new tab opens the user's default shell and uses the directory basename as its initial title.
- Added persistent window naming and a New TermDeck window flow that asks for a group name before choosing the first tab directory.
- Removed the task launcher, task-template modules, template CLI command, custom-task GUI path, and launcher shortcuts.
- Removed project detection from the GUI, command palette, quickstart report, CLI, and executable package.
- Simplified `termdeck new`: removed `--tag`, changed the JSON result from `sessions` to `tabs`, and stopped exposing internal tag/tmux fields in its normal creation response.
- Removed visible tag chips, Retag actions, and Tag columns from the Tabs pane, Search, and Tab Manager while retaining internal metadata for backward compatibility.
- Reworded the primary GUI around Window, Tabs, New tab, Rename window, Tab manager, and directory-based search.
- Preserved restoration of existing remembered tabs, saved window layouts, Terminator imports, snapshots, and hidden tmux processes.
- Added tests for directory chooser behavior, directory-derived titles, scoped window renaming, timestamp preservation, and removal of obsolete launcher/project surfaces.
- Verified 319 tests and 11 subtests, plus desktop and compact GTK renders with zero application or layout errors.

## 0.1.32

- Replaced Ctrl+right-click session collection with standard Ctrl+left-click multi-selection across the complete Sessions card.
- Ctrl+left-click now toggles a session from the card body, checkbox, tag, Rename button, or Open button while preserving peer selections.
- Right-click is now menu-only and no longer changes selection, even when Ctrl is held.
- Updated Sessions-pane guidance, tag tooltip, README documentation, and pointer regression coverage.
- Verified 323 tests and 11 subtests, plus desktop and compact GTK renders with zero application or layout errors.

## 0.1.31

- Reduced titlebar minimum width by converting New, Shell, Search, Palette, and More to compact icon controls with preserved tooltips and accessible names.
- Added ellipsis-aware title and subtitle bounds so long workspace names no longer force the window wider.
- Marked both `Gtk.Paned` children shrinkable, preserving the preferred desktop split while allowing constrained terminal and Sessions-pane layouts.
- Extended the real GTK visual-smoke harness with explicit window dimensions, requested-versus-captured reporting, and failure on window-size clamping.
- Added a `640x520` compact render to CI alongside the desktop frame and upload both screenshots as review artifacts.
- Added responsive-layout and visual-harness regression coverage; the full suite now passes 321 tests and 11 subtests.

## 0.1.30

- Added an isolated Xvfb visual-smoke harness that launches the real `termdeck gui` entry point, captures the mapped GTK window, and cleans up its temporary HOME, database, D-Bus session, X server, and tmux server.
- Fixed a runtime Sessions-pane crash caused by unsupported PyGObject `set_data` / `get_data` calls; card references now use normal Python widget attributes.
- Fixed `termdeck gui --db ... --workspace ...` by preventing argparse-consumed options from being passed to `Gtk.Application` a second time.
- Added regression coverage for the GTK card-reference path and the CLI-to-GTK argument boundary.
- Added a dedicated GitHub Actions `ui-smoke` job that installs GTK/VTE dependencies, renders the real application, and uploads the captured frame as a CI artifact.
- Repaired CI by separating Python 3.10–3.12 syntax/CLI compatibility checks from the full GTK-enabled test suite, so PyGObject-dependent tests run against system GTK bindings instead of failing during import.
- Updated GitHub Actions to the current `checkout` v7, `setup-python` v6, and `upload-artifact` v7 major releases.
- Updated development documentation for the modern overflow header and repeatable real-window visual verification.

## 0.1.29

- Rebuilt the GTK chrome around layered dark, noir, and system palettes with modern spacing, rounded geometry, elevation, and consistent interactive states.
- Simplified the main header to high-frequency New, Shell, Search, and Palette actions with secondary workspace controls in a responsive overflow menu.
- Redesigned the Sessions pane with a workspace hierarchy, live active/selected counts, stronger search and prompt fields, semantic bulk actions, and a guided empty state.
- Added persistent selected-card styling, clearer health/tag badges, modern tab treatment, refined menus, tooltips, scrollbars, tables, dialogs, and toast feedback.
- Extracted the chrome stylesheet into a dedicated module and added visual-system regression coverage plus GTK parser verification for every theme.

## 0.1.28

- Added Ctrl-right-click selection across the complete Sessions card, including its checkbox and action buttons, while preserving existing multi-selection.
- Updated Sessions-pane guidance and added behavioural coverage for card-wide context selection.
- Materialised portable workspace placeholders such as `{shell}` immediately before GTK project panes create their sessions.
- Prevented detected project shells from launching the literal `{shell}` command and dying with status 127.
- Added a runtime regression test for project-pane command materialisation.

## 0.1.27

- Made select-to-copy durable across focus and selection changes by installing GTK clipboard ownership after VTE updates its internal selections.
- Added a regression test that verifies durable clipboard ownership is the final step in the copy pipeline.
- Shared one cached tmux all-pane snapshot across repeated single-session PID lookups instead of spawning tmux for each UI action.
- Matched tmux and `/proc` cache lifetimes to the configured UI refresh cadence and reduced unchanged Sessions-pane metric rebuild frequency.
- Removed killed session tabs from every open TermDeck window, not only the window that initiated the action.
- Explicitly marked auxiliary windows deletable in addition to their existing Close and Escape paths.

## 0.1.26

- Added browser-style main-tab context menus for rename, retag, pin/unpin, hide, kill, Terminal options, Master tasks, hide-other-tabs, and hide-tabs-to-the-right.
- Made pinned tabs explicitly closeable through a guided **Unpin and hide tab** path instead of silent close-button no-ops.
- Made Sessions-pane row menus bulk-aware for Ctrl-click multi-selection, including retag, pin, unpin, hide, and kill actions.
- Debounced heavy GUI search/filter refreshes and added behavioural tests for refresh coalescing and timer cleanup.
- Cached Master captured-output previews briefly and added behavioural tests proving repeated previews avoid repeated backend capture calls.
- Added behavioural tests for pinned-tab close handling and expanded user-reported UI regression coverage.

## 0.1.25

- Added configurable UI refresh intervals through `appearance.refresh_interval_seconds` / `appearance.ui_refresh_interval_seconds` / `appearance.refresh_seconds`.
- Wired the setting into the side pane, master task window, and window overview timers.
- Reused the configured interval for side-pane dynamic resource refresh buckets, so slow machines can reduce background churn without code edits.
- Clamped refresh intervals to 1–30 seconds and added settings/static UI coverage.

## 0.1.24

- Fixed periodic refresh timer leaks after helper windows or side-pane widgets are destroyed.
- Added `_refresh_alive` destroy hooks so overview, master, and side-pane timers return `False` and self-cancel after teardown.
- Added static regression coverage for timer cleanup paths.

## 0.1.23

- Fixed side-pane resource staleness introduced by the refresh signature cache.
- Added a timed resource-refresh bucket so unchanged session metadata preserves rows briefly but still rebuilds CPU/RAM/health cues periodically.
- Extended the static side-pane regression test to require both pre-clear cache checks and timed dynamic refreshes.

## 0.1.22

- Fixed a side-pane refresh regression where the unchanged-signature fast path could return after clearing all visible session rows.
- Moved row removal/rebuild work after the cache-signature check, preserving the current Sessions pane when nothing changed.
- Added a static regression guard ensuring the side pane checks its refresh signature before clearing widgets.

## 0.1.21

- Added short-lived per-PID resource snapshot caching so adjacent side pane, master, and overview refreshes do not repeatedly walk `/proc` trees and RSS data for the same process.
- Added explicit `ResourceSampler.invalidate_cache()` for process-state changes and tests for cache reuse/invalidation.
- Kept the cache TTL short by default so UI responsiveness improves without stale resource data persisting across interactions.

## 0.1.20

- Added a short-lived tmux pane/PID cache so multiple UI surfaces can share one `list-panes -a` snapshot instead of repeatedly spawning tmux during refresh.
- Invalidated the pane cache after session create, rename, and terminate operations so process controls stay fresh.
- Added backend tests proving repeated `pane_pids` calls reuse the cached snapshot and `invalidate_cache()` forces a refresh.

## 0.1.19

- Fixed select-to-copy to write through the real GTK system clipboard for external paste targets, while keeping PRIMARY and VTE fallbacks.
- Added a terminal right-click menu for copy, paste, paste primary, select all, and clear selection.
- Added cursor blink configuration and a Terminal options checkbox to disable blinking.
- Added persistent left/right Sessions pane placement with right-click header controls.
- Hid stopped/killed sessions from the Sessions pane and added Ctrl-click tag multi-selection.
- Added explicit Close/Esc behaviour to master/search/overview helper windows.
- Reduced periodic heavy UI refreshes and skipped unchanged side-pane rebuilds for better responsiveness.

## 0.1.18

- Strengthened terminal select-to-copy so selected text is synchronised to both the system clipboard and PRIMARY selection.
- Added delayed selection-change copying to cover double-click, triple-click, keyboard selection, and drag-selection timing edge cases.
- Kept immediate left-button-release copying for browser-like mouse selection.
- Expanded static GTK tests to cover the debounced clipboard sync path.

## 0.1.17

- Added `SessionSearchWindow` so remembered-tab search is available from the GTK header and command palette, not only from the CLI.
- Routed the new `search tabs` palette action to the GUI search surface.
- Fixed GTK pin/unpin static import wiring so side-pane and master-window pin buttons resolve cleanly at runtime.
- Expanded static GTK import coverage to include session lifecycle and session search modules.
- Verified 169 tests plus targeted CLI smoke checks before packaging.

## 0.1.16

- Added `termdeck search` for remembered-tab metadata search, with optional redacted captured-output matching.
- Added `termdeck search-output` for cross-session terminal-output search with safe snippets and `--allow-empty` scripting support.
- Added `termdeck activity` to classify captured output into attention-ready browser states such as needs input, error, warning, ready, active, silent, not started, and unavailable.
- Added `termdeck reconcile` to repair stale remembered sessions before restore without starting processes.
- Added `recently-closed` and `reopen-closed` for browser-like hidden-tab recovery.
- Added `pin` and `unpin` lifecycle controls and pinned-state support in storage, search, and task rows.
- Routed GTK selected-session controls through the shared `ControlPlan` safety renderer.
- Fixed pinned-state schema/model drift and verified 167 tests plus CLI smoke checks.

## 0.1.15

- Added `termdeck quickstart` as a first-run readiness report covering dependency checks, Terminator import status, desktop launcher state, detected project tabs, and recommended next actions.
- Added shortcut profiles: `browser` and `emacs-safe`.
- Added `termdeck shortcuts` and `termdeck shortcuts --json` for Terminator keybinding imports, duplicate accelerator reporting, and terminal/editing conflict reporting.
- Added GTK shortcut help and command-palette discovery for keyboard controls.
- Stabilised the shortcut backend used by CLI, GTK, tests, and Terminator-import reporting.
- Added static GUI coverage for the shortcut help surface.
- Verified 139 tests plus CLI smoke checks for quickstart, shortcuts, resource budgets, diagnostics, windows, tasks, workspace samples, and no-tmux adoption error handling.

## 0.1.14

- Added local settings support for global and per-tag health thresholds.
- Added `termdeck settings-sample` and `examples/sample.settings.json`.
- Fixed diagnostics/fresh-process CLI import regression caused by the missing settings backend.
- Exposed browser-tab lifecycle actions in the GTK side pane and master task window.
- Added command-palette actions for hiding and archiving selected sessions.

## 0.1.13

- Added `termdeck adopt-tmux` to list, dry-run, and adopt already-running tmux sessions as browser-like TermDeck tabs.
- Added active-pane targeting for adopted external tmux sessions.
- Added `pane_target` and `external` session metadata with SQLite schema migration.

## 0.1.12

- Added browser-tab lifecycle commands: `hide`, `show`, `move`, `rename`, `retag`, and `archive`.
- Added `session_lifecycle.py` so tab visibility, window placement, naming, tags, and archiving are handled without exposing tmux commands.
- Added `diagnostics` for redaction-safe support bundles.
- Added `terminator-workspace` for converting Terminator layout information into workspace recipes.

## 0.1.11

- Added configurable per-tag resource budgets through `termdeck budget-plan`.
- Added confirmation-gated `termdeck pause-over-budget` for budgets whose action is `pause`.
- Added `examples/sample.budgets.json`.

## 0.1.10

- Added `termdeck project-detect` and `termdeck project-init` for browser-like workspace suggestions from project directories.
- Added detection for Node scripts, package-manager lockfiles, Python markers, tests, Django, Makefile targets, Docker, and logs.
- Added `termdeck windows` and GTK `WindowOverview` for aggregate multi-window resource visibility.

## 0.1.9

- Added `termdeck install-desktop` and `termdeck uninstall-desktop` for user-local desktop/app-menu integration.
- Added dry-run JSON output for desktop launcher installation.
- Added a small embedded SVG icon and freedesktop `.desktop` entry generation.

## 0.1.8

- Added headless browser-style task creation through `termdeck new`, including template resolution, custom commands, remembered window/tab placement, and `--no-start` support.
- Added `workspace-open` to materialise JSON workspace recipes into remembered process capsules without launching the GTK UI.
- Added `tasks` as a headless master-task snapshot.
- Added selected-session safety planning and default capture redaction.

## 0.1.5

- Added scriptable pause/resume/kill/send/capture controls that hide tmux behind browser-like selectors.
- Added JSON outputs for side-pane/controller integration.

## 0.1.0

- Initial GTK/VTE MVP with hidden tmux sessions, Terminator theme import, browser-like tabs, side pane, restore model, and master task window skeleton.
