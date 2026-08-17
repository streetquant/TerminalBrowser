# T1000 Five-Round Production Hardening Ledger

This document is the release controller's append-only design and evidence ledger
for BR issue `termdeck-production-hardening-release-po8`.  Each round is closed
only after implementation, validation, and an adversarial review.  The review of
round `r` supplies the accepted findings and implementation plan for round
`r + 1`; later rounds do not retroactively weaken an earlier acceptance gate.

## Shared system model

### Ontology

- **Persistent task**: a tmux session/pane and its foreground process tree.
- **Transport**: one `tmux -C attach-session` subprocess owned by
  `ControlModeClient`.
- **Protocol client**: `ControlModeClient`, which serializes tmux control-mode
  replies and notifications.
- **Pane stream**: `PaneStreamController`, which reconstructs tmux state into
  VTE and carries input/output thereafter.
- **Terminal surface**: one `AttachTerminal` VTE widget and its GTK scheduling
  state.
- **Registry row**: one durable `SessionRecord`, including immutable private
  Codex scope identity.
- **Release candidate**: the exact Git tree, tests, smokes, build artifact, and
  installed runtime that share a recorded content identity.

### State model

For one terminal, let

- `C` be the protocol client state;
- `P` be the pane-stream state;
- `G` be the stream generation;
- `B_o` be pre-GTK output bytes;
- `B_i` be pre-ready input bytes;
- `R` be outstanding retry resources;
- `T` be the transport process and its file descriptors.

The important transition is

```text
DISCONNECTED -> CONNECTED -> SETUP -> READY -> STREAMING
                  |             |
                  +--retry------+--bounded failure/retry
```

The release must maintain these invariants:

1. **Persistence**: closing or restarting T1000 never kills a healthy persistent
   task unless the user chose an explicit destructive action.
2. **Identity**: private `CODEX_HOME`, auth-scope ID, exact recovery UUID, tmux
   target, and registry row are never silently substituted.
3. **Ordered visibility**: for generation `G`, emulator reset precedes replay,
   and replay precedes live output.  Bytes from generation `G - 1` cannot paint
   generation `G`.
4. **Boundedness**: transport processes, queues, buffers, retry timers, GTK
   callbacks, and worker resources have finite ownership and finite limits.
5. **Terminal ownership**: VTE is the only client-side terminal emulator and
   owns normal scrollback; tmux remains the persistence server.
6. **Rendering fidelity**: supported low-contrast foreground colors may be
   lifted, but background colors, reverse video, unknown color spaces, and
   unrelated SGR attributes are preserved.
7. **Release identity**: tested tree = committed tree = pushed tree = built tree
   = installed tree.

## Round 1 — reconcile the executable verification contract

### Implementation

Round 1 preserved the production loading/contrast fixes already in the candidate
and repaired runtime smokes that still asserted the removed PTY/proxy-history
architecture.  A shared `scripts/smoke_support.py` now provides a lock-consistent
`StreamSnapshot` and `stream_ready()` oracle.  Geometry verification now checks

```text
G_controller = G_tmux_window = G_tmux_pane = G_VTE
```

rather than blank control-client PTY dimensions.  Move, auth, new-tab,
clipboard, selection, header, visual, scroll, and restore smokes now verify the
current control-mode/VTE design without relaxing their user-visible contracts.

### Validation

- `1051 passed, 1 skipped, 45 warnings, 98 subtests passed`.
- All 16 `scripts/check_*smoke.py` programs passed under a per-smoke hard
  timeout.  The real-Codex cold-scope smoke reported an explicit bounded skip
  because the sealed environment exposes no direct Codex executable.
- Real VTE contrast measured `4.53:1` for formerly invisible colon RGB, faint
  RGB, and indexed-grey cases on `#0a0d13`.
- Native VTE scroll, bidirectional selection, CLIPBOARD/PRIMARY ownership,
  alternate-screen fallback/mouse routing, 30-tab restore, private exact UUID
  recovery, window move, auth switching, and geometry convergence passed.
- Candidate hashes were unchanged by testing; live T1000 PID `1995582` and the
  default tmux namespace were unchanged.

### Adversarial review

