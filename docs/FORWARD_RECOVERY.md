# Recovering from an old failed activation

An irrecoverable old inverse must retain its `rollback-failed` receipt. Current
healthy TLS does not prove restoration of the old TLS directory or its inodes.
The supported forward path binds a fresh prepared transaction to that preserved
failure and a verified current baseline. It does not run the old rollback.

Use the source-owned activation CLI from the reviewed candidate. First prepare
a fresh qualified package using `--prepare` and the canonical activation-v2
evidence root. Then run `--stabilize` with the same path options:

```sh
python3 scripts/termdeck-web-activate.py --stabilize \
  --transaction /absolute/canonical/evidence/tx-NEW \
  --recover-from /absolute/canonical/evidence/tx-FAILED \
  --failed-receipt-sha256 REVIEWED_OLD_RECEIPT_SHA256
```

Review the returned record before execution. It binds the unchanged old receipt
and marker, fresh candidate artifacts, installed managed release and wrappers,
loaded web services and their live process identities, signed runtime outputs,
separately observed consumer TLS identities, current LAN and certificate expiry.
Stabilization reads current runtime state without requesting broker preparation;
only the new transaction receipt changes. Private TLS bytes are not stored in
the record. Existing execute-time broker checks still apply during activation.

Execute that same transaction with `TERMDECK_ACTIVATION_APPROVED=1`. Admission
expires after ten minutes and is rechecked before the first production mutation.
Drift requires a new stabilization and review. Ordinary execute rejects any
unresolved recovery marker without this explicit admission.

The old marker remains throughout activation and rollback on failure. Only after
durable activation and fresh health, managed-release and preserved-TLS checks
does the transaction journal its discharge and remove the exact unchanged marker
with the anchored compare-and-swap publisher. The old receipt remains failed and
byte-identical. A fresh inverse must be qualified separately using the new
transaction's own retained baseline; it cannot certify the historical inverse.

If interruption occurs after durable activation but before discharge completes,
repeat `--stabilize` with `TERMDECK_ACTIVATION_APPROVED=1` on the activated transaction with the same failed-transaction
path and receipt digest. This resumes only gate discharge after rechecking the
successful release and preserved TLS. A foreign marker is never removed. A
failed discharge leaves the new successful activation in place for inspection;
it does not trigger a stale automatic inverse.
