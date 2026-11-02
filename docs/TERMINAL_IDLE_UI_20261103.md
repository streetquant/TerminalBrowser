# Idle rendering and stable Windows navigation

This increment preserves the installed November 2 terminal responsiveness
rollout. It incorporates the complete existing v7 review candidate and its two
followups into local `main`, with additional cold-start and module-loading
refinements. BR task: `t1000-9lo`.

## Behavior

- The renderer watchdog parks after idle and resumes on rendering. Slow-renderer
  fallback, context-loss recovery and disposal remain enabled. Temporary GPU
  capability probes are released immediately; their Boolean result has a bounded
  per-document lifetime and is invalidated after failure.
- Layout requests coalesce within one animation frame, and disposal cancels
  outstanding work.
- The Windows page retains its rows, controls and search field. Inventory updates
  preserve focus on the exact **Kill window** button instead of moving it to the
  Open link. Search still includes all supported window/tab metadata, and a
  detached page can remount the same revision correctly.
- Windows presentation is a separate small module. The old implementation is
  removed from `workspace-ui.js`, reducing that asset from 65,404 to 62,373 bytes.
  Only the Windows homepage loads the new module; terminal routes have no added
  request. Initial inventory insertion remains a single DOM operation, with no
  eager search normalization when the query is empty.

The accepted input ordering, immediate idle dispatch, bounded queues, cached-tab
isolation, output handling and authorization behavior remain covered by the
existing tests. This change does not alter those protocols or the native runtime.

## Non-regression validation

The reference is source `2b1c4ab9e1e587a286eca68abc46e8ecf53e1319` and installed
web binary SHA-256
`1b175cdfac9b9d59be8c2c7f216b2ca0de070e749f3239032f0f2694d4cc5a0c`.
Comparisons use matched terminal geometry, browser, fixture and route. Initial
construction, steady refresh, switching and input echo are reported separately.
Network and headless-browser observations are not physical-display measurements.

The deterministic scheduling fixture runs eight retained views over 1,000
simulated display frames. Baseline: 8,008 watchdog callbacks; candidate: 136.
Both resume after rendering and leave no pending callbacks after disposal.
The candidate has no pending watchdog frame loop after settling. This measures
scheduling, not an equivalent CPU or typing-latency improvement.

In the 1,000-window browser fixture, the first comparison reduced median repeated
DOM refresh work from 78.9 to 1.2 ms (p95 105.2 to 2.7 ms). An additional run
measured 74.4 to 4.1 ms (p95 101.4 to 6.0 ms). The baseline created 240,000 elements
over 30 inventory updates; the candidate created none. The matching window beyond
the initial rows remains discoverable and destructive-control focus is retained.

An initial single cold-render observation was slower, so the implementation was
revised before acceptance. The final alternating cold-start run measured median
construction/layout at 1,000 windows of 8.45/55.3 ms before and 8.65/51.3 ms after.
At one window, construction median/p95 were identical (0.2/0.3 ms). At 16 windows,
median construction differed by 0.1 ms. Final repeated inventory refresh measured
70.1 to 0.7 ms at the median and 93.5 to 1.4 ms at p95.

Alternating baseline/candidate terminal runs include 120 observations per version:

| Headless observation | Baseline median / p95 | Candidate median / p95 |
| --- | --- | --- |
| Cached switch, including buffer observation | 5.65 / 7.2 ms | 6.1 / 7.4 ms |
| Input to fixture echo in xterm buffer | 11.9 / 13.1 ms | 11.5 / 13.3 ms |

These observations support no material regression under this matched workload;
they do not prove zero variation on every machine. Small sub-millisecond
differences and the separate cold-construction tails remain in the raw results.
Installed routes and physical clients are separate acceptance layers.

Source qualification:

- 87 frontend tests pass, with idle/resume/fallback/context-loss/disposal,
  retained focus, filtering, reordering, safe text and remount coverage.
- 152 Rust workspace tests pass; release compilation succeeds offline.
- 48 Python web-contract tests pass. Native Python files are unchanged here.
- Browser fixture: 42 behavioral assertions pass on the baseline and candidate,
  including current-snapshot admission, input, paste, Unicode, controller fencing,
  reconnect, management, keyboard focus, appearance and responsive layout.
- Ten application configurations pass: Bash, less, Vim, Neovim, nested tmux,
  mouse-protocol variants and top.
- 39 cached-tab/authentication/revocation checks and 12 native-scroll feedback
  checks pass.
- Pinned frontend build, syntax, asset-size and patch-whitespace checks pass.

Repository-wide `cargo fmt --check` also reports preexisting formatting in
recovery-observer and tmux-runtime code. This increment formats its new additions
without rewriting unrelated accepted source.

The browser fixture previously required a Live stream while deliberately
blocking the current availability snapshot. The installed baseline fails that
same stale assertion. The corrected fixture requires visible chrome without an
unauthorized stream, then measures readiness after the successful snapshot. It
retains total recovery time, including the normal retry interval, in the result.

## Preservation and activation boundary

Detailed, private evidence is retained under
`.termdeck/qualification/main-ui-20261103/`. It includes the original review's
verified complete Git bundle, candidate patch, binary/source hashes, browser
results and identity snapshots. The four original root database backups are
retained and excluded from publication.

Before activation the capture includes 308 saved sessions, 16 remembered windows,
290 exact tmux pane PID/start identities, three active authentication records,
eight protected password/TLS files and the shared tunnel identities. A normal
window-open timestamp changed during validation; no window identity or terminal
process changed. Preservation checks distinguish such live activity from loss.

The rollout uses the supported activation transaction and the previously
qualified web-only deployment-artifact procedure. All 84 installed Python modules
must remain byte-identical: unrelated source/native recovery work is not activated
by this release. The already failed saved-terminal recovery unit must not be
started. Existing tmux applications, databases, authentication, TLS and shared
Cloudflare routes remain preservation gates.

Installed verification and publication results are recorded after those gates
complete. Source tests alone do not establish deployed performance or physical
client acceptance.
