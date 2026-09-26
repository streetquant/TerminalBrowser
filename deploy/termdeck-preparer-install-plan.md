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
- `/usr/lib/termdeck-preparer/termdeck` is the private, root-owned Python
  library for the signer and receipt-state helpers. Its `__init__.py`,
  `runtime_attestation.py`, and `preparer_state.py` are explicit manifest
  members, installed `root:root` mode `0644`; both library directories are
  `root:root` mode `0755`. Both libexec entry points use the same isolated
  loader, which reads only these fixed files through no-follow descriptors
  after validating every ancestor and file. It never imports TermDeck from
  the checkout, user site, `PYTHONPATH`, or a global Python installation.

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
   Packaging qualification must execute both staged libexec entry points with
   isolated system Python, exercise a disposable Ed25519 receipt round trip,
   and reject absent, linked, writable or incorrectly owned private libraries.
   `tests/test_preparer_packaging.py` uses an unprivileged bubblewrap namespace
   for these installed-path checks. A skipped executable test is a missing
   qualification prerequisite; static unit verification cannot substitute for it.
   `tests/test_termdeck_preparer_signed_boundary.py` additionally runs the actual
   staged preparer, signer and broker with separate producer, consumer and root
   identities. It must pass first preparation from legacy TLS, signed A/B
   publication, restored-source inverse and replay. The activation consumer and
   rollback-restart suites must also pass with distinct producer/consumer inodes.
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

### Arch package artifact

On Arch, build the manifest payload as an ordinary user with the native package
manager tools, using a fixed qualification timestamp and an existing empty
mode-0700 output directory:

```sh
python scripts/deploy/build-preparer-arch-package.py \
  --output /absolute/private/package-output --epoch QUALIFICATION_UNIX_TIMESTAMP
```

The builder uses network-isolated bubblewrap and native makepkg, with a fixed
`/build` path and explicit configuration. It verifies the archive contains only
the thirteen allowlisted files, their ancestor directories and native package
metadata, with exact bytes, root ownership and declared modes. It does not use
an install scriptlet, fetch dependencies, execute sudo or install anything.
`receipt.json` and `SHA256SUMS` identify the result. Repeat builds at the same
epoch must have the same digest on the same qualified toolchain.

The output also contains `pacman-hooks/` overrides for the five reviewed broad
systemd hooks. Standard Arch sysusers/tmpfiles hooks operate on **all** system
definitions; the install command below defers those hooks and the broad service
reload/enqueue/update hooks. It leaves unrelated hooks and package signature,
integrity, dependency and conflict checks enabled. Inspect current host hooks
and retain their exact preimages before adopting this transaction policy; a new
or changed matching hook requires review. The privileged preparer services must
be stopped before replacing an older preparer package; preserve user terminals
and the existing application services.

After the host preimage gate below, the administrator's native install command
from the output directory is:

```sh
sha256sum -c SHA256SUMS &&
sudo /usr/bin/pacman -U \
  --hookdir /etc/pacman.d/hooks \
  --hookdir "$PWD/pacman-hooks" \
  ./termdeck-preparer-VERSION-any.pkg.tar.zst
```

Keep the ordinary custom hook directory first and the transaction overrides
second, so unrelated custom hooks remain active. Use the exact basename recorded
in the receipt. Do not add `--overwrite`,
`--nodeps`, signature overrides or `--noconfirm`. This installs package files
only. Follow the scoped sysusers/tmpfiles, installed-helper, keypair and unit
checks below; the deferred global hooks are not a substitute for those checks.
The broker's authenticated prepare/verify transaction remains a subsequent
gate before application activation.

