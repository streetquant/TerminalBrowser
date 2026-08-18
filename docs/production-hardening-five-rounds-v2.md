# T1000 post-0.2.11 production hardening — five-round ledger

This ledger records the second five-round production-hardening programme for
T1000. Each round is sequential: implementation and direct verification finish
first, an adversarial review then decides which findings survive, and only those
findings may authorize the next round. Existing VTE/tmux behavior, persistent
Codex tasks, per-tab `CODEX_HOME` isolation, semantic runtime state, rollback,
and private-upstream identity are immutable constraints.

## Round 1 — byte-faithful pane replay

### Reproduction and rejected hypothesis

The user reported repeated “crazy characters” in Codex output. The first
hypothesis was that the colon-form SGR contrast parser leaked fragments such as
`4::3`. Host-namespace capture of the exact screenshot pane `%220` refuted that
hypothesis: neither styled nor plain tmux capture contained literal `4::3` or
`4:3` text. The glyphs in tmux were valid Codex Unicode (`•`, `└`, `│`, `…`,
`─`), so stripping them would have corrupted legitimate application output.

A disposable real tmux/control-mode replay found the actual defect. Direct
capture emitted UTF-8 bytes such as `e2 80 a2` for `•`; initial T1000 replay
emitted `c3 a2 c2 80 c2 a2`, which VTE renders as mojibake. Live `%output` was
already correct. `ControlModeClient` uses Latin-1 as a reversible one-codepoint-
per-byte transport carrier, but `PaneStreamController._capture` encoded that
carrier as UTF-8 and `_build_replay` encoded it again.

### Implementation

- `_capture` now restores command-reply carrier strings with strict Latin-1 and
  returns byte lines.
- History and visible-screen replay are assembled entirely as bytes.
- The conversion boundary contains a concise comment describing why Latin-1 is
  an identity carrier rather than a text encoding choice.
- Unit tests cover UTF-8 glyphs, CSI/SGR bytes, backslashes, and arbitrary
  eight-bit values.
- `check_unicode_replay_smoke.py` compares authoritative tmux capture with pane
  replay and feeds the result through the real contrast filter and VTE widget.

### Direct verification

- Focused tests: `158 passed`.
- Full inherited suite: `1156 passed, 1 skipped, 98 subtests passed`.
- Strict runtime matrix: all `17` smokes passed with empty stderr.
- The real Unicode replay smoke preserved both history and visible UTF-8 and
  found no mojibake.
- The live managed GUI PID, its control-reader TIDs, the Xvfb PID set, the Git
  head/status, source hashes, and the 281-row semantic runtime snapshot were
  invariant across verification.

### Adversarial review

Two sealed model-free reviews passed the byte-level implementation:

- byte-fidelity review: `52 passed`, exact source hashes, and full 256-octet
  Latin-1 carrier round-trip;
- API review: `80 passed` and source inspection confirming the capture path no
  longer UTF-8-reencodes the carrier.

A third isolated integration run passed `144` tests with `3` intentional skips;
its sealed Xvfb step exited without diagnostic output. The identical immutable
workspace smoke passed when replayed by the controller, and the canonical strict
17-smoke matrix had already passed. This is classified as an isolated-worker
GUI harness limitation, not product evidence, and is not counted as an
independent VTE pass.

The API review sustained one material finding: `CommandResult.lines` is typed as
ordinary `str` even though command replies are Latin-1 byte carriers, and no
explicit byte accessor exists. Nine `.lines` consumers were found. The Round 1
local fix is correct, but another future binary consumer could repeat the same
UTF-8 mistake.

## Round 2 plan — make transport semantics explicit

Round 2 will remove that API footgun without changing the wire protocol:

1. add an explicit, tested `CommandResult.byte_lines` accessor that performs the
   strict reversible Latin-1 conversion;
2. route pane capture through that accessor rather than ad-hoc encoding;
3. document when `lines` is textual protocol metadata and when `byte_lines` is
   required for binary-preserving command output;
4. inspect every product consumer and prove that only the capture path requires
   raw bytes;
5. rerun focused, inherited, real tmux/VTE, and strict runtime gates before the
   Round 2 adversarial review.

The rollback boundary is narrow: remove the accessor and restore the Round 1
conversion in `_capture`; no session registry, release state, tmux task, Codex
process, or authentication scope is mutated by this refactor.

