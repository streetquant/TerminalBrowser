# t1000 UI/UX and performance status

Updated 22 September 2026. This reports source changes, not a live deployment.
The user subsequently approved safe live repair. Installation is blocked by the
privileged preparation and ownership issues below, not by missing user approval.
The original Cloudflare deferral was lifted by the user on 27 September 2026 for
`https://maxchat.semiotech.org/t1000/`; consult live BR and the
[BR/BV handoff guide](BEADS_WORKFLOW.md). The source observations below do not
establish the current production service or saved-session state.

## Current interface

The desktop app is the visual and interaction reference. The web app now uses
its exact chrome/terminal token families, a compact searchable window list, and a desktop-style
Tabs pane. The default is flat charcoal with an emerald accent; ink/blue,
paper/cobalt and system themes are also available. Rust/xterm and GTK/VTE were retained:
the measured problems did not require a stack replacement. Real interaction
checks qualify behavior; screenshots qualify only the observed layout.

Design self-review: **8/10, subjective, not user approval**. The refactoring-ui
skill informed the restrained color/spacing hierarchy, flat tabs, visible focus,
responsive controls and confirmation defaults. The remaining gap to 10 is
physical-client font/contrast and interaction review with the user, plus live
deployment validation. Feature tests and screenshots are not a design acceptance
score. No overall production completion is claimed.

## Desktop/web parity

| Capability | Current source result |
| --- | --- |
| Password-only login | One password field; automatic browser name. Initial setup confirms the password. Real form submission, incorrect passwords and mobile layout verified |
| Terminal I/O | Real tmux/WebSocket input, ANSI, alternate screen, Unicode buffer state, paste, mouse, replay and reconnect qualified |
| New tab | Working directory, optional name/command and pin state; opens the real new terminal |
| Rename / kill | In-app dialogs with keyboard focus, safe Cancel default for kill and persistent failures; successful kill removes the tab and stops its process |
| Pause / resume | Real fixture process status changes; status-correct menu |
| Pin / unpin | One correct action, pin indicator, additive snapshot field and revision binding |
| Hide / reopen | Hide preserves the process. Hidden tabs are scoped to this window; reopening preserves the exact pane PID and requires CSRF |
| Search tabs | Name/directory filter with keyboard navigation and direct selection |
| Command palette | New tab, hidden tabs, find, appearance, shortcuts, rename, hide and tab results |
| Find in terminal | Actual xterm search, previous/next, selected match and no-match feedback |
| Appearance | All seven exact desktop terminal palettes; installed/custom fonts, maximum text size, cursor color/shape/blink, scrollback, reset and persistence; the active browser writer sizes its tmux window to the chosen font and viewport |
| Shortcut help | New tab, search, palette, find, switching, rename, context actions and Escape |
| Window overview | Searchable list; refresh preserves query and keyboard focus |
| Create / rename windows | Durable empty-window creation and rename, resolved through canonical routes; no process restart |
| Move / reorder tabs; bulk hide | Cross-window moves preserve PID/tmux identities; drag and Ctrl+Shift+PageUp/PageDown reorder; stale edits fail without partial changes; Hide others/right with Cancel and pin protection |
| Desktop Tabs sidebar | Basic single-tab pane implemented: filter, tab/cwd details, navigation, rename/pin/hide, left/right placement, hide/show and layout persistence; compact screens use an overlay |
| Tab manager | Multi-selection, global sorting/search, window/history filters, 100-row pages, live PID/state/health/CPU/RAM/process-count/runtime/shortcut/command/directory columns, redacted preview, open/rename/move and bulk lifecycle actions |
| Manager input | Explicitly confirmed Send text, Send + Enter and Status request; exact targets, controller/sequence receipts, no uncertain-input retry, normal keyboard control returns afterwards |
| Interface settings | Four chrome themes, density, interface font/scale, sidebar placement/visibility and reduced motion |
| Desktop settings exchange | Explicit import and consent-gated save, revision checks and preservation of unrelated config. Defaults apply on next desktop launch; no running session is restarted. Client-installed fonts still determine browser rendering |

The requested management and appearance controls are implemented, not placeholders.
Production activation and physical-client validation remain open in BR
(`t1000-deploy-xeh` and `t1000-physical-qzr`). `todo.md` is retired.
This does not claim parity with unrelated desktop process-monitor, recovery or
Codex-captain subsystems; those are outside this UI/UX task.

## Root causes repaired

- `Referrer-Policy: no-referrer` stripped Chromium native password POST Origin,
  causing authority rejection. `same-origin` preserves local form provenance
  while suppressing cross-origin referrers. Host/origin/CSRF gates remain strict.