Two sealed model-free workers reviewed the exact Round 1 source snapshots.

**Lifecycle worker** (`snap-7f0196bc845038d96074fbcdd6e9c2ba`):

- 102 tests passed, 3 intentionally skipped.
- 500 repeated `ControlModeClient` start/stop cycles leaked no reader thread.
- It proved `_terminate_quietly` neither closes stdin/stdout nor waits for the
  child process.
- It found no explicit GTK output-buffer cap and no tracked setup-retry timer.
- Manual runtime evidence also showed repeated `BrokenPipeError` messages while
  disposable smoke transports finalized after sessions were terminated.

**Rendering worker** (`snap-5a430c42bdbf66f60e7e1580b8ddaa9a`):

- 75 focused tests passed.
- 100,000 random SGR streams were equivalent under whole-buffer and arbitrary
  two-chunk processing.
- Byte-at-a-time splits preserved valid colon RGB/indexed forms, reverse-video
  restoration, underline style, explicit background, and fail-open unknown
  color spaces.

### Review decision

Rendering changes are accepted as the Round 1 baseline and do not justify a new
parser rewrite in Round 2.  Lifecycle resource ownership remains below the
production bar.  Round 2 is therefore authorized only for bounded lifecycle,
transport teardown, scheduling, and retry ownership.

## Round 2 — bounded lifecycle and deterministic teardown

### Plan derived from Round 1 review

#### A. Transport termination contract

For every transport `T`, stopping must establish within a bounded interval:

```text
T.poll() != None
and stdin/stdout are closed or unreachable
and the reader thread is not alive.
```

Implementation will close the write side, request termination, wait boundedly,
use a kill fallback when supported, close remaining streams, and avoid waiting
on the current reader thread.  Tests will cover clean exit, stuck exit, close
failures, callback-triggered stop, repeated stop, and real subprocess reaping.

#### B. Retry ownership

For each pane-stream generation, at most one setup retry timer may exist:

```text
|R_setup(G)| <= 1
```

The controller will track the timer, cancel it on stop/generation change, and
clear ownership before requeueing.  Stale timers cannot keep a stopped
controller or worker alive.

#### C. Bounded pre-ready buffers

Pre-ready output and input will have explicit byte/count limits.  Output
overflow will not silently truncate terminal history: it will invalidate the
partial incremental buffer and force a fresh authoritative pane capture before
`READY`.  Input overflow will fail closed and report a bounded diagnostic rather
than accumulating unbounded user bytes or replaying an ambiguous suffix.

#### D. GTK scheduling reliability

Critical state/output delivery will use the existing primary-plus-fallback GLib
scheduler.  A double scheduling failure must clear the scheduling latch and
retry boundedly or surface a deterministic failure; it may not strand output
forever because one `idle_add` call failed.

#### E. Smoke teardown ownership

Disposable smokes will stop/destroy GUI stream controllers and drain GTK before
terminating their isolated tmux sessions.  The full smoke gate will reject
`BrokenPipeError`, leaked control clients, leaked Xvfb processes, and unexplained
stderr.

### Round 2 acceptance gate

1. Focused lifecycle tests, real subprocess teardown, repeated stop/reconnect
   stress, overflow tests, timer-cancellation tests, and GLib scheduling-failure
   tests pass.
2. All 16 runtime smokes pass with no `BrokenPipeError` and no leaked disposable
   process/session.
3. The full inherited suite, static checks, immutable hashes, live-runtime
   non-interference, and private-scope invariants pass.
4. An independent adversarial review identifies the highest-value Round 3
   rendering/property hardening work before Round 3 begins.

### Implementation completed

- `ControlModeClient` now closes its write side, requests termination, waits
  with a bounded grace period, uses a supported kill fallback, reaps the child,
  and closes all remaining streams.  Repeated stop/EOF races remain idempotent.
- `PaneStreamController` owns exactly one setup-retry timer per generation and
  cancels it on reconnect or stop.  Timer-allocation failure requeues through
  the existing worker path instead of stranding a connected tab.
- Pre-ready output is bounded by both bytes and chunk count.  Overflow never
  commits an arbitrary suffix; it retries from a fresh authoritative tmux
  capture.  Pre-ready input is bounded by bytes and chunks, accepts payloads
  atomically, and returns `False` instead of truncating an accepted queue.