### Round 1 evidence correction

A later release-gate review found that the shell assertion used to identify the
managed GUI was too broad: `pgrep -f <bootstrap-path>` also matched the gate's
own command line because that literal path appeared in the script. Therefore the
Round 1 statement that a particular managed-GUI PID and reader-TID set remained
unchanged is withdrawn. This does not affect the source, unit, full-suite,
real-tmux/VTE, 17-smoke, Xvfb-leak, or semantic-state results. An exact
`/proc/<pid>/cmdline` argv-element scan subsequently proved that no managed GUI
was running during the corrected Round 2 direct gate. Exact argv identity plus
process start ticks, rather than substring process matching, is now a mandatory
release/activation invariant.

## Round 2 — explicit command-reply byte semantics

### Implementation

- `CommandResult` now documents that `lines` is a Latin-1 one-codepoint-per-byte
  protocol carrier, not decoded pane text.
- The immutable `byte_lines` property strictly reverses that carrier with no
  replacement, normalization, or implicit UTF-8 step.
- `PaneStreamController._capture` consumes only `byte_lines`; ASCII tmux metadata
  continues to use the textual `lines` surface.
- The product-wide consumer audit found no other command-result body requiring
  raw bytes.
- Tests cover all 256 octets, UTF-8 glyphs, CSI/SGR/OSC bytes, backslashes,
  success and error results, tuple immutability, and the capture migration
  boundary.

### Direct verification

- Focused tmux-control and pane-stream gate: `55 passed`.
- Complete inherited suite: `1159 passed, 1 skipped, 98 subtests passed`.
- All `17` strict GTK/VTE/tmux smokes passed with empty stderr, including the
  real Unicode replay smoke.
- A corrected exact-process direct gate passed with the 281-row semantic receipt
  byte-identical before and after testing; exact argv inspection showed no
  managed GUI before or after the gate.
- Static compilation and `git diff --check` passed.

The broad Round 2 wrapper itself returned non-zero only after every test and
smoke had passed: its final process assertion reused the same substring `pgrep`
mistake documented above. This is classified as a validation-harness defect, not
as product or test evidence, and it is not hidden or counted as a pass.

### Adversarial review

The explicit byte API closes the Round 1 recurrence path, but an independent
real-tmux command-reply challenge found a more severe framing defect. A pane was
made to display literal lines beginning with `%output`, `%message`, and `%end`.
Direct `tmux capture-pane` returned all lines. Through `ControlModeClient`, the
known notification-looking lines were dispatched as fake asynchronous events and
the literal `%end ...` line prematurely terminated the command reply, dropping
subsequent body data.

The failure mechanism is ordering and grammar, not encoding: `_handle_line`
recognizes `%begin`, `%end`, `%error`, and notification names before deciding
whether the line belongs to an active command body, and its frame-marker checks
accept any whitespace suffix. Real tmux frame markers have exactly three decimal
fields, for example `%end 1787009236 290 1`; arbitrary pane text does not acquire
protocol authority merely by sharing a prefix.

Disposition: Round 2 is accepted. Its API change is correct and regression-safe.
The command-framing finding is a distinct verified defect and authorizes Round 3.

## Round 3 plan — preserve percent-prefixed command body lines

1. Define and test the exact tmux reply-frame grammar: `%begin`, `%end`, and
   `%error` followed by exactly three decimal fields. Reject prefix-only and
   malformed lookalikes as framing tokens.
2. While a command reply is active, preserve every non-frame line as body data,
   including strings that begin with known notification names. Outside a reply,
   retain the existing asynchronous notification dispatch behavior.
3. Preserve greeting-block ownership, FIFO command pairing, timeout semantics,
   reconnect recovery, and real `%error` handling.
4. Add failing-first fake-transport tests for literal `%begin`, `%end`, `%error`,
   `%output`, and `%message` bodies, plus valid-marker and out-of-band event
   controls.
5. Add a disposable real-tmux fidelity smoke that compares direct capture with
   `CommandResult.byte_lines` and proves that no fake event is emitted.
6. Run focused, full inherited, property, and all strict runtime gates before an
   isolated adversarial review. No release or GUI activation is permitted until
   this parser boundary is accepted.

Rollback is local to control-line classification. No persistent tmux session,
Codex process, registry row, authentication scope, or managed release is changed
by the Round 3 implementation.

