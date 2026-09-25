# TermDeck preparer install and rollback gate

This runbook covers the privileged preparation boundary for the migrated t1000
installation. It prepares an operator-reviewed change; it is not authorization
to modify the host. Do not run the installation section until the BR deployment
and administrator gates are satisfied and the exact source tree has passed the
qualification section below.

## Canonical ownership model

The supported layout has two disjoint runtime trees and one fixed publication
boundary:

- `/var/lib/termdeck-preparer` is owned by
  `termdeck-preparer:termdeck-preparer` at mode `0700`.
- `/var/lib/termdeck-preparer/runtime` is the private producer runtime, also
  owned by `termdeck-preparer:termdeck-preparer` at mode `0700`.
- `/var/lib/termdeck-preparer/.termdeck-tls-recovery-key` is the producer-only
  32-byte journal authenticator at mode `0600`.
- `/warp/projects/softwares/termdeck/.termdeck` is the max-owned consumer tree.
  It contains user state, authentication data, managed releases, binaries and
  service runtime. No tmpfiles rule may create it, chown it, chmod it, or claim
  any of its descendants.
- `/usr/libexec/termdeck-web-prepare-runtime` is the fixed root-owned image that
  runs as the dedicated preparer principal against the private producer tree.
- `/usr/libexec/termdeck-preparer-publication` is the fixed root-owned helper
  that seeds approved inputs and publishes receipt-bound outputs between the
  producer and consumer trees.
- `/usr/libexec/termdeck-runtime-attestd` signs only the producer receipt. Its
  service receives a read-only bind of `/var/lib/termdeck-preparer/runtime` and
  no view of the consumer tree.

The publication helper may read only the approved consumer inputs
`bin/termdeck-web-server`, `nginx/nginx.conf`, the existing TLS set, and an
optional legacy recovery-key leaf. It may publish only `build-receipt.json`,
`tls`, `current-lan-ip`, and `nginx/allowed-hosts.map`. It must not traverse or
rewrite `state`, authentication data, managed releases, unrelated binaries, or
other consumer paths.

The former shared-parent design is rejected. In particular, never apply a
configuration that assigns `/warp/projects/softwares/termdeck/.termdeck` to the
preparer account or places the producer receipt and recovery authenticator in
that max-readable tree.

## Source qualification gate

Run these checks from the exact reviewed repository root before staging or
installing any host asset:

1. Record `git status --short --branch`, the reviewed commit or source digest,
   and hashes of every path listed by
   `deploy/preparer-install-manifest.json`. Preserve unrelated dirty work.
2. Run `python scripts/deploy/install-preparer-assets.py --check`.
3. Stage into a new empty package root with
   `python scripts/deploy/install-preparer-assets.py --destdir <empty-dir>`.
   Compare every staged file byte-for-byte with its manifest source and require
   the declared mode. The package manager must install every manifest entry as
   `root:root`.
4. Run the focused publication, broker, provisioning-template, packaging,
   attestation and release-path-migration tests against this exact source.
5. Run `systemd-analyze verify` on
   `termdeck-runtime-attestd.service`,
   `termdeck-preparer-broker.socket`, and
   `termdeck-preparer-broker.service` from the staged tree.
6. Inspect the staged tmpfiles file and fail if it mentions the consumer root.
   It must create only the private producer paths and root control paths.
7. Fail the gate on any test failure, manifest mismatch, unreviewed source
   change, unsafe symlink, unexpected owner/mode, or missing rollback evidence.

Source tests and a successful staging run qualify only the package inputs. They
do not prove that the host installation or live services work.

## Capture host preimages

Before installation, record and protect all of the following:

1. Installed bytes, hashes, owners and modes for every destination in the
   preparer install manifest.
2. Current enabled/active/restart state for the attestation service, broker
   socket and broker service.
3. Numeric identities, primary groups and account properties for `root`, `max`
   and `termdeck-preparer`. `max` must not be a member of the preparer group,
   and the preparer account must use `/usr/bin/nologin`.
4. Device, inode, owner, group and mode for the producer parent, producer
   runtime and consumer root.
5. A bounded inventory and hashes for consumer `state`, authentication data,
   managed releases, binaries and recovery recipes. Do not copy secrets into
   ordinary logs.
6. Presence, size, owner, group, mode, link count and digest of both possible
   recovery-key locations:
   `/var/lib/termdeck-preparer/.termdeck-tls-recovery-key` and the legacy
   `/warp/projects/softwares/termdeck/.termdeck/.termdeck-tls-recovery-key`.
7. Existing signer keypair state under `/etc/termdeck` and the exact previous
   unit/package assets needed for rollback.

Stop if the consumer root is not a real max-owned mode-`0700` directory, either
runtime root resolves through an unsafe symlink, both recovery keys exist with
different bytes, or any required preimage cannot be captured reliably.

