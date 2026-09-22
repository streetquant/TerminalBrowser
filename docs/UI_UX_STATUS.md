# Desktop and web UI/UX work

Audit date: 22 September 2026. This checklist concerns the source checkout;
production installation and validation are separate outcomes. Existing terminal
sessions, remembered tabs, authentication scopes, and working directories must
survive any release.

The cross-session handoff is [todo.md](../todo.md). Its active UI-session ID
matched this audit session. Completed colocated-path checks in that handoff are
not rerun here, and storage cutover is not part of UI validation.

## Commit and installation status

The implementation is committed: `ca1b34a` contains the colocated deployment
migration, `1c1e4fb` the desktop/web UI and performance work, and `69dcb40` the
release smoke and verification documentation. The clean v0.2.21 wheel builds
reproducibly and verifies all 78 members. No remote push was requested.

Installation is blocked before live activation. The migrated managed-state
file still identifies `/warp/runtime/termdeck/bin/t1000` as its stable launcher;
the installer explicitly rejects changing that binding in place. Recovery,
runtime-attestation, and preparer-broker services are absent. The privileged
tmpfiles template also needs an ownership review before applying it to the
now-shared `.termdeck` directory containing user state and managed releases.
Do not edit authenticated state hashes or apply that ownership change blindly.
The running backend and HTTPS edge remain healthy and unchanged. Complete
canonical production qualification and guarded activation remain outstanding.

## Current implementation checklist

| Priority | Work | State and evidence |
|---|---|---|
| 1 | Desktop layout, controls, focus visibility and compact-window usability | Implemented; long-label account selector bounded to 148 px (previously 328 px); visible keyboard focus; focused GTK tests pass. Actual terminal screenshots pass at 693×480, 1290×848 and 1440×900. The verified desktop minimum remains 693 px |
| 1 | Responsive browser shell, accessible navigation and narrow-screen controls | Implemented and qualified; mobile safe areas and touch targets, keyboard tab navigation, click-to-type and stable refresh DOM. All 22 integrated Chromium assertions pass, including desktop, compact and mobile layout |
| 1 | Reduce redundant browser terminal layout/rendering work | Implemented; redundant resize suppression, fit caching with font/layout invalidation, shared input encoder and listener cleanup. Three frontend regression tests and the integrated browser proof pass |
| 1 | Repair the performance CI command and make measurement failures fail the gate | Implemented; `tests/test_perf_probe.py` exercises workload selection, errors, invalid samples and budget failures |
| 2 | Prevent browser terminal performance and bundle regressions | Implemented; `npm test` and deterministic asset rebuild checks added to CI |
| 1 | Repair the release smoke's dirty-candidate installation failure | Implemented and verified end to end using a disposable committed source snapshot; the production clean-source gate remains enforced |
| 2 | Validate rendering and terminal behavior without touching live sessions | Complete for this source pass; isolated desktop, recovery, release and browser checks pass. Installed-release qualification remains separate |

## Performance evidence and limits

The former CI command supplied `--sessions` and `--assert-thresholds`, which the
probe did not accept. The probe now accepts both options, reports its workload and
scope, rejects missing or invalid measurements, and exits unsuccessfully on probe
errors or requested budget failures. CI measures 100 synthetic sessions over 200
iterations instead of attempting five iterations with unsupported arguments.

The headless p95 CPU/model budgets are 8 ms for snapshot construction, 0.1 ms for
the frame monitor, and 8 ms for the synthetic line-buffer operation. These are
absolute regression budgets; they do not assert terminal, network or display
latency and do not establish that this application is faster than other terminals.

Current 200-sample measurement: snapshot construction 0.762104 ms p95; frame monitor
0.000360 ms p95; synthetic line buffer 0.486355 ms p95. No model optimization is
claimed from this run. Host storage pressure was high during this audit, so
interactive benchmarks must include their environment and sample counts.

The final disposable Chromium run measured eight tab switches: 128.466 ms p50
and 139.404 ms p95. One keyboard-dispatch-to-terminal-buffer echo took 66.236 ms;
terminal-buffer readiness took 1591.425 ms from navigation. These timings include
browser automation and fixture overhead. They are not physical display latency,
a broad performance distribution, or a before/after speed comparison.