- The GTK worker-to-main-loop backlog is bounded.  Scheduler failure clears its
  latch while preserving bytes for the next output/state edge.  Backlog
  overflow reconnects only the control transport and replays the persistent
  pane; it never kills the tmux task.
- Rejected input is reported once on the terminal surface until a later payload
  is accepted.
- Auth recreation freshness now uses the complete prior session/pane/process
  tuple.  tmux's one-second `session_created` resolution can no longer reject a
  valid same-second recreation.  Failed recreation cleanup waits boundedly for
  the exact process start-time identity to exit, become a zombie, or be
  replaced by PID reuse.
- Runtime smokes use lock-consistent control-mode readiness and current T1000
  surface names without synthetic GTK popup calls that produce warnings.

### Validation completed

- `117` focused lifecycle/stream/GTK tests passed before the auth-race repair.
- `23` focused auth-recreation/change tests passed after the full-identity and
  bounded-cleanup repair.
- Ten consecutive real GTK account-selector transitions passed with physical
  combo selection, exact private-scope verification, preserved cwd/tab, and
  empty stderr.
- Three consecutive 30-tab/10-window restore cycles passed with empty stderr;
  the original `BrokenPipeError` finalization traces did not recur.
- All 16 runtime smokes passed with empty stderr.  Exact Xvfb PID and live
  T1000 control-reader TID sets were unchanged before/after the matrix.
- The inherited suite passed with
  `1069 passed, 1 skipped, 44 warnings, 98 subtests passed`.
- Static compilation and `git diff --check` passed.  The live T1000 process and
  persistent tmux task namespace were not intentionally mutated.

### Round 2 adversarial review

Two sealed model-free workers attacked the exact Round 2 source snapshots.

**Lifecycle/resource worker** (`snap-c76db1a8feea43e8a86ab2825192ec6a`):

- `102 passed, 1 skipped, 137 deselected`, stderr empty.
- `2,000` repeated fake transport start/stop cycles closed both pipes, reaped
  every child, joined every reader, and leaked no `tmux-control-reader` thread.
- `12` real subprocesses that installed a SIGTERM handler were all reaped with
  closed stdin/stdout/stderr.
- Static evidence confirmed byte and chunk limits, single retry-timer ownership,
  primary-plus-fallback GTK scheduling, full auth identity, bounded cleanup,
  and transport close/wait/reap.

The lifecycle implementation is accepted.  Round 3 must not reopen or replace
it except for a demonstrated regression.

**Rendering/parser worker** (`snap-5dfb2019af41a74ea1eaa3be21045326`):

- `75` focused tests passed.
- `200,000` random inputs were idempotent and equivalent under arbitrary
  two-chunk processing.
- A complete SGR parameter containing `5,000` digits raised Python's bounded
  integer-conversion `ValueError`, proving the stream filter is not total over
  arbitrary pane bytes.
- The parser has no explicit complete-SGR size bound or numeric-token length
  bound.
- RGB channels above `255` are clamped internally even though VTE ignores the
  invalid SGR.  Indexed values above `255` already fail open.

Controller-side real VTE evidence refined those findings:

- VTE rendered both semicolon and colon C1-CSI (`0x9b`) samples using the normal
  default foreground at `15.5:1`; it did **not** interpret C1 as SGR.  Adding a
  C1 parser would therefore invent semantics and is rejected.
- VTE ignored indexed `256` and RGB `256` values.  The filter's RGB clamp,
  however, changed its state to white.  In the sequence
  `reverse + dark RGB -> invalid RGB 256 -> reverse off`, VTE retained the dark
  foreground while the filter believed it was white and skipped lifting.  The
  resulting normal text rendered at `1.023:1`.

### Round 2 review decision

Round 2 is accepted.  Round 3 is authorized for parser totality, exact invalid
value semantics, state-integrity recovery, property testing, and real-VTE
rendering boundaries only.  It may not broaden into transport, tmux, auth, or UI
redesign.

## Round 3 — total, semantics-preserving SGR filtering

### Plan derived from Round 2 review

