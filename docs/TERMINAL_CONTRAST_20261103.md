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
