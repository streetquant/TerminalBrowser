# Readable application text and a steady browser cursor

BR: `t1000-bhw`. User feedback: the Codex prompt blends into its panel across
themes; the browser terminal cursor must never blink.

## Implementation

The browser renderer previously configured xterm with `minimumContrastRatio: 1`,
disabling its contrast correction. Changing a terminal palette cannot change an
application's explicit RGB foreground/background colors.

The renderer now uses xterm's cached 4.5 contrast policy against each cell's
resolved background, including application panels and reverse video. Dim text
keeps xterm's distinct half-strength policy (2.25); block graphics are exempt.
The raw terminal stream, terminal palettes and panel backgrounds are preserved.
No additional input handler, byte filter, polling loop or dependency is added.

The cursor is steady at creation and during appearance changes. Legacy saved
`cursorBlink: true` preferences are read as false, and settings no longer offer
a blinking checkbox. Application cursor-shape commands still work with steady
block/underline/bar shapes; reset restores the selected shape. Blink-enable
commands are suppressed. A combined DEC mode command still applies its other
modes; a one-shot parser-completion listener restores the steady cursor before
rendering. No permanent frame/output listener is needed for that guard.

## Validation

`scripts/check_web_terminal_contrast.py` uses disposable browser pages with the
real bundled terminal. Its 28 configurations cover baseline/candidate, all seven
palettes and DOM/real software-WebGL rendering. Software WebGL is functional
coverage with test-only GPU identification; production keeps its hardware policy.

Pixels are checked for RGB/indexed/ANSI text, dark/light panels, inverse video,
dim text, unchanged readable text and unchanged block graphics. Software WebGL
can dither a channel by one 8-bit step; the checker tolerates only that step for
unchanged colors. The screenshot-like prompt improves from 3.59:1 to 5.28:1.
Palette switching checks the already-populated buffer, without resetting it.

Both renderers also pass checks for legacy preferences, every standard cursor
shape, blink-enable commands including combined mouse/cursor modes, and identical
idle cursor screenshots. The appearance fixture verifies legacy preference
handling, settings changes, reload and reset. Its missing shared-tab helper was
repaired by loading the actual `app.js` globals; there is no substitute UI mock.

The 87 frontend tests, 152 Rust tests, pinned asset build and syntax/whitespace
checks pass. The exact rebuilt server passes all 42 browser integration checks,
including input, paste, Unicode, alternate screens, controller fencing, settings,
reconnect, navigation and destructive-action handling on disposable terminals.

An ABBA comparison against the preceding installed binary records 120 samples per
version. These are headless observations and include frame-polling variation:

| Observation | Before median / p95 | After median / p95 |
| --- | --- | --- |
| Cached switching | 5.90 / 8.00 ms | 5.25 / 7.60 ms |
| Input-to-buffer echo | 11.80 / 13.60 ms | 12.05 / 15.50 ms |

The small echo-tail increase is retained, rather than described as a speedup or
zero variation. These measurements do not establish physical-display latency or
a public-route speedup.

## Installed validation

The source and portable BR record were published on existing `main` in
`475e1d3f464012f84e67ff0ef96ae5062befea2b`. Supported activation transaction
`tx-20261102T223958Z-4401a29f84` completed with status `activated`. The installed
backend matches the qualified binary SHA-256
`8f9e77acf00bd9f19e5584925e3326db4140a85380acaa46e4bb9522f8956ab5`.
The LAN service remains the existing nginx proxy. All 84 installed Python modules
are byte-identical to the preceding qualified installation.

Fresh LAN and public-route browser checks each pass six acceptance groups:
readable prompt and steady cursor across all seven live palettes; legacy blinking
preferences remain steady after reload; both served assets match `main`; no
browser errors; the test's temporary device is revoked; and original pane
identities remain intact. The live screenshot-like prompt measures 5.28:1 in every
palette. Direct LAN and public HTTPS health checks pass TLS verification.

Matched live LAN Bash typing uses 45 trusted key presses per version, without
submitting a command. The browser rendering observation and dispatch measurements
are:

| Observation | Before median / p95 | After median / p95 |
| --- | --- | --- |
| Key to rendering observation | 31.7 / 32.5 ms | 32.1 / 32.5 ms |
| Input dispatch | 0.3 / 0.4 ms | 0.2 / 0.4 ms |

There are no observed long tasks or browser errors in either typing run. The live
rendering p95 is unchanged; this does not erase the separate headless echo-tail
increase above or establish zero variation for every workload.