## Larger remaining work

- Support desktop windows below 693 px without degrading VTE rendering. The
  hidden inline-rename entry contributes to the header minimum. A smaller
  requisition admitted a real 640 px window but produced blank terminal output
  in the compact smoke; that experiment was discarded. The current layout
  preserves the verified terminal rendering instead.
- Repair and recheck the installed release identity before promotion. The source
  deployment-path gate now passes after concurrent migration edits in the shared
  worktree. However, the current `.termdeck/bin/t1000 release info --verify`
  exits 78: `managed bootstrap path mismatch`. The backend and LAN services are
  running, but `termdeck-web-recovery.service` is not installed. Source-path
  consistency is not evidence of a repaired installed release or rollback path.
- Complete representative end-to-end latency measurement for local GTK and web:
  typing-to-rendered-output, tab switching, new-tab readiness, scrolling, resizing,
  and idle CPU with many tabs. Headless model probes are only one part of this.
- Qualify a GTK4/VTE migration separately. GTK4 is available on this host, but
  `gi.require_version("Vte", "3.91")` currently fails because the GTK4 VTE binding
  is unavailable. Installing that dependency alone would not complete a port.
- Stage and validate an installed release through the managed release workflow,
  including rollback and unchanged live-session identities, before switching the
  running desktop or browser service to the new build.

## Verification

The combined focused run under Xvfb and a disposable D-Bus session passes:
195 unique tests and 32 subtests covering performance instrumentation, actual GTK
long-label allocation, lifecycle, recovery, focus, clipboard, resize and theme
behavior. The corrected CI performance command also passes.
`npm test --prefix web/frontend` passes all three terminal-layout and listener
lifecycle tests. Their fit-call counts demonstrate avoided model work, not a
measured improvement in physical display latency.
Rebuilding the embedded terminal assets reproduces all three files byte for byte;
the source asset-size, dependency-pin and manifest-hash checks pass.
The disposable Tabs-pane and window-restore smokes pass, including 30/30 restored
tabs across ten windows, foreground working-directory preservation and dead-pane
recovery. An additional focused run of performance-probe, visual-smoke-harness and
release-workflow tests passes 39 tests and six subtests; this overlaps the earlier
run and is not an additive total. `check_visual_polish_smoke.py` passes.

The final desktop screenshots use an isolated runtime directory and wait for
three actual tmux panes to emit fixture markers. This fixes the previous smoke
harness's false readiness signal.

A fresh Chromium layout probe verifies that inserting a refresh-error notice
does not change terminal geometry or introduce page overflow at 1440×900,
980×620, 430×740 and 320×568. It exercises the actual stylesheet with a small DOM
fixture, not live terminal transport. The former error notice became an extra
grid row and collapsed the terminal despite its connection remaining live.
The corrected notice is positioned over the existing terminal grid area.
The browser runner initially timed out using its bundled Node executable; the
same probe passes with `PLAYWRIGHT_NODEJS_PATH=/usr/bin/node`.

The final integrated Chromium proof passes all 22 assertions against the rebuilt
server. It covers real tmux/WebSocket terminal I/O, keyboard control bytes,
bracketed paste, mouse protocol, alternate-screen and Unicode buffer state,
canonical geometry isolation, desktop/compact/mobile layout, reload and server
restart recovery, writer ownership, and the complete disposable tab lifecycle.
New persistent checks verify keyboard tab focus after terminal readiness,
click-to-type focus after repeated pointer selections, and open-menu focus across
metadata refresh. They caught and protected two additional focus-race fixes.

Proof artifact: `/tmp/td-ui-qualified-S4owuB/result.json`.
Screenshot: `/tmp/td-ui-qualified-S4owuB/terminal-browser-proof.png`.
Qualified server SHA-256:
`c888e8576e6015db33843d5cc55b462e2e3a1167269987315d2d45c4cd9c2265`.
These are disposable local proof artifacts, not a production installation.

The release workflow smoke passes with 78 verified installed members, tamper
rejection, launcher repair, doctor identity and legacy rollback. Its install and
rollback operate only inside a temporary fixture; they do not establish that the
running installation is repaired. All 28 focused release-workflow tests pass.