For a first-install rollback, stop the broker socket and drain its service,
stop the signer if this transaction started it, then remove only this package
with `sudo /usr/bin/pacman -R --hookdir /etc/pacman.d/hooks --hookdir "$PWD/pacman-hooks" termdeck-preparer`. Do not use
recursive dependency removal. Refresh the daemon after removal and restore
captured unit enablement; retain accounts, private keys, producer/consumer data
and journals. For upgrades, reinstall the complete recorded prior package with
the same scoped hook policy while the privileged services are stopped, then
verify its compatibility before restoring prior activity. Actual host rollback
still requires the checks in the Rollback section.

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
   unit/package assets needed for rollback, including every private Python
   library member and the library-directory metadata. Capture an explicit
   absence record for destinations not installed yet. Preserve the matching
   launcher/library package as one versioned rollback set.

Stop if the consumer root is not a real max-owned mode-`0700` directory, either
runtime root resolves through an unsafe symlink, both recovery keys exist with
different bytes, or any required preimage cannot be captured reliably.

## Installation and first transaction

1. Install all thirteen exact staged manifest entries as `root:root` with their
   declared modes through the administrator's reviewed package deployment
   mechanism. The repository installer validates and stages the payload; its
   `--destdir` is an existing empty package root, never `/`. Create only the
   private library directories as `root:root` mode `0755`. Install the shared
   launcher at **both** libexec destinations and the three private library
   files as one complete package. Keep the broker socket and signer stopped
   during a package replacement so an invocation cannot mix versions.
   Recompare every installed file with its qualified stage and verify all
   library ancestors are real root-owned directories without group/other
   write permission. Run these non-mutating executable checks before keygen
   or service startup:

   ```sh
   /usr/bin/python3 -I /usr/libexec/termdeck-runtime-attestd --help
   /usr/bin/python3 -I /usr/libexec/termdeck-preparer-state --help
   ```

   Stop on import, library, ownership or mode failure. No global `pip` install,
   editable checkout, `PYTHONPATH` override, or mutable runtime helper is part
   of this package contract.
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

### Interrupted initial preparation

Initial seeding validates all approved source, TLS and recovery-key inputs before
publication. It stages a complete TLS directory and exchanges it atomically;
failures restore the original directory and recovery-key inodes. A successful
repair may create new producer inodes while preserving the selected CA/key bytes.

The only automatic legacy repair is an exact proper prefix of the sorted allowed
TLS leaves, with identical retained bytes, owners and modes, matching preceding
binary/configuration copies, and no producer receipt, output or generation state.
Conflicting files and established generations fail closed or remain unchanged.
Do not delete the partial tree or regenerate keys to force a retry. Install the
qualified complete package, then issue one ordinary authenticated prepare request.

The first preparation of a complete legacy TLS seed may retain TLS without a
prior signed runtime receipt. This absence is accepted only when the private
journal authenticator proves an explicit absent receipt and binds the exact
current/prior TLS generations. An established generation's missing receipt or
unexpected retained runtime outputs still fail. Both producer and consumer nginx
configuration copies use mode `0600`, including source restoration before inverse.

Broker journal failures contain fixed phase/category codes. Use those codes to
locate seed, execution, publication or verification failure; a generic refused
client response alone does not establish the failing stage. Raw helper output and
credential material are not journal diagnostics.

Verification returns the unchanged signed producer receipt plus a separate
consumer publication observation. Producer and consumer inodes differ, and runtime
and TLS transaction generations are independently signed. Activation verifies the
signature against the private producer path and rechecks the consumer files before
adoption. Restore responses preserve producer lineage separately from consumer
copies; completed replay must verify the recorded copies without republishing them.

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
3. Restore the exact preimage executables, all three private Python library
   files, library-directory metadata, unit files, sysusers file and other
   manifest assets as one matching package. Preserve absence for destinations
   that were absent before this first installation; remove only the exact
   verified installed leaves owned by the package transaction. Do not remove
   producer, consumer, signer, recovery-key or journal data. Recheck both
   installed libexec entry points with isolated Python before restarting a
   restored compatible package. A prior ten-file payload without its private
   library is not an executable rollback package on this host; retain it as
   evidence and keep the privileged services stopped.
   Do not execute an older tmpfiles rule that would chown or
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
