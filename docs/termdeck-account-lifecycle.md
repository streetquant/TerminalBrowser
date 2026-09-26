# TermDeck account state and process lifecycle

TermDeck's web login, enrolled machine credentials, per-terminal Codex accounts,
and a separately installed local agent are distinct systems. This integration
operates on TermDeck's configured account source and private terminal homes.
It does not configure CloudMCP or rewrite the local agent installation.

## Four observations

The browser **Codex accounts** panel and desktop account tooltip distinguish:

1. Canonical account inferred from the account source's effective `auth.json`.
2. Private tab selection metadata (`current` or its private registry).
3. Effective private credentials, inferred from `auth.json` contents or an exact
   validated account-file link. A stale marker is not credential evidence.
4. The account at the last observed guarded process restart, bound to its PID,
   kernel start time, observed Codex child incarnations and inherited private
   `CODEX_HOME`.

TermDeck cannot inspect credentials cached inside an arbitrary provider process.
An absent, stale or mismatched restart receipt is reported as unobserved or
requiring inspection/restart. A receipt is lifecycle evidence, not a successful
provider login. A changed credential digest invalidates the prior observation.
PID reuse cannot validate an old receipt. Existing processes without receipts
are preserved, not claimed to have loaded the current selection.

Version 2 receipts distinguish a stopped tab, an idle shell and a guarded Codex
restart. A stopped receipt expires when the session record starts again. An idle
shell reports no running Codex account; a later child cannot inherit an earlier
child's restart evidence. Older untyped receipts require a fresh observation.

The existing account handoff proves the exact pane, process incarnation,
quiescence and recoverable conversation before changing a private pointer.
An exact running conversation is restarted in its pane; unsupported or ambiguous
processes are left unchanged with an error. Account files changing on disk alone
do not count as an activated switch. Receipt publication failure is also reported
as failure requiring inspection, even if the process was already restarted.

## Credential refresh

Credential reads are bounded and reject unsafe files, duplicate JSON fields,
partial/malformed payloads and internally inconsistent identity fields. Matching
a rotated OAuth snapshot requires both a stable account and provider principal;
account ID alone is insufficient for a multi-user account. Decoded claims are
used only for local matching, not authentication or proof of provider acceptance.
Ambiguous identities, equal-time different snapshots and older private snapshots
are preserved for review. No old snapshot is restored over a newer one.

A newer private `auth.json` can be atomically published to its saved account only
when that source has an explicit private `.termdeck-account-authority.json`:

```json
{"version":1,"owner":"termdeck","refresh_writes":true}
```

This is an administrator-owned policy decision for a TermDeck account catalogue.
The implementation does not create this policy in existing installations. It
must not be added to an unrelated agent home to make an error disappear. Without
it, newer credentials stay private and synchronization reports the missing policy.

Synchronization occurs only inside the caller's guarded, quiescent account
lifecycle. A per-catalogue lock orders TermDeck refreshes across tabs; digest
checks detect concurrent external writes before publication. The policy assumes
TermDeck owns catalogue mutation; unsupported writers that ignore its lock are
not a supported concurrent writer model. Polling itself never repairs files.
The desktop schedules the guarded same-account handoff for a detected refresh.
Web users explicitly apply/restart the selected account through the panel.

Saved credentials can remain shared by same-account scopes while `auth.json`,
selection and SQLite state stay private. Changing one tab's selection never
changes another tab's pointer or canonical login. A refreshed saved token becomes
visible to scopes already selecting that account; their prior running-process
observations become stale. Refreshes to unrelated accounts do not trigger the
desktop's per-tab activation fingerprint.

## Web actions

The existing authenticated management endpoint adds `accounts`, `switch-account`
and `switch-all-accounts`. Cookie sessions, same-origin checks and CSRF remain
mandatory. Account source comes only from the host's qualified launch policy;
clients cannot supply a home or server path. Each terminal's persisted canonical
home binding must match that source before mutation.

Switching requires exact tab IDs, observed state fingerprints, a request ID and
confirmation of the exact scope. **Switch all tabs** is separate and confirms
all currently eligible managed scopes, including stopped tabs. A changed scope
requires a new review. Outcomes are per tab; partial failures are visible and do
not undo successful independent selections. Durable session-bound receipts make
replay return the prior result. An interrupted transaction remains indeterminate
and cannot silently replay a process restart. Inspect current state before making
a new request. Revoked/deleted saved accounts cannot be selected.

No token, refresh token, full credential JSON or decoded claim is returned by
these APIs or written to diagnostic output. A digest in the private restart
receipt is not exposed as token material. `codex login --device-auth` explicitly
starts an authorization flow; repeated invocation is not evidence of an OAuth
loop caused by TermDeck.

`codex-runtime-scope-prepare.py` is neither present nor referenced in this
repository's tracked source, packaging or deployment scripts. This work does
not assign its interrupt traceback to TermDeck or modify an external copy.

## Runtime qualification boundary

The account-selector smoke uses a compiled disposable process and synthetic
credentials to prove the guarded restart and preserved conversation contract.
It does not prove provider authentication.

The separate cold-scope smoke requires an operator-selected absolute
`TERMDECK_REAL_CODEX_BINARY` path for the TermDeck runtime being qualified. It
does not search PATH or another agent's installation. Without that selection it
reports a skipped prerequisite; an invalid explicit selection fails. Version
and login-status probes use disposable homes and synthetic credentials. A
passing login-status probe checks local credential recognition, not a live
provider login. Production runtime compatibility and authentication still need
the selected TermDeck installation and its own account catalogue.