## Installation and first transaction

1. Install the exact staged manifest entries as `root:root` with their declared
   modes. Do not execute a helper from the mutable producer or consumer tree.
2. Install the sysusers definition and run `systemd-sysusers` for that exact
   file. Verify the dedicated UID/GID, primary group and nologin shell.
3. Install the tmpfiles definition and run `systemd-tmpfiles --create` for that
   exact file. Verify the producer parent and runtime are preparer-owned mode
   `0700`. Re-read the consumer root and prove its identity, owner and mode did
   not change.
4. Do not manually create, copy, truncate, rotate, or chown a recovery key in
   the consumer tree. On the first authenticated broker request:
   - a valid legacy-only key is copied with no-replace semantics to the private
     producer parent and removed from the consumer only after both identities
     and bytes are revalidated;
   - matching private and legacy keys retain the private key and remove the
     verified legacy leaf;
   - conflicting, malformed, linked or incorrectly owned keys fail closed; and
   - when neither key exists, the unprivileged preparer creates one private
     32-byte random key with `O_EXCL` and mode `0600`.
5. If the Ed25519 attestation keypair is absent, run
   `/usr/libexec/termdeck-runtime-attestd keygen` once as root. Never replace
   either member of an existing pair. Validate the root-only private key and
   public-key readback before starting the daemon.
6. Run `systemd-analyze verify` against the installed units, reload systemd,
   start `termdeck-runtime-attestd.service`, then enable/start only
   `termdeck-preparer-broker.socket`. The broker service remains socket
   activated and refuses manual start.
7. As `max`, issue one closed prepare request through
   `/run/termdeck-preparer-broker/request.sock`. The request may select only the
   allowlisted restore mode and generation; it cannot supply a path,
   executable, descriptor, environment, UID or GID.
8. Require a successful seed, preparer and publication sequence. The broker
   must authenticate `SO_PEERCRED`, execute the fixed preparer exactly once as
   the dedicated principal, and execute the fixed publication helper as root.
9. Require a final signed producer receipt, exact signer readback, a successful
   consumer verification result, and owner/mode checks for every published
   postimage. Confirm the consumer receipt names the same generation and
   digests as the producer receipt.
10. Recompare all protected consumer inventories captured before installation.
    `state`, authentication data, releases, unrelated binaries, saved tabs,
    recovery recipes and working directories must be unchanged and accessible
    to `max`.

## Upgrade verification

For an upgrade, repeat source qualification and host preimage capture. Exercise
both successful publication and an injected disposable failure. Require:

- same-directory transactional replacement of the allowlisted consumer outputs;
- exact restoration of prior consumer inodes and bytes after a failed
  publication;
- no leftover `.termdeck-new-*`, `.termdeck-prior-*`,
  `.termdeck-prior-dir-*`, `.termdeck-rollback-dir-*`, or key-migration
  temporary files;
- unchanged user state, authentication, release artifacts and unrelated
  binaries; and
- continued verification by the attestation daemon and publication helper.

Never use production sessions for input, kill, resize, or bulk-action tests.
Those checks require disposable terminal state under the later qualification
and smoke-test BR tasks.

## Rollback

Rollback is a controlled package-and-service rollback, not a reversal to the
unsafe shared-parent ownership model.

1. Stop the broker socket and allow any activated broker service to terminate.
   Stop the attestation service only after recording its final state and logs.
2. Preserve the private recovery authenticator, signer keypair, producer
   runtime, transaction journals, failed receipts and all consumer data. Do not
   rotate or copy a key back into the consumer tree merely because package
   files are being rolled back.
3. Restore the exact preimage executables, unit files, sysusers file and other
   manifest assets. Do not execute an older tmpfiles rule that would chown or
   chmod the consumer root. If the prior package requires that unsafe rule or
   cannot operate with the preserved private key, keep services stopped and
   record a rollback blocker instead of forcing compatibility.
4. Reload systemd and start only the units that were active before the change
   and remain compatible with the preserved ownership boundary.
5. Re-read hashes, owners, modes, service state and the protected consumer
   inventory. Require user state, authentication, releases, binaries, saved
   tabs, recovery recipes, TLS and working directories to match the captured
   preimages except for an explicitly accepted, receipt-bound publication.
6. Retain before/after evidence and any failure receipts. Do not declare
   rollback complete while a service is failed, a protected path differs, or a
   transaction temporary remains.

## Stop conditions

Stop without activation and update the governing BR task when any prerequisite
is absent, a claim or administrator authorization is missing, ownership is
ambiguous, a key or receipt conflicts, a test fails, the host differs from the
captured model, or verification would require touching live user sessions.
Cloudflare changes and unrelated Warp infrastructure remain outside this gate.
