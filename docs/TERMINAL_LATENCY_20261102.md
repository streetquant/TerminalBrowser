# Terminal scrolling and latency qualification, 2026-11-02

The terminal now uses xterm's native history scrollbar. Its thumb represents
actual retained history and viewport position. The painted thumb is 5 px wide;
the pointer target remains 14 px. Pointer dragging, keyboard Home/End/Page keys,
proportional thumb size, tab-local position and the return-to-live button use
the same viewport. The old range input, relative application thumb and arrow
buttons have been removed.

Full-screen applications own their internal document position. Their wheel
input still works through the terminal protocol, but an alternate screen with
no terminal history does not invent a draggable document-position thumb.
Mouse reporting preserves coordinates, modifiers, SGR/legacy encodings and
normal/alternate-screen modes. Trackpad fractions accumulate. Applications
without mouse reporting retain bounded, server-validated page-key navigation
where those keys are supported. Shift scrolling retains local terminal history;
Ctrl zoom and horizontal browser scrolling remain available.

## Demonstrated hot path and change

The prior renderer fixes and first-keystroke fast path were already installed.
The remaining wheel path sent each native report as a separate WebSocket frame.
Every frame incurred fresh durable authorization, controller validation, a tmux
command and acknowledgement. Rapid wheel input multiplied that work.

The browser now sends up to eight identical native wheel reports in one passive,
sequenced frame. The first batch is dispatched in the wheel event's own turn;
only excess distance waits for a subsequent frame. Pending wheel distance is
bounded to 120 ticks and discarded on controller or application-mode changes.
Keyboard input remains ordered ahead of a later wheel batch and retains its
immediate idle-queue path. Browser output, in-flight frames and socket buffering
remain bounded.

The Rust parser accepts only complete identical vertical reports, with at most
eight per batch. Mixed coordinates, modifiers, direction, encoding, keys,
trailing paste, truncation and a ninth report are rejected. Delivery rechecks the
current tmux mouse mode and encoding. Authorization, durable anti-rollback
clock, revocation, controller lease and replay fences remain in force.

## Installed measurements

Brave 156.0.8078.48, Chromium headless, 1440 x 900 viewport, software DOM renderer.
The typing workload uses 45 trusted keydowns and verifies the exact unsent draft.
The wheel workload uses 90 repeated gestures while typing. Paint figures are
browser requestAnimationFrame observations, not physical-display measurements.

| Installed workload | Median key-to-paint observation | p95 | Median WebSocket ping |
| --- | ---: | ---: | ---: |
| LAN Bash | 31.9 ms | 32.7 ms | 1.2 ms |
| LAN OpenCode with wheel burst | 36.1 ms | 56.1 ms | measured separately |
| Public Bash, first check | 215.4 ms | 216.1 ms | measured separately |
| Public Bash, transport control | 364.7 ms | 365.9 ms | 347.2 ms |
| Public OpenCode with wheel burst | 381.9 ms | 417.3 ms | measured separately |

Median browser event dispatch was 0.2-0.3 ms, with no recorded browser long
tasks in these runs. Public transport latency varied substantially between
checks. The public transport-control run parsed the echo at a median 348.6 ms,
compared with a median 347.2 ms ping on the same authenticated WebSocket. This
bounds the remaining remote echo delay; instantaneous public echo is not
claimed. LAN access is `https://192.168.68.60:7443/`.

The same LAN wheel workload used 730 frames before and 180 after, a 75.3%
reduction, with 810 native reports carried after the change. The public recheck
used 178 frames for 801 native reports. The preceding public baseline used 519
frames. Different public round-trip conditions prevent attributing the overall
typing distributions to the wheel optimization. A public run timed out while
waiting for a typed echo; subsequent instrumented OpenCode and Bash runs passed.

## Qualification and activation

- Python: 4,184 tests and 319 subtests pass; 15 capability-dependent tests skip.
- Frontend: 70 tests pass, including batch bounds, immediate input, ordering,
  viewer fencing, output backpressure, renderer policy and cached-tab isolation.
- Rust: 152 tests pass, including batch parsing, replay and mode validation.
- Browser: 12 mouse-protocol combinations pass. Native history geometry,
  both drag boundaries, four themes, viewer-local scrolling, live output,
  mode/lease changes, reconnect and bounded DOM updates pass.
- Ten real applications/configurations pass: Bash, less with/without mouse,
  Vim with/without mouse, Neovim with/without mouse, nested tmux, normal-screen
  mouse reporting and top. Applicable typing, resize and return-to-shell checks
  pass. Real OpenCode wheel/trackpad scrolling and exact drafts pass on both URLs.
- Real Rust/tmux transport checks pass for bounded batches, delivery once,
  stale controllers, keyboard ordering, mode changes and malformed input.
- Thirty-nine tab switching, authentication, logout and revocation checks pass.

Activation used the supported transaction API with a clean source commit and
verified reused binary. Installed binary SHA-256:
`1b175cdfac9b9d59be8c2c7f216b2ca0de070e749f3239032f0f2694d4cc5a0c`.
The deployed web Git tree is `296906abfd26cf64b238cea4d8fbec30a7bf6490`.
All served terminal assets and CSS match the reviewed source on both routes.

The installed Python payload retains all 84 modules from the preceding qualified
runtime. Other pending recovery and native-launch changes are included in the
authorized source publication and receive Python regression qualification;
this web rollout does not activate them. Existing CLI/argv test expectations
were updated to recognize their explicit environment binding while continuing
to assert the working directory, exact resume command, private rcfile and scope.
The Python qualification command uses `umask 077`, `xvfb-run -a`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and pytest's normal short temporary root.
An unrelated installed pytest plugin is unavailable; deep temporary paths also
exceed Unix socket limits. Neither condition requires changing product guards.

Preservation compares the supported transaction's database/TLS admission with
independently retained identity captures: 307 saved tabs, 16 windows, 289 exact
pane PID/start identities, existing commands/directories/recipes/account bindings,
three active authentication credentials and expiries, eight protected password/TLS
files, TLS inodes/modes and shared tunnel state remain preserved. The preexisting
failed saved-terminal recovery service was temporarily masked for activation and
unmasked afterward without starting it. Existing terminal processes were retained.

Local detailed evidence is under `.termdeck/qualification/latency-20261102-r2/`
and `.termdeck/qualification/latency-20261102-r2-build/`. Runtime receipts,
databases, TLS/authentication, browser captures and local database backups are
excluded from source publication. Reload an existing browser page to load the
new scroller and transport code; live tmux applications remain in their panes.
