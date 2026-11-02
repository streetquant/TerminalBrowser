# Web UI control-retention performance pass — 2026-09-30

## Outcome and boundary

The tab strip and sidebar list now retain controls by pane ID instead of rebuilding their DOM on every selection. This is a source-qualified change in `/warp/projects/softwares/termdeck`, not a production deployment or a claim that all UI slowness is resolved. No live terminal commands, production service restart, authentication change, session deletion, or external-path source write was performed by this pass. Existing terminal-session caching and transport code were left unchanged.

## Implementation

`web/crates/termdeck-web-server/assets/app.js` adds the shared `T1000TabChrome` renderer. Selection changes within the same normalized snapshot update the old/new selection and keyboard-focus attributes. Snapshot changes reconcile rows by ID, update changed visual fields, remove stale rows, and reorder only when needed. Event handlers retain the latest pane metadata rather than stale captured objects. Rename, pin/unpin, hide, context actions, pending-action state, accessibility attributes, and focus preservation remain covered by tests. Tab focus lookup uses the ID map rather than scanning all tab controls.

`web/crates/termdeck-web-server/assets/workspace-ui.js` caches the pane lookup and first 100 matching sidebar rows until the snapshot or search query changes. Selecting another pane does not rescan/filter the entire snapshot. Details for the active pane remain available even when it is outside the first 100 rows. Detail action handlers use current metadata after refreshes. The small active-pane detail panel can still rebuild when its displayed pane changes; the zero-child-list-mutation result below applies to the tab strip and sidebar list, not every UI element.

The fast path relies on the current application's immutable normalized snapshot arrays. An in-place mutation added by future code would require explicit invalidation or a revision key. Initial rendering and new-snapshot reconciliation still scale with pane count; this change does not virtualize the tab strip or make the entire terminal-switch path constant-time.

## Verification actually performed

The complete frontend Node test suite passed: 44 tests, zero failures, cancellations, or skips. Eleven tests in the new `web/frontend/test/tab-chrome.test.mjs` cover both shared-renderer modes and the production sidebar update function. Checks include no pane traversal on selection, DOM/control retention, current metadata, filtering, hidden-sidebar refresh, selected off-list details, pending state, reorder, empty transitions, focus, opaque IDs, and the per-asset size limit.

The new `scripts/check_web_tab_chrome.py` executed 14 additional behavior checks in Chromium 153.0.8010.52. These checked retained nodes, focus, selection/tabindex, fresh session metadata, rename/pin, disabling and reenabling hide controls, reorder, filtering beyond 100 rows, empty search results, off-list details, current detail actions, context selection, and clearing orphan controls.

Both edited JavaScript assets passed `node --check`; `git diff --check` passed. Asset sizes were 44,924 bytes for app.js and 65,123 bytes for workspace-ui.js, both below the existing 65,536-byte cap. The latter has only 413 bytes of remaining headroom.

## Browser comparison

Metric: execution of the production tab-strip and visible-sidebar render functions plus forced synchronous layout, in milliseconds. Production CSS was used with synthetic panes and a disposable browser document. There was no terminal connection, network interaction, public-origin access, or physical display measurement. Each case had eight warmup iterations and 60 measured iterations. Baseline/candidate order was reversed for the 100-pane case. These are descriptive measurements from one machine/run, not confidence intervals or a guaranteed client speedup.

| Pane count | Baseline median | Candidate median | Baseline p95 | Candidate p95 |
| --- | ---: | ---: | ---: | ---: |
| 100 | 9.3 ms | 1.6 ms | 10.7 ms | 2.0 ms |
| 1,000 | 56.7 ms | 3.1 ms | 72.8 ms | 4.2 ms |

For both multi-pane cases, the candidate produced zero tab-strip/sidebar-list child-list mutation records across 60 selections and retained all original strip rows. The baseline produced 120 child-list mutation records and retained none. The one-pane boundary case also passed; its sub-millisecond candidate timings reached the browser timer's resolution, so no zero-cost or infinite-speedup conclusion is drawn.

## Evidence and reproduction

Persisted evidence, confirmed by native read after the successful operation completed:

- `.termdeck/qualification/ui-chrome-20260930-maxcloud/browser-report.json`: raw samples, hashes, browser/platform information, load averages, and browser checks.
- `.termdeck/qualification/ui-chrome-20260930-maxcloud/frontend-tests.log`: complete Node test output.

The successful managed operation was `opref_9184c60413f70ca91a5bd1120d5a9ae4`, completed at 2026-09-30T16:54:37.984Z with exit code 0 and no stderr. Its complete stdout SHA-256 is `debc93c80aca738a9a343d31e2e31dfb8cfd77eda2e8297d0f68c26e39540f33`. The benchmark timestamp is 2026-09-30T16:52:49.576444+00:00. Earlier incomplete/failed command attempts are not acceptance evidence.

The baseline was extracted from Git at `611bd9a4e30e7ad9df9c63f2f8dc2e16f1d44d80` and verified against the exact pre-edit source hashes before running. A separate fresh execution also confirmed that both current candidate hashes still match the measured files:

| File | Baseline SHA-256 | Candidate SHA-256 |
| --- | --- | --- |
| app.js | `76f76b6e121774a25d4a9a141013545ce46b17604bbbc240df8ab7a1af431c97` | `7426d79214c99d710454d5712a8d83450cbb824f0d18ac1d61937c9a30f6f582` |
| workspace-ui.js | `31adb89023b60d9f075081c404d64bc9bfe18297ac6ec06ac906a9fb4d99318b` | `225cf4d0622ccd91e14d3fdffdccc9b70e57ecccfa1c2c8661ab255d4fca7af9` |

From the workspace root, rerun:

```sh
node --check web/crates/termdeck-web-server/assets/app.js
node --check web/crates/termdeck-web-server/assets/workspace-ui.js
node --test web/frontend/test/*.test.mjs
python3 -B scripts/check_web_tab_chrome.py \
  --baseline .termdeck/qualification/ui-chrome-20260930-maxcloud/baseline \
  --output .termdeck/qualification/ui-chrome-20260930-maxcloud/browser-report-rerun.json
git diff --check
```

The browser runner uses a short, unique workspace-local temporary directory for profile/cache state and removes only that owned directory. This avoids the Unix-domain socket path limit without disabling the browser sandbox.

## Coordination and remaining acceptance

The repository instructions and `develop-with-br-bv-cass-cm` skill were read. BR all-status records and BV were consulted, and the shared coordination check reported a healthy BR database with BV using the same repository. This work extends `t1000-web-terminal-source-latency-ier`. Native CASS/reviewed-EE retrieval was attempted but blocked by the client; this pass does not claim new memory evidence or manufacture reviewed guidance.

A remaining coordination-persistence blocker is explicit. An initial guarded BR reopen/claim appeared successful inside its managed command, but a fresh `br show` in operation `opref_86768a52ff7e54644d05b9872772c8e6` still returned the historical closed record and seven criteria. A guarded retry, `opref_80bc08670635db9d0fd10020c2a24abf`, performed reopen, claim, an eighth source-only acceptance criterion, evidence notes, acceptance check, close, and explicit `br sync --flush-only`. It read back the intended closed record inside that execution. However, CloudMCP reported `contained runner exited with code 1` alongside command exit code 0, and a native uncached file read after completion still showed the unchanged historical JSONL record (552,434-byte file; source task on line 54, historical owner `codex-tab-switch-20260930`). Therefore durable BR completion for this follow-up is NOT claimed. The ledger update must be reconciled through authoritative BR after the execution/writeback issue is resolved; do not edit the database or JSONL manually. This document and the source/test/evidence files were separately saved and read back using native workspace file tools, so their persistence is independently established.

Production activation, physical-client latency, terminal attach/input/scroll latency, large initial snapshots, and broader whole-UI responsiveness are not certified by this isolated rendering benchmark. Qualify and activate through the repository's existing release procedure before attributing a live-client improvement to this patch. Preserve live sessions, saved windows/tabs, state, credentials, and unrelated worktree changes. No blanket no-regression claim or closure of the broader performance/physical-client work is warranted.