#### A. Bounded total parser

Introduce explicit limits for one complete SGR and one decimal parameter.  All
numeric conversion goes through a non-throwing bounded helper:

```text
parse_decimal(token, maximum?) -> integer | invalid
```

For every byte string `x` and every partition `p`, the filter must satisfy:

```text
process(x) terminates without exception
process(x[:p]) + process(x[p:]) = process(x)
```

An oversized or structurally unsupported SGR passes through byte-identical and
taints only the state that cannot be inferred; it is never partially rewritten.

#### B. Exact color-domain semantics

- Indexed colors are valid only on `[0, 255]`.
- RGB channels are valid only on `[0, 255]`.
- Out-of-range values are VTE-invalid no-ops: preserve both bytes and the prior
  tracked color rather than clamping or replacing it.
- Unknown color spaces remain fail-open and mark that color field unknown.
- Background colors remain byte-identical; they are tracked only to calculate
  foreground contrast.

This distinction prevents invalid no-op syntax from corrupting the filter state
while preserving forward compatibility for genuinely unknown color spaces.

#### C. State-integrity model

Malformed whole-SGR syntax can affect more than foreground color.  Add an
explicit tainted-state path.  While tainted, no contrast rewrite occurs until a
full SGR reset (`0`) reestablishes foreground, background, and reverse state;
valid groups following that reset in the same sequence may then be interpreted.
Field-specific unknown colors recover through their matching reset or a full
reset.

#### D. Property and mutation-oriented verification

Add deterministic property coverage for:

- arbitrary multi-part chunking, not only one split;
- idempotence;
- giant numeric tokens and oversized complete/incomplete sequences;
- every indexed/RGB boundary around `0`, `15`, `16`, `255`, and `256`;
- separator mutations (`;`, `:`, empty color-space ID, explicit `0` ID);
- reverse-video transitions with valid, invalid, and unknown colors;
- malformed background/foreground combinations and reset recovery;
- bounded cache and pending-buffer behavior.

A small reference state model will compare the production scanner's decisions
for the supported SGR subset.  No new runtime dependency is permitted.

#### E. Real VTE evidence

Extend the pixel smoke to prove:

1. formerly invisible semicolon/colon RGB and indexed gray remain at or above
   `4.5:1`;
2. invalid RGB/indexed values preserve VTE's no-op semantics;
3. the reverse-invalid-range regression renders normal text above `4.5:1`;
4. explicit backgrounds and reverse video are not flattened;
5. C1 samples remain byte-identical and are not falsely treated as SGR.

### Explicitly rejected Round 3 changes

- No C1-CSI rewriting: current VTE does not interpret it as SGR.
- No clamping invalid RGB/indexed values.
- No background-color rewriting.
- No conversion of all terminal colors to white.
- No third-party parser or fuzzing dependency.
- No transport/lifecycle refactor after the accepted Round 2 review.

### Round 3 acceptance gate

1. The invalid-RGB reverse regression changes from `1.023:1` to at least
   `4.5:1` in a real VTE capture.
2. Giant/oversized/malformed SGR streams are total, bounded, byte-preserving,
   and exception-free.
3. Reference-model, property, mutation, focused, inherited, and all 16 strict
   runtime-smoke gates pass.
4. An independent adversarial review identifies and plans Round 4 packaging,
   observability, rollback, and installed-runtime work before Round 4 begins.

### Round 3 implementation completed

- The SGR scanner is total over arbitrary pane bytes. Complete sequences,
  decimal tokens, and retained partial escapes have explicit bounds; decimal
  conversion is non-throwing; nested `ESC`, `CAN`, and `SUB` cancellation is
  recognized; and oversized incomplete CSI/OSC/DCS sequences retain only a
  bounded grammar-state receipt while their bytes pass through unchanged.
- Oversized or structurally unknowable SGR taints the inferred terminal state
  instead of guessing. A full reset restores a known state, including valid
  groups following the reset in the same sequence.
- RGB and indexed colour values are valid only on `[0, 255]`. VTE-invalid
  values are byte-identical no-ops and preserve the previous tracked colour;
  they are no longer clamped into invented state.
