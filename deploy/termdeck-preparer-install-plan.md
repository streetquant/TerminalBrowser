# TermDeck preparer install and rollback gate

This repository only prepares the privileged assets.  Do not run these steps
until an operator approves the live installation gate and records the current
unit, executable, account, runtime, and key preimages.

1. Run `scripts/deploy/install-preparer-assets.py --check` from the selected,
   immutable source tree.  Stage the exact manifest into an empty package root
   with `--destdir`; compare every staged file with its manifest source and
   require the declared mode and `root:root` ownership when the package manager
   installs it.
2. Install the sysusers and tmpfiles definitions, then run `systemd-sysusers`
   and `systemd-tmpfiles --create` for those exact files.  Confirm that `max`
   is not a member of `termdeck-preparer` and that the dedicated account has
   `/usr/bin/nologin`.
3. If `/warp/runtime/termdeck/.termdeck-tls-recovery-key` is absent, create a
   same-directory temporary file from 32 bytes returned by the kernel random
   source, set `termdeck-preparer:termdeck-preparer` and mode `0600`, fsync the
   file and parent, and publish with a no-replace rename.  Never replace an
   existing key.  Confirm its exact size, ownership, mode, link count, and
   regular-file type.
4. If the Ed25519 keypair is absent, run
   `/usr/libexec/termdeck-runtime-attestd keygen` once as root.  Never replace
   either member of an existing pair.  Validate the root-only private key and
   public-key readback before starting the daemon.
5. Run `systemd-analyze verify` on the three units, reload systemd, start
   `termdeck-runtime-attestd.service`, and then enable/start only
   `termdeck-preparer-broker.socket`.  The service remains socket activated.
6. As `max`, issue one closed `prepare` request through the socket.  Require a
   successful broker response, a final Ed25519 `producer_attestation`, the same
   receipt device/inode/mode/link tuple as the unsigned core, and a successful
   activation readback before declaring cutover complete.

For rollback, stop the broker socket and both services, retain all signing,
recovery, transaction-journal, and failed-receipt evidence, restore the exact
preimage package assets and unit files, reload systemd, and start only the
previously active units.  Do not rotate either key as part of package rollback.
Record file hashes and service state before and after the transition.