## Round 3 — exact reply framing and body ownership

### Implementation

- Frame control now requires a full-line `%begin`, `%end`, or `%error` record
  followed by exactly three ASCII-decimal fields.
- The opening identity is retained and only a matching exact end/error closes
  the block. Frame-shaped lines with another identity remain body data.
- Once a reply or greeting block owns the wire, every other line belongs to that
  block. Notification parsing resumes only after the matching terminator.
- Real tmux error diagnostics now come from the body; numeric terminator fields
  are no longer exposed as error text.
- The code documents the one unavoidable protocol ambiguity: tmux does not quote
  a body line that is byte-for-byte equal to its matching terminator, so the
  first exact matching record must keep protocol authority.
- A disposable real-tmux smoke compares `capture-pane` with
  `CommandResult.byte_lines` for notification names, frame lookalikes, Unicode,
  and ordinary text.

### Direct verification

- Focused control/replay tests: `59 passed`.
- Complete inherited suite: `1163 passed, 1 skipped, 98 subtests passed`.
- All `18` strict runtime smokes completed successfully with empty stderr,
  including real body-fidelity and Unicode/VTE replay.
- The body smoke produced identical direct/reply SHA-256
  `7c62e05a630e1a5c32c77d326c432c0744fc8ff1235b9023155165484aa2ea75`
  and emitted no fake notification.
- A separate exact postcondition gate confirmed the 281-row semantic snapshot,
  no repository smoke process, no mojibake, static compilation, source hashes,
  and working-tree identity.

The first aggregate postcondition wrapper finished every test and smoke but then
returned non-zero from one undifferentiated raw process-set equality assertion.
It did not identify which volatile field differed. The source and semantic
snapshots were unchanged and the exact follow-up gate passed. The wrapper result
is not counted as a pass; instead it is evidence that ad-hoc PID/process-state
shell assertions are too ambiguous for production activation decisions.

### Adversarial review

- Sealed state-machine review snapshot
  `snap-b4a7e4fdcdbc42ef017e1e29a9f474b5` passed `87` tests and `10,000`
  deterministic cases containing `80,000` percent-prefixed or arbitrary body
  lines with zero leaked events; receipt
  `eb7c61da2b6c607177c54473f77c964e25f248922552caaadc7416b848b88a60`.
- Sealed error/FIFO review snapshot
  `snap-9f66d862e802b3152e0c3f2199446514` passed `87` tests and independently
  verified greeting ownership, body-derived error summaries, outside-block event
  dispatch, and FIFO recovery after an error; receipt
  `6010ca5508319e1168f4e9cccd4000014b95d59ffff14936610e959c69fa7ecf`.
- An isolated real-wire worker passed its `59` tests but its sealed subprocess
  step exited without captured diagnostics. The identical source snapshot's
  real-tmux smoke passed when replayed directly in that worker workspace and in
  the controller's strict matrix. The failed sealed subprocess is retained as a
  worker-harness limitation and is not presented as independent real-wire proof.
- A later fresh **frame/parser review** snapshot
  `snap-daf261124053d25e807c1f6751ad1003` independently passed the final
  `59` focused tests, in-memory compilation, exact source hashes, and explicit
  accepted/rejected frame-grammar checks; receipt
  `2ece15ba16a4d1975b45949cba207a2e384d6a751a65cc04b3a0b946ccc6fd6f`.
- A later fresh **body-contract review** snapshot
  `snap-f9b3ab37993eee0108e6c6d239cbf954` independently passed `10,000`
  active-block cases containing `90,000` notification/frame-looking body lines,
  seven focused boundary tests, smoke-contract/hash verification, and AST review;
  receipt
  `51b74eb7def609cab099829eb656b23ab503c278b06b6cefbabd79aa98ccd00b`.

Round 3 is accepted. The sustained operational finding is not another terminal
parser defect: release verification and activation still have no canonical,
race-aware managed-GUI process inventory. Substring `pgrep` can match the
verifier itself, raw process-state equality is volatile, and `release info`
currently verifies files but cannot distinguish a stopped GUI, one exact managed
GUI, multiple managed GUIs, or a short-lived managed CLI invocation.

## Round 4 plan — exact managed-GUI process identity and observability