- Unknown colour spaces remain fail-open. Backgrounds and reverse video are
  tracked for contrast calculation but never flattened or rewritten.
- `raise_contrast` now evaluates both black and white endpoints and chooses the
  shortest attainable blend. This closes the saturated-mid-tone hole where a
  luminance shortcut selected white even though only black could meet `4.5:1`.
- `scripts/check_ansi_contrast_properties.py` adds a deterministic independent
  reference model, arbitrary-byte chunking, mutation boundaries, cache/pending
  bounds, and a saturated-background contrast grid without a new dependency.
- The real-VTE pixel smoke now covers ten cases: semicolon/colon/faint/indexed
  lifting, invalid RGB/indexed no-op behaviour, reverse-invalid recovery,
  explicit backgrounds, reverse video, and unsupported C1 CSI.

### Round 3 validation completed

- `175 passed, 1 skipped, 8 subtests passed` in the focused rendering,
  lifecycle-boundary, and VTE suite.
- Controller property gates passed `100,000` arbitrary-byte cases, `20,000`
  independent reference-model cases, `248` boundary mutations, and `46,875`
  contrast-grid cases. Additional independent runs covered `100,000`
  whole/chunk byte streams, `20,000` grammar/state streams, and `139,968`
  endpoint-grid decisions.
- The real VTE rendered all formerly invisible cases at `4.53:1` on
  `#0a0d13`; invalid no-op cases retained the normal foreground at `15.5:1`;
  the explicit light background measured `19.447:1`; reverse video measured
  `19.028:1`; and C1 input remained byte-identical.
- All 16 strict runtime smokes passed with empty stderr. Exact Xvfb PID and the
  live T1000 control-reader TID sets were unchanged. The native-scroll smoke
  also passed ten consecutive stress repetitions.
- The full inherited suite passed with
  `1090 passed, 1 skipped, 46 warnings, 98 subtests passed`.
- Compilation and `git diff --check` passed. Source hashes remained immutable
  during the gates and the live T1000 process was not restarted.

### Round 3 adversarial review

Two final sealed, model-free workers reviewed independent snapshots of the exact
candidate. Earlier dispatch-only failures caused by read-only worker workspaces
were discarded; they did not produce product findings.

**Parser/property worker** (`snap-b6937a6ededfe0e802ae9a489ec60af1`):

- `107 passed, 8 subtests passed`.
- Its independent deterministic gate passed `50,000` arbitrary byte streams,
  `10,000` reference-model streams, `248` mutation cases, and `46,875`
  contrast-grid cases, with the `4096`-byte pending cap and `512`-entry cache
  cap intact.
- The source hashes exactly matched the controller-tested candidate.
- Evidence receipt:
  `f04bf2851d5e2da35088bd14d78f65546c1abd8a8afab6fa81b0e6922d436abb`.

**Real-VTE rendering worker** (`snap-5c663ec788735cb6ff2690c7da0780a7`):

- `144 passed, 3 skipped`.
- All ten real-VTE byte/pixel cases passed with the same contrast and
  byte-fidelity results as the controller gate.
- The source hashes exactly matched the controller-tested candidate.
- Evidence receipt:
  `5d61431ba2f9bdc23876d7f2e54497b4fb0899df107d30124fb07f8d4b5b8612`.

### Round 3 review decision

Round 3 is accepted. The parser and rendering path now satisfy the totality,
chunk-equivalence, state-integrity, boundedness, and real-emulator contracts.
Round 4 may not reopen them without a new reproducer.

The operational review found that the currently installed `t1000` is still an
editable checkout install: package metadata resolves directly to this repository,
the launcher is a generic `pip` entry-point script, and there is no immutable
build receipt tying the running installation to a commit, source tree, or wheel.
T1000 already provides mature `doctor`, redaction-safe `diagnostics`, desktop
integration, and shutdown-service primitives; duplicating those surfaces would
be a regression. The highest-value work is therefore release identity, atomic
versioned installation, rollback, and installed-runtime verification.

## Round 4 — content-addressed packaging, atomic install, and observability

### Plan derived from Round 3 review

#### A. Deterministic release identity

Use a non-self-referential two-level identity. A wheel cannot contain its own
SHA-256 without an impossible fixed-point construction, so the bundled record
binds the source and the external manifest binds the final artifact:

