# TermDeck remaining work

Status captured: 2026-09-22 UTC

This file records only unfinished TermDeck work. Do not repeat the completed
checks listed at the end.

## Active UI and release work

- Qualify the clean committed release against the production contract before
  switching the running application. Do not confuse the completed focused UI
  checks with the complete release qualification.

## Production activation

- Repair the live managed launcher error: `managed bootstrap path mismatch`.
  Read-only release verification identifies the underlying mismatch: the
  colocated managed `state.json` still binds its stable launcher to
  `/warp/runtime/termdeck/bin/t1000`. The existing install contract refuses an
  in-place launcher-path change; do not hand-edit its authenticated hashes.
- Install or restore the missing TermDeck recovery service from the corrected
  canonical templates.
- Publish the managed service wrappers under
  `/warp/projects/softwares/termdeck/.termdeck/t1000-managed`; the checked-in
  systemd templates intentionally resolve there.
- Resolve privileged preparation prerequisites before activating managed LAN
  wrappers: `termdeck-runtime-attestd.service` and
  `termdeck-preparer-broker.service` are currently not installed. Review the
  colocated runtime ownership boundary before applying privileged tmpfiles;
  `.termdeck` currently contains user-owned application state and releases.
- Activate the qualified release without losing the current TermDeck sessions,
  windows, tabs, foreground working directories, authentication state, or TLS
  material.
- After activation, verify both user services are loaded and running with zero
  restarts, then exercise HTTPS, WebSocket terminal I/O, tab switching, typing,
  resize, reconnect, and recovery against the live installation.

## Final Warp cleanup

- Retire the obsolete root-owned `/etc/systemd/system/termdeck-checkpoint.service`.
  It still points at the retired `/var/lib/cloudmcp-exec` layout and is handled
  by the prepared Warp layout finalizer.
- Complete the mount-alias and hot-LV cutover, then verify TermDeck still uses
  only `/warp/projects/softwares/termdeck` and its `.termdeck` subtree.
- Confirm no active TermDeck unit or executable references
  `/warp/runtime/termdeck`, `/warp/state/termdeck`,
  `/warp/projects/software/termdeck`, `/8tb`, or
  `/var/lib/cloudmcp-exec/termdeck*`.

## Already completed; do not repeat

- Committed the colocated deployment migration (`ca1b34a`), UI/performance and
  regression tests (`1c1e4fb`), and clean-snapshot release smoke plus audit
  documentation (`69dcb40`). No remote push was requested or performed.
- Built the clean committed v0.2.21 Python package twice with byte-identical
  output and verified all 78 wheel members. This is a build, not installation
  or complete production qualification; no live service was switched.
- Release preflight confirms non-rotational XFS storage. The existing backend
  health check succeeds and HTTPS returns its normal authentication redirect
  with the existing CA trusted. The managed state, bootstrap, launcher, web
  executable and CA were left unchanged.
- The UI session finished all 22 integrated Chromium assertions, including
  failure-state geometry, mobile layout, keyboard navigation, click-to-type,
  and metadata-refresh focus. See `docs/UI_UX_STATUS.md` for proof and limits.
- The focused UI suite passed 195 unique tests and 32 subtests; all three
  frontend performance/lifecycle regression tests pass.
- The running backend and LAN proxy are active with zero restarts and already
  use `/warp/projects/softwares/termdeck/.termdeck`.
- The canonical path deployment checker passes all 19 contract checks.
- The focused colocated-layout suite passed 437 tests and 22 subtests; its one
  corrected message-regex assertion passed independently.
- Python compilation and `git diff --check` passed for the path conversion.
- Desktop recovery restored all 30 disposable tabs across 10 windows with
  foreground working directories preserved.
- Desktop visual checks now contain real terminal output and no startup error.
- The disposable release smoke passes installation, tamper rejection, launcher
  repair, and rollback.