- Native browser Rename/Kill prompts were replaced with accessible dialogs.
  Action failures now survive metadata refresh and reconnect instead of vanishing.
- Search inputs consumed Escape without closing the dialog. Explicit handling
  now closes it; tool shortcuts are verified not to leak bytes into the PTY.
  Search results wrap correctly with Up/Down, and selecting or reopening a tab
  returns focus to its terminal rather than the toolbar.
- Missing pin metadata caused simultaneous contradictory Pin/Unpin controls.
- HTTP text paste held its stdin pipe open while waiting for `tmux load-buffer`
  to exit. Closing the pipe after the bounded write fixes the deadlock. A 64 KiB
  subprocess regression verifies EOF and duplicate acknowledgment without
  repeated input; real browser checks verify two actual fixture terminals.
- Manager CPU sampling previously had no preceding counter in a fresh helper
  process. Two bounded samples now provide a real delta instead of permanent
  misleading zero. Telemetry refresh runs only while the manager is open/visible.
- Large interface scale now expands chrome rows; tabs fit the strip instead of
  clipping their active underline. Terminal host backgrounds follow the terminal
  palette, eliminating mismatched blocks after appearance changes.
- Refreshes that changed selected-tab metadata rebuilt the action menu and
  discarded its focused control. Menu nodes now remain mounted and update in
  place; the browser regression changes pin state externally through the
  disposable CLI while verifying exact focus/node preservation.
- Web ANSI colors had drifted from the desktop palette. Every built-in palette
  now matches the desktop definitions, guarded by an exact cross-client test.
- Desktop compact replay materialized full-row padding and trailing empty lines,
  causing VTE reflow to move visible content away. Normal replay now uses its
  existing full-screen clear and trims synthetic trailing blank rows. Styled
  blanks and alternate-screen behavior remain preserved. The hidden rename entry
  no longer imposes the old 693 px minimum.

## Performance work

- Repeated unchanged window snapshots skip row construction and DOM replacement.
- Tabs-pane, tab-picker and hidden-tab results are bounded to 100 rendered matches, with
  search over the full bounded list, rather than thousands of controls at once.
- Workspace tools are a separate same-origin immutable v6 asset; each small
  frontend asset retains the 64 KiB cap. Transport/auth code still does not use
  browser storage; only non-sensitive appearance and pane-layout preferences are persisted.
- Existing terminal layout caching, redundant resize suppression, shared input
  encoding, bounded input/output queues and subscription cleanup are preserved.
- Appearance changes invalidate font metrics and resize the active browser writer's tmux window; passive viewers do not change its grid.
- Management work is outside the terminal transport path. Tables render 100 rows
  per page, search is debounced, and automatic refresh preserves selection/focus.
  Moves and reorder use exact IDs and a database transaction, not terminal respawns.

## Verification and evidence limits

Two complete browser runs passed **39 assertions each**, with zero page errors,
after the final application-source change:

- `/tmp/t1000-ui-web-proof-20260922-parity-qualified/result.json`
  SHA-256 `de001437026a104701c906f4257525daecf8a60cf42e2b566acb076166f2f880`
- `/tmp/t1000-ui-web-proof-20260922-parity-qualified-repeat/result.json`
  SHA-256 `e043286b6d1951d97cb2e81b5acce65c592845221fe36bf5c5c4e47316cdd312`

Both used debug server SHA-256
`225f94a00ce2a184314d35c733c72ff8a93754242c012f00b14d18bb7315fd2a`,
private authenticated backends, tmux sockets, registries, desktop configuration
and browser profiles. All send/kill/remove tests target disposable Python terminal
fixtures, never production work or a real nested Codex process.

The matrix covers the original terminal/reconnect/ownership checks plus actual
window creation/rename, move/reorder, bulk lifecycle, pin protection, cancel,
preview, text/Enter delivery, keyboard handoff, settings import/save/persistence,
and 320 px/max-scale layout. Moves/reopens preserve actual process identities.
The settings write test reads the fixture's real desktop config afterwards.

Expected offline/denied-action console errors, intentional killed-pane disconnects
and headless software-WebGL warnings remain in the evidence. Each full run records
one cancelled manager `list` read during navigation. Only exact `ERR_ABORTED`
telemetry reads receive that classification; failed reads and cancelled writes
still fail acceptance. Seven negative/positive contract tests protect this rule.
This is not an empty-console or zero-network-cancellation claim.

Inspected screenshots include `workspace-charcoal.png`, `tab-manager-desktop.png`,
`tab-manager-mobile.png`, `settings-light.png` and the 320 px/1.75-scale layout.
The host lacks a normal scalable CJK font: Unicode buffer correctness is verified,
but screenshots do not prove complete CJK glyph coverage on all clients.