```text
B = (schema, version, source_commit, source_tree_sha256,
     source_date_epoch, build_mode)
build_id   = sha256(canonical(B))
wheel_hash = sha256(wheel_bytes)
release_id = sha256(canonical(build_id, wheel_hash))
```

The wheel bundles `B` and `build_id`; the canonical release manifest bundles
those values plus `wheel_hash` and `release_id`. The managed launcher verifies
both levels before import. Development/editable executions report an explicit
`development` posture rather than pretending to be a release, and installed
code exposes the same immutable record through read-only CLI/diagnostic
surfaces.

#### B. Reproducible, offline wheel construction

A release builder will:

1. require a clean Git commit by default;
2. export a controlled source tree rather than build from mutable caches;
3. inject deterministic build information before packaging;
4. set `SOURCE_DATE_EPOCH` from the commit;
5. build twice offline and require equal wheel SHA-256 values;
6. verify wheel paths, metadata, entry points, package data, and `RECORD` hashes;
7. emit a canonical manifest containing hashes of every release artifact.

Round 4 may exercise an explicitly labelled dirty-tree candidate, but Round 5
must rebuild from the final clean pushed commit.

#### C. Atomic versioned user installation

Managed releases live under a versioned, content-addressed directory instead of
mutating the checkout in place. Installation will:

- verify the manifest and wheel before extraction;
- stage on the destination filesystem;
- verify import, CLI identity, and read-only doctor checks in the stage;
- atomically publish the release directory and launcher pointer;
- retain the previous release pointer and immutable artifact for rollback;
- use a lock and compare-and-swap receipt so concurrent installers cannot
  interleave;
- never kill tmux, Codex, or the currently running GUI.

Rollback uses the same verification and atomic pointer swap. A failed install
leaves the previous launcher and running process untouched.

#### D. Extend, do not duplicate, observability

Integrate release identity into the existing `doctor` and redaction-safe
`diagnostics` payloads, and add a concise read-only release-status command. The
report distinguishes:

- editable/development checkout;
- unmanaged wheel install;
- verified managed release;
- missing, stale, or hash-mismatched managed metadata.

No environment secrets, terminal content, or account material enter the receipt.

#### E. Semantic runtime verification

Replace brittle equality of transient transport-client PIDs with a semantic
snapshot of persistent user state:

```text
S = {(session_id, tmux_target, pane_id, pane_pid/start_time,
      exact recovery UUID/cwd when present, auth scope ID)}
```

Install and activation gates require `S_before = S_after`; control-mode client
PIDs may legitimately reconnect. The verifier also proves the wrapper, imported
module, bundled build record, manifest, and release directory all name the same
`release_id`.

#### F. Round 4 acceptance gate

1. Unit and adversarial tests cover canonical serialization, source export,
   double-build reproducibility, wheel/`RECORD` verification, path traversal,
   symlink/special-file rejection, lock/CAS concurrency, interrupted install,
   atomic publish, rollback, and tamper detection.
2. A disposable isolated home performs build -> verify -> install -> invoke from
   outside the checkout -> rollback -> invoke, with exact release identities.
3. Existing doctor, diagnostics, desktop, CLI, all runtime smokes, and the full
   inherited suite pass without changed user-visible behaviour.
4. Live T1000 and persistent tmux/Codex semantic state remain unchanged.
5. A sealed adversarial review supplies the final Round 5 release/promotion plan.

### Explicitly rejected Round 4 changes

- No auto-updater, network dependency, package registry, or telemetry service.
- No replacement of existing doctor/diagnostics/desktop integration.
- No in-place mutation of the live checkout as the production install model.
- No destructive GUI restart during packaging tests.
- No claim that a dirty-tree candidate is the final production release.

### Round 4 implementation completed

- Added a canonical bundled build record and content-addressed release manifest.
  Development executions identify themselves explicitly as `development`; a
  production record binds version, source commit, source-tree digest, source
  epoch, wheel digest, and release ID.
- Added a deterministic offline builder. It snapshots one real Git worktree,
  rejects tracked links and special files, detects commit/worktree movement
  during capture, exports through no-follow regular-file reads, builds twice
  under fixed environment and epoch controls, and requires identical wheels.