Post-activation and post-test preservation checks pass for all 308 saved sessions,
16 remembered windows, 290 existing pane process identities, three active
authentication records, eight protected password/TLS files, two tunnel units and
two shared cloudflared processes. The already-failed saved-terminal recovery unit
was temporarily masked during activation, unmasked afterward and never started.
Only disposable test terminals and temporary devices were used for validation.

An already-open browser page needs one reload to load the new renderer. No forced
page reload or interruption of user terminals was performed.

Detailed source, binary, browser, performance and preservation evidence is private
under `.termdeck/qualification/contrast-20261103/`. Local and remote publication
use the existing `main`, with no new branch or PR.

## Cursor visibility regression

The user subsequently reported that the cursor disappeared in every application,
including the plain terminal, and clicking inside did not restore it. The earlier
idle screenshot equality check did not positively establish cursor visibility;
an invisible cursor also passes that check. BR `t1000-bhw` was reopened.

Read-only pane metadata reported all 290 cursors as shown. A new disposable live
terminal showed the default cursor, so the exact user's browser state was not
recovered. A concrete rendering failure was reproduced with a saved cursor color
equal to the selected palette background: both renderers produced zero cursor
pixels. The prior one-pixel bar and underline were also easy to overlook.

The correction validates the cursor color against the selected terminal palette
when appearance is created or changed. A requested color below 3:1 contrast falls
back to the palette foreground, or a contrasting black/white fallback if needed.
Readable custom colors retain their value. Cursor text also retains a contrasting
accent. The bar is now two CSS pixels wide. Application hiding, application cursor
shapes and the no-blink policy are preserved. No input/output handler, recurring
frame work or polling was introduced by this correction.

The new checker compares shown-cursor pixels with deliberately hidden-cursor
pixels and proves that showing restores the original image. It passes 63 cases
per renderer (seven palettes, three shapes and three requested colors), plus 21
unfocused cases per renderer. Every case has visible pixels; minimum cursor
contrast is 4.33:1 in this matrix. All 28 original text configurations still pass,
including the 5.28:1 prompt. The 87 frontend tests and 152 Rust tests pass; the
final rebuilt server passes all 42 browser integration checks. Its SHA-256 is
`fcf9e558aeeac224ab021bea6a41191afd492471ef5b537b7fc11f2ffd87a26e`.

Fresh regression evidence is private under
`.termdeck/qualification/cursor-20261103/`, including the original start commit,
task-start snapshot, native runtime operation export and completion audit.
Installed correction results are recorded after supported activation.

## Text-matching cursor requirement

The user then required the cursor to have the same color as terminal text.
The final policy derives the cursor directly from the selected theme foreground
and its block accent from the background, replacing the earlier custom-color
contrast fallback. This also neutralizes stale saved cursor colors. Settings
show an automatic text-matching color instead of an editable override. Shape,
two-pixel bar width, application hiding and the steady cursor policy remain.
Strict tests now require foreground equality as well as positive visible pixels.
No per-input, output, timer or frame work was added.

Final source qualification for text matching: 87 frontend tests, 42 browser
integration assertions, 28 text configurations, 126 focused cursor cases and 42
unfocused cursor cases pass. The settings/apply/reload/reset browser test confirms
foreground-colored cursors and the automatic color control. The new backend
SHA-256 is `42b6d4c039bd093a884cea00d4aeaa85ecaeb8f7deb97e6b6e065c557a7e7168`.

Supported activation `tx-20261102T233823Z-ee8244cc65` installed that exact binary.
The live backend SHA-256 matches the qualified build. LAN and public routes each
pass nine acceptance groups, including 21 positive cursor visibility cases with
text-color equality, reload with stale saved colors, matching served assets and
original pane preservation. The 84 installed Python modules remain unchanged.

The final matched LAN comparison uses 45 trusted keys per version:

| Observation | Before median / p95 | After median / p95 |
| --- | --- | --- |
| Key to rendering observation | 31.9 / 32.5 ms | 31.8 / 32.4 ms |
| Input dispatch | 0.3 / 0.4 ms | 0.3 / 0.4 ms |

No long tasks or browser errors were observed. The maximum rendering observation
was 32.7 ms before and 37.2 ms after; these samples do not guarantee zero variation
or physical-display latency. All preservation groups still pass: 308 sessions,
16 windows, 290 pane identities, three active auth records, eight protected files,
two tunnel units and two shared cloudflared processes. Only disposable test
resources were removed. Existing browser pages need one voluntary reload.