Earlier focused Python run: **216 tests + 23 subtests passed**, covering replay, actual GTK
layout, tabs, terminal mouse/focus, VTE attachment, shortcuts, themes, branding and
contained resize. Actual visible VTE text survives 640×480 → 980×620 → 640×480.
GTK visual-polish, scrolling and Unicode/replay smokes pass under isolated
Xvfb/D-Bus/private runtime directories. Backing tmux output alone is not accepted
as proof that the VTE is visibly rendered.

Latest focused Python run: **253 tests + 14 subtests passed**. It covers management transactions, stale edits,
partial-action failures, pin protection, stopped-record safety, desktop settings
revision checks, malformed settings, palette parity, contrast, replay, CLI,
session lifecycle and smoke contracts. The only warning was an existing GLib
deprecation.

Rust core/state/server/authentication: **102 tests passed** (27 + 5 + 41 + 28 unit
tests and one auth-directory-move integration test). Strict workspace Clippy
passes. Three frontend tests pass, including palette/font validation, appearance bounds and grid
preservation. The full web smoke passes after the last application change and
recognizes existing Cargo integration tests
as qualification targets, while still rejecting binary/build-script/proc-macro
authentication targets; focused tests protect that distinction. Its
workspace-tools asset hash matches the final source:
`4bce903560738e649a2a2e369c4b332fe52a6582d611cd4ef59bba2afd98315b`.

The final fixture measured one keyboard-to-terminal-buffer echo at 68.631 ms;
eight switches had p50 127.876 ms / p95 144.690 ms. Terminal readiness was
719.265 ms. A 190-byte typing burst with deliberately delayed 120 ms acknowledgments
completed in 188.651 ms. These include automation/fixture overhead, not physical
display latency or a before/after speed comparison.

The 200-sample/100-session model probe passed: snapshot p95 0.755462 ms, frame
monitor 0.000340 ms, synthetic line buffer 0.487017 ms. Broad local/physical-LAN
typing, scroll, resize and idle-CPU distributions remain unmeasured. No claim of
universally fastest performance or full application-wide regression coverage.

### Reproduction

Run from the repository root; browser fixtures clean up only their own processes
and temporary state:

```sh
python3 -m pytest -q tests/test_web_management.py tests/test_web_appearance.py tests/test_web_desktop_parity.py tests/test_web_smoke_contract.py tests/test_ui_theme.py tests/test_ui_theme_contrast.py tests/test_settings.py tests/test_pane_stream.py tests/test_session_lifecycle.py tests/test_master_tasks.py tests/test_task_snapshot.py tests/test_window_tab_model.py tests/test_cli.py
npm test --prefix web/frontend
python3 scripts/check_web_readonly_smoke.py
cargo build --manifest-path web/Cargo.toml -p termdeck-web-server
python3 scripts/check_web_terminal_browser.py --server-binary web/target/debug/termdeck-web-server --output /tmp/t1000-ui-web-proof-new-run
python3 scripts/perf_probe.py --iterations 200 --sessions 100 --assert-thresholds
git diff --check
```

The full web smoke includes locked workspace tests, formatting, strict Clippy,
frontend syntax/asset limits, dependency and authentication-contract checks.
For just the manager/settings fixture, add `--manager-only` to the browser command.

## Approved repair blocked before activation

The live login was rechecked and still serves v5 and asks for a device name.
`.termdeck/bin/t1000 release info --verify` still exits 78 with
`managed bootstrap path mismatch`. The installed managed
launcher has a retired path binding, privileged preparation services are absent,
and the shared-runtime ownership template would assign the entire user runtime
parent to the preparer account at mode 0700. Applying it as-is would make user
state/authentication/releases inaccessible. The signer requires a preparer-owned
tree, so changing only that ownership line is not a valid repair. The preparation
tree and consumer publication must be separated and qualified together.

Host sudo was rejected in this execution environment. An administrator-enabled
execution path is required after the ownership design is fixed; no root-only
contract was bypassed. The install plan now explicitly blocks applying the unsafe
template to the migrated runtime.

A private pre-repair backup exists at
`.termdeck/repair-backup-20260922-DqqNWH`. It contains coherent session/auth database
backups, all 304 saved tab records, installed binaries and launcher, password/TLS
state, and both user service definitions. Both SQLite integrity checks and all
four checksums in `SHA256SUMS` passed when captured. The latest read-only service
check reports backend/proxy PIDs 1921209/1921259, both active/running with zero
recorded restarts. These differ from the earlier snapshot; this UI work did not
restart either service. No production terminal recovery or session launch was
attempted; no Codex runtime or Cloudflare changes were made.