1. Add one read-only, caller-injectable `/proc` scanner to the existing release
   manager; do not create a second supervisor or process-control subsystem.
2. Match the canonical managed prefix as argv elements—not substrings:
   `[configured-python, "-I", configured-bootstrap]`. Resolve `/proc/<pid>/exe`
   and require it to equal the configured interpreter. A verifier carrying the
   same bootstrap path later in its own argv must never match.
3. Classify only actual GUI invocation forms: no application subcommand, explicit
   `gui`, and the supported default workspace form. Managed CLI invocations such
   as `release info`, `doctor`, or `sessions` are not GUI instances.
4. Bind every observation to `(boot_id, pid, start_ticks, uid)` and re-read the
   identity before returning it so PID reuse, process exit, or argv replacement
   cannot masquerade as continuity. Skip vanished/malformed entries and expose a
   fail-closed incomplete-scan posture when evidence cannot be made complete.
5. Return only redaction-safe evidence: posture (`stopped`, `single-gui`,
   `multiple-gui`, or `scan-incomplete`), count, PID/start ticks/boot ID/UID,
   canonical executable/bootstrap identity, argument count, and argv SHA-256.
   Never expose arbitrary workspace, recovery, account, or CODEX_HOME arguments.
6. Integrate this single snapshot into managed release status and `release info`;
   doctor and diagnostics must reuse that result rather than duplicate process
   discovery.
7. Add failing-first tests for absent, valid single, multiple, default/explicit
   GUI, verifier self-match, wrong prefix position, wrong interpreter/exe/UID,
   malformed stat/cmdline, vanished processes, PID reuse, and incomplete scans.
8. Add a disposable smoke that launches a harmless Python/`-I`/bootstrap-shaped
   fixture, proves the exact process is found, proves a later-argv substring
   lookalike is excluded, and reaps both fixtures cleanly.
9. Run focused, inherited, release-workflow, semantic-state, and all strict
   runtime gates, followed by fresh model-free adversarial review. No persistent
   tmux/Codex task or real managed GUI may be signalled, started, stopped, or
   restarted in Round 4.

Rollback is confined to read-only release observability. The accepted Round 1–3
byte/replay/framing implementation is frozen unless a new reproducer appears.

## Round 4 — exact managed-GUI identity and fail-closed observability

### Implementation

- The release manager now inventories managed T1000 GUI processes from an
  injectable procfs root without signalling or mutating them.
- A process qualifies only when its argv begins with the exact canonical managed
  interpreter, `-I`, and managed bootstrap path and its resolved executable is
  that configured interpreter. Substring lookalikes and managed CLI invocations,
  including the verifier itself, are excluded.
- Only parser forms that deterministically dispatch to the GUI are accepted:
  default invocation, explicit `gui`, and the supported top-level workspace form.
  Private database and workspace values are never returned.
- Every accepted identity is bound to the canonical Linux boot ID, PID, process
  start ticks, real/effective UID, executable, invocation kind, argument count,
  and SHA-256 of the raw cmdline. A candidate is read three times; PID reuse,
  exec, exit, permission failure, or torn observations make activation fail
  closed rather than manufacturing continuity.
- Proc directory symlinks and special entries, malformed same-UID records,
  invalid boot identity, and unreadable candidate records are explicit evidence
  defects. Ordinary unrelated processes and foreign-UID entries are ignored.
- Internal empty argv elements are preserved. Only the conventional final NUL is
  structural, preventing a malformed extra empty argument from masquerading as
  the no-argument GUI form.
- The result classifies `stopped`, `single-gui`, `multiple-gui`, or
  `scan-incomplete` and exposes a separate activation decision. Managed release
  integrity remains independent: multiple or stopped GUIs do not imply artifact
  corruption, and corrupted artifacts do not become acceptable because process
  posture is clean.
- `managed_release_status` and `release info` expose this one redaction-safe
  snapshot instead of duplicating shell process searches.

### Direct verification

- Focused proc/race/redaction surface: `23 passed`.
- Release manager, CLI, build-identity, and diagnostics surface: `82 passed`.
- Reproducible dirty-candidate release workflow passed double build, verification,
  install, tamper rejection, launcher repair, legacy rollback, outside-checkout
  invocation, and unchanged `281`-row semantic state.