- Added strict wheel verification: exact filename and dist-info identity,
  `Wheel-Version` 1.x, pure-Python `py3-none-any`, one exact console entry point,
  exact package/metadata/build-record versions, exact RECORD coverage/hashes,
  top-level allowlisting, traversal/link/special/encryption rejection, and
  bounded archive/member/manifest limits.
- Added a managed state schema that binds the selected release to the exact
  Python interpreter, bootstrap, stable launcher, manifest, previous release,
  generation, and preserved legacy launcher. State and manifests must use
  canonical JSON.
- Added a versioned content-addressed user installation. Wheel bytes are
  revalidated against their receipt before extraction; extracted files,
  artifact, and manifest are read-only, single-link regular files; the exact
  installed file set must equal RECORD; all files and directories are fsynced.
- Added fail-closed lock, no-follow input/output, compare-and-swap, and atomic
  activation. Bootstrap, state, and launcher publication is one semantic
  transaction with exact preimage restoration after any write or postcondition
  failure. Managed rollback and legacy rollback use the same transaction model.
- Added a self-verifying bootstrap. Before importing TermDeck it checks canonical
  state, interpreter identity, its own bytes, the stable launcher, release and
  manifest identities, wheel and build identities, RECORD, file set, file modes,
  hard-link count, and path containment. A concurrent state change causes a
  bounded re-exec rather than mixing two releases.
- Extended the existing CLI, doctor, and redaction-safe diagnostics with release
  build/verify/install/rollback/status and release posture; no duplicate health,
  desktop, or shutdown subsystem was introduced.
- Added a read-only semantic runtime snapshot. SQLite is opened with `mode=ro`,
  `query_only`, and one read transaction; no migration or directory creation is
  allowed. The snapshot binds registry/session/tmux/pane/process/recovery/auth
  identities while excluding transient control-client PIDs.

### Round 4 validation completed

- `63` focused build, wheel, transaction, rollback, CLI, doctor, and diagnostics
  tests passed. Tests cover path traversal, archive limits, invalid metadata,
  source and input symlinks, source movement, lock races, file modes, hard links,
  CAS failure, every activation-publication failure position, postcondition
  failure, backup escape, managed and legacy rollback repair, and tamper.
- The disposable workflow completed a deterministic double build, verification,
  installation into an isolated home, invocation and doctor from outside the
  checkout, member/launcher/bootstrap tamper rejection before import, launcher
  repair, and legacy rollback. It reported `63` installed members, no process
  restart, and an unchanged 281-row semantic snapshot.
- The full inherited suite passed with
  `1153 passed, 1 skipped, 45 warnings, 98 subtests passed`.
- All 16 strict GTK/VTE/tmux runtime smokes passed with empty stderr. Candidate
  hashes, semantic state, Xvfb PIDs, and the live T1000 reader-TID set were
  unchanged; no smoke process leaked.
- Compilation and `git diff --check` passed. The exact candidate file hashes
  remained unchanged through focused, full, smoke, and semantic-state gates.

### Round 4 adversarial review

The controller rejected two early worker programs because the disposable release
workflow intentionally requires a Git repository, while sealed worker snapshots
intentionally contain no `.git` directory. Their focused test steps passed; the
failure was a review-harness mismatch, not product evidence. Revised read-only,
model-free, network-disabled programs were then run against fresh exact snapshots.

**Release-integrity worker** (`snap-0c498c36ee16a67d2d270d099a6032f1`):

- `63 passed` across release manager, build identity, release CLI, doctor, and
  diagnostics.
- Source hashes exactly matched the controller-tested candidate.
- Evidence receipt:
  `071f203904e1d88bf103733c7dbb1280ce61fd07cc1495af144d2cc850b2895f`.

**Operational-integration worker** (`snap-c96d6a9b52160966dc4b27db9144ac0b`):

- `70 passed` across CLI, desktop integration, shutdown service, release CLI,
  diagnostics, and build identity.
- A distinct adversarial selection passed `29` install/rollback/tamper/link/lock/
  snapshot/status tests (`14` unrelated tests deselected).