Source is published on existing `main` in `c6d56f8066c1d6d8160467dbd384c696f195d6e4`.
No branch or PR was created. Final qualification records are private under
`.termdeck/qualification/cursor-20261103/text-color/`. The completion audit uses
the unchanged canonical deterministic gates with the user-authorized portable
BR path explicitly allowed; other paths are restricted to the exact task scope,
and original instruction pins remain unchanged. Native runtime records retain
the original session and tool identities; no independent reviewer is claimed.

## Application color override regression

A later user screenshot still shows a dark block after the text-matching change.
The exact existing browser state was not captured. A read-only match of the known
composer row confirmed an application-defined RGB panel; no input, resize or new
attachment was sent to the user's pane. Its tmux cursor-color metadata was unset,
so the screenshot alone does not establish which application or cached client
state introduced the dark color.

The previous tests had a concrete gap: `options.theme.cursor` can remain light
while xterm's native cursor becomes dark after OSC 12. Positive pixel differences
alone also accept this near-invisible block. A new regression test fails against
`c933f63`: actual cursor `#252829`, foreground `#dfe6f2`. The same composer fixture
has 242 changed cursor pixels both before and after, but its measured cursor-to-
cell contrast improves from **1.12:1 to 13.29:1**.

The renderer now subscribes to the pinned xterm 6 color service. Only native color
changes synchronize cursor to foreground and cursor accent to background. This
covers OSC 12, stacked OSC 10/11 cursor values, foreground/background changes and
color restoration. An equality guard bounds re-entrant notifications. Native
foreground/background/ANSI colors remain intact, and intentional cursor hiding
and steady block/bar/underline shapes retain their behavior. The subscription is
disposed with the view. There is no polling, frame loop, per-key handler or output
rewriting. The private pinned xterm interface is deliberately covered by tests of
the real bundle; an xterm upgrade must pass those checks.

Source qualification passes all 87 frontend tests and 42 integration assertions
on rebuilt server SHA-256
`87d3805f3c8df1d4d82e9f07a8a9142e041d3dce6a15665a11bce910119ca18e`.
The original 28 text configurations and 126 focused/42 unfocused cursor checks
still pass. An additional 112 application-color cases check actual native colors
and raster contrast in DOM and software WebGL, plus 28 bar/underline and 14
unfocused cases after a dark override. Minimum measured block cursor contrast in
that new matrix is 5.27:1. Both renderers remain steady while idle; color changes
produce no terminal input, and ANSI restoration/intentional hiding still work.
The pre-install LAN baseline records 45 trusted keys: render median/p95
32.0/32.7 ms and dispatch median/p95 0.3/0.4 ms. Installed comparison follows.

Evidence for this continuation, its task-start snapshot at `c933f63`, retained
original policy pins and native runtime receipts is private under
`.termdeck/qualification/cursor-stream-20261103/`. The original cursor goal base
and prior receipts remain preserved. Existing browser pages still require a
voluntary reload to load new renderer code.

## Latest requirement: fixed plain white

The user explicitly replaced text matching with: “Make the cursor just plain
white all the time.” The final policy is therefore fixed **`#ffffff`**, independent
of palette foreground, old saved cursor colors, or application color commands.
The native color-change guard retains the initially white color and restores it
when required, without resetting application foreground/background/ANSI colors.
Cursor accent follows the background so characters inside a white block retain
contrast. The no-blink policy, shapes and two-pixel bar remain unchanged.
Settings shows disabled `#ffffff` with “Always white”; there is no obsolete
text-matching claim or editable override.

Strict native and pixel checks now require white rather than theme-foreground
matching. Foreground/background preservation checks still exercise application
color changes. The existing frontend test changes its expected value to exactly
`#ffffff`; no assertions or coverage are removed. The real settings test verifies
fixed white, disabled editing, apply, theme changes, reload and reset.

The changed requirement was snapshotted before edits at `95aed6c` in private
`white-start-snapshot.json`. Original task-start `c933f63`, earlier goal provenance,
policy pins and previous qualification records remain preserved. Fresh fixed-white
build, installation and performance evidence is under
`.termdeck/qualification/cursor-stream-20261103/white/`.