- Full inherited suite: `1182 passed, 1 skipped, 98 subtests passed`.
- All `19` strict GTK/VTE/tmux smokes passed with empty stderr. This includes the
  new real managed-process inventory, exact tmux body fidelity, and Unicode VTE
  replay smokes.
- The complete gate preserved the ten candidate source hashes, Git HEAD/status,
  Xvfb set, exact semantic snapshot, and stable managed-process identity. It left
  no repository smoke process and observed no managed GUI before or after.
- The semantic snapshot remained `281` rows with receipt SHA-256
  `5b840a12df3d8fe00b59c42dfc8c3107553ffea3bdad9c0597618611548ac93e`.

### Adversarial review

The race/redaction snapshot `snap-b9877f0ef8e50e12d1a911acd6546c79`
independently passed the `23` focused tests. Its follow-up sealed replay checked
`20,000` deterministic argv round-trips, accepted and rejected CLI grammar
boundaries, exact source hashes, in-memory compilation, and absence of process
mutation APIs. A bytecode-producing compile step was rejected by the immutable
snapshot filesystem; it is a harness restriction, not product evidence.

The release-integration snapshot `snap-f790b6fb4241d929299e8261bfd1652b`
independently passed all `82` release tests, the real procfs smoke, exact source
hashes, in-memory compilation, redaction, required identity fields, and the
separation of artifact integrity from runtime posture. Its attempt to run the
full release builder was correctly refused because sealed worker snapshots omit
`.git`; the same exact candidate passed that workflow in the canonical Git
worktree.

No product finding survived review. Round 4 is accepted. The remaining work is
promotion of the reviewed source as an immutable release; no further terminal or
process-scanner redesign is authorized without a new reproducer.

## Round 5 plan — immutable promotion, rollback proof, activation, and replay

1. Advance the patch version consistently to `0.2.12` in `pyproject.toml`, the
   package version, development build identity, changelog, and release notes.
   Describe the UTF-8 replay correction, explicit byte API, exact tmux framing,
   and race-aware managed-GUI inventory without claiming unsupported behavior.
2. Rerun focused release/terminal tests, the complete inherited suite, every
   strict smoke, the isolated release workflow, static compilation, diff checks,
   semantic-state equality, and stable process identity after metadata changes.
3. Commit only the exact tested files. Verify a clean worktree, private upstream
   identity, commit/tree hashes, branch tracking, and then push that exact commit.
   No mutable build from a dirty or unpushed source is eligible for installation.
4. Build twice offline from the clean commit with `t1000 release build`; verify
   wheel, manifest, RECORD, bundled build identity, source commit/tree, and
   reproducible hashes. The packaged artifact must be exactly the tested commit.
5. Capture semantic runtime and managed-process receipts immediately before
   production mutation. Require a complete `stopped` or exact `single-gui`
   posture; refuse incomplete or multiple-GUI evidence.
6. Transactionally install the verified release while retaining `0.2.11` as the
   rollback target. With no managed GUI running, exercise the real managed-root
   rollback to `0.2.11`, verify it, reinstall `0.2.12`, and prove persistent
   semantic state is byte-identical throughout.
7. Re-scan procfs immediately before activation. If stopped, start exactly one
   stable managed launcher without touching tmux or Codex tasks. If one GUI is
   present, bind any graceful shutdown to boot ID, PID, start ticks, UID,
   executable, and cmdline hash and recheck before signalling. Never use name or
   substring process matching.
8. Verify the live managed GUI and installed artifact independently: exact
   release ID/version/module path, launcher/bootstrap/state hashes, one stable GUI
   identity, unchanged `281`-row semantic state, no CODEX_HOME or persistent-task
   regression, real Unicode/VTE replay with no mojibake, exact percent-prefixed
   tmux body fidelity, and clean stderr/resource teardown.
9. Run fresh isolated source/artifact and installed-runtime replays. Repair every
   sustained finding and rerun affected gates. Then close the Round 1–4 child
   obligations, the Round 5 promotion obligation, and the parent only after BR
   sync and final acceptance evidence are complete.

The production rollback point is the currently verified release
`6b1321e01cc7fd6addd14f7b7889af0fe1f4c66f3373fe831ee6e40e99108bf5`.
Persistent tmux/Codex sessions and per-tab authentication scopes remain outside
release ownership and may not be killed, recreated, or migrated by promotion.