- Source hashes exactly matched the controller-tested candidate.
- Evidence receipt:
  `3777122d098c554c6b9e267a5e5733c3d43c7923f8f361e73a808c80f4692534`.

### Round 4 review decision

Round 4 is accepted. The installed release is now independently verifiable,
content-addressed, rollback-capable, and transactionally activated without
changing persistent terminal tasks. No high or critical implementation finding
remains. The residual obligation is release promotion itself: the current tree
is deliberately dirty, therefore its successful dirty-candidate wheel is evidence
only and cannot be installed as the production release.

## Round 5 — clean promotion, independent replay, activation, and closure

### Plan derived from Round 4 review

#### A. Freeze the final release contract

- Promote the accumulated Unreleased work as version `0.2.11`; update the package
  version, bundled development record, changelog, and release notes together.
- Review the complete diff, instruction scope, source modes, package data,
  generated/debris boundaries, and secret scan. No acceptance threshold,
  retained feature, historical release note, or unrelated file may be weakened
  to obtain a clean commit.
- Capture a pre-promotion semantic runtime receipt and the running GUI/environment
  identity needed for a graceful restart.

#### B. Commit-addressed final verification

1. Commit the exact reviewed source tree.
2. Run focused release tests, deterministic property gates, the complete inherited
   suite, all strict smokes, compilation, diff/static checks, and semantic-state
   equality on the committed tree.
3. Require a clean Git worktree before production build.
4. Record the tested commit and all critical file hashes. Any post-test source
   edit invalidates the gate and restarts this sequence.

#### C. Reproducible production artifact and upstream identity

- Build twice offline from the clean commit; require equal wheel hashes and a
  `build_mode=release` record with the exact commit (no `+dirty`).
- Verify the wheel, manifest, RECORD, release ID, and artifact hashes using both
  the release manager and direct archive inspection.
- Push the exact tested commit to the private `origin`; verify the remote branch
  SHA equals the local SHA. The pushed commit, tested commit, and built source
  commit must be identical.

#### D. Non-destructive install and graceful activation

- Snapshot semantic runtime state immediately before install.
- Install the verified wheel/manifest through the managed transactional installer;
  verify release status and doctor from a directory outside the checkout.
- Prove install alone did not restart the running GUI or mutate persistent
  tmux/Codex/session identity.
- Gracefully stop only the T1000 GUI process through its supported shutdown path,
  preserving every tmux task, then launch the stable managed `t1000` entry point
  in the same desktop session. Verify restored windows/tabs, installed module
  paths, managed release ID, and semantic state.
- If activation verification fails, atomically roll back to the previous launcher
  or release and relaunch it; never kill persistent tmux/Codex tasks.

#### E. Independent final replay and closure

- Run a final model-free, network-disabled replay against the clean committed
  source/artifact identity: focused tests, artifact verification, outside-checkout
  invocation, and exact hashes.
- Repeat selected real runtime smokes after activation, including loading,
  contrast, scrolling, selection, auth isolation, geometry, and restore.
- Close `termdeck-n35`, `termdeck-gqy`, and the five-round release BR only after
  acceptance criteria, upstream SHA, installed release ID, runtime state, and
  rollback evidence are attached.
- Release all workers/leases, verify BR/BV health, finish the durable Goal, and
  leave the worktree clean.

### Round 5 hard gates

```text
tested_commit = pushed_commit = build_info.source_commit
clean_source_tree = true
wheel_build_1_sha256 = wheel_build_2_sha256 = manifest.wheel_sha256
installed_release_id = manifest.release_id
S_before_install = S_after_install = S_after_activation
all inherited tests and strict runtime smokes pass
no unexplained stderr, worker, Xvfb, transport, or GUI leak remains
```

### Explicitly rejected Round 5 changes

- No installation of a dirty-candidate artifact.
- No force push, public-repository change, or unreviewed history rewrite.
- No direct Codex launch, Codex worker, or termination of persistent tmux/Codex
  tasks.
- No `pip install -e` as the production activation mechanism.
- No deletion of the previous release until rollback and post-activation evidence
  are complete.
- No BR or Goal closure based only on unit tests or textual review.