Fixed-white source qualification passes all 87 frontend tests, 42 rebuilt-server
integration assertions, real appearance apply/reload/reset checks, 28 text
configurations and all previous cursor visibility checks. All 112 native color
cases preserve a `#ffffff` cursor; minimum measured block cursor contrast against
the fixture panel is 16.67:1. Additional shapes, unfocused cursors, application
color restoration, intentional hiding and the no-blink policy also pass in both
DOM and software WebGL. Qualified fixed-white server SHA-256:
`8ea77149eb5bc90c04350617482967b0910f583cca0101d58e48ad5b306428f2`.

Fixed-white source is published on existing `main` in `164cef5`. Supported
activation `tx-20261103T001349Z-c1810a173f` installs the exact qualified binary;
all 84 Python modules are unchanged. Fresh LAN/public browsers each pass ten
acceptance groups, including an actual dark OSC 12 command delivered through the
live socket, 21 white/visible cursor cases per route, stale saved settings and
reload. Both HTTPS health checks return 200 with TLS verification. These checks
are fresh contexts, not proof that an already-open page loaded the new code.

Matched LAN Bash typing uses 45 trusted keys per version: render observation
median/p95 31.9/32.4 ms before and 31.7/32.4 ms after; dispatch 0.3/0.4 ms both.
Maximum rendering observation is 35.3 ms before and 32.6 ms after, with no long
tasks or browser errors. This establishes no p95 regression in that workload;
it does not guarantee physical-display latency or every application's latency.

The raw activation preservation check is retained as failed: one session changed
visible 1 to 0 and its pane disappeared. Access logs show a separate authenticated
LAN client successfully posting a tab mutation for that exact session during
activation. The request body/action is not logged, so its precise action is not
claimed. All other 289 original pane identities and 307 other session records are
identical. Final verification admits only that specific evidenced concurrent
change; no other discrepancy is allowed. All 308 session records, remembered
windows, auth/TLS files, tunnel units and shared processes remain present. No old
database was restored and the removed pane was not recreated. The saved-terminal
recovery service was temporarily masked/unmasked and never started.

The user's subsequent report that existing applications still have an invisible
cursor exposed an installation/loaded-client distinction: at investigation, the
active LAN client last requested `terminal.js` at 23:45:15 UTC, before either new
color-service or fixed-white update. Opening an application or reconnecting the
socket does not replace already-executing JavaScript. The user was asked to hard
refresh once with Ctrl+Shift+R. Loaded-client fetch evidence is retained privately;
actual user-visible acceptance after that reload remains pending until confirmed.


### Existing-browser acceptance and style-override diagnosis

The first requested hard refresh did not resolve the report. A fresh client
`terminal.js` response at 00:23:38 UTC ruled out stale JavaScript as the complete
explanation. Read-only diagnostics supplied by the user then confirmed DOM
rendering, an initialized and enabled cursor, `#ffffff`, block style, and no
blinking. The cursor was inside the visible grid, but its computed outline was
`rgb(48, 52, 54)` against `rgb(30, 30, 30)`. This is a real styling mismatch,
independent of the renderer's white color setting.

The user's Console confirmed Dark Reader styles were present. The user explained
that the extension was enabled globally but excluded on this site, then directly
confirmed “yeah now its working” and “I did ctrl shift r”. This is consistent
with residual extension styling despite the site exclusion. No post-refresh
extension lifecycle or style-removal measurement is claimed. Physical cursor
acceptance is now confirmed by the affected user; no additional renderer patch,
service restart, or extension setting change was performed for this diagnosis.

A separate fresh live-shell check covered startup, ordinary clicking, typing and
reload without writing terminal controls or calling the terminal focus API.
The same check covered a real OpenCode fixture and software WebGL activation.
Every checked normal cursor cell was fully white; original panes were unchanged
and only the tests' own terminals and temporary devices were removed. These
checks address the earlier fixture limitation: forced cursor-show sequences
alone cannot establish that normal startup and typing display the cursor.

Private evidence: `white/affected-client-diagnostics.json`, `white/normal-cursor.json`,
`white/normal-cursor-dom-opencode.json`, `white/normal-cursor-webgl-shell.json`,
and the native session export under the existing `cursor-stream-20261103` evidence
root. The fixed-white implementation remains source commit `164cef5`. The latest
matched performance comparison remains 32.4 ms p95 rendering observation and
0.4 ms p95 dispatch both before and after; this diagnosis made no product-code
change. Final audit receipt: `cursor-stream-20261103/final-compliance-receipt.json`.
