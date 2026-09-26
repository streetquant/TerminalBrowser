# TermDeck account state and process lifecycle

TermDeck's web login, enrolled machine credentials, per-terminal Codex accounts,
and a separately installed local agent are distinct systems. This integration
operates on TermDeck's configured account source and private terminal homes.
An explicitly approved system Codex executable/configuration and codex-auth
catalogue can supply the runtime and accounts. Per-tab selection stays in
TermDeck; the canonical login and standalone runtime remain independently owned.

## Four observations

The browser **Codex accounts** panel and desktop account tooltip distinguish:

1. Canonical account inferred from the account source's effective `auth.json`
   (the selected login home's credentials for a system profile catalogue).
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

### System codex-auth profiles

The read-only adapter understands saved `accounts/*.json`, `login-homes`, the
`next-login-home` selector, and optional `account-profiles.json` aliases. The
selector chooses a home; credential content proves which account it contains.
Aliases for the same proven identity share one account entry. Contradictory,
malformed or unsafe profiles fail clearly without repairing the source.

For this profile layout, each tab retains its own bounded saved credential
copies. A guarded quiescent handoff atomically incorporates newer private
credentials into that tab's catalogue before switching accounts. Switching
alpha → beta → alpha therefore retains refreshed alpha credentials. The source
catalogue and other tabs remain unchanged. Source identity and availability
are rechecked; a removed account cannot be resurrected from a private copy.
Concurrent changes abort publication, with the prior private catalogue and
selection restored on failure. Polling remains read-only.

### Legacy shared catalogues

The pre-existing legacy-only catalogue contract retains its explicit refresh
publication policy. A newer private `auth.json` can be atomically published to
its saved account only
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

## Configured browser tasks

For a configured `web-agent` tab, account handoff retains its server launch
recipe and mount namespace. The observer verifies the approved executable,
generated argv, tab/private-scope markers, process incarnation, working directory,
and one exact conversation. The restart uses that conversation with the original
policy and immutable attachment generations; refreshed uploads cannot replace
the task's files. Generic terminal recovery recognition is unchanged.

Each continuation has a server-issued, single-use permit tied to the original
launch, tab and private home. Preparation and execution both revalidate the
original account, enrolled machine, credential generation, active login, policy,
workspace and attachment bindings. A later permit supersedes an unused one;
abandoned handoffs cancel their permit. The existing input fence, old-process
quiescence, account transaction lock and fresh-process readiness checks surround
the pointer change. A restart receipt is published only after the exact confined
conversation is observed again.

The original launch login is deliberately retained as its authorization grant.
A newly authenticated browser session does not replace it implicitly. An expired
or revoked original login, revoked machine, changed policy or unavailable
attachment blocks continuation. The error distinguishes login expiry from
machine revocation. Authorize a new launch from a current login when the old
grant is no longer valid; this release does not retarget existing grants.
Failed readiness is reported as recovery pending, never as a successful switch.

New configured Codex tasks provision private `sessions` and `archived_sessions`
directories as well as private authentication and SQLite state. They may read
approved system configuration and selected credentials but do not mount a
writable canonical conversation tree. The private-history preference persists
when the account controller prepares the scope again. Existing generic terminal
history sharing is unchanged. A configured scope already linked to external
history requires explicit migration; it is rejected without replacing that link
or discarding its conversations.

`codex-runtime-scope-prepare.py` is neither present nor referenced in this
repository's tracked source, packaging or deployment scripts. This work does
not assign its interrupt traceback to TermDeck or modify an external copy.

## Runtime qualification boundary

The account-selector smoke uses a compiled disposable process and synthetic
credentials to prove the guarded restart and preserved conversation contract.
It does not prove provider authentication.

`tests/test_confined_account_resume.py` also executes the actual configured
argv inside the real mount namespace with a compiled disposable runtime. It
covers per-tab switching, original-generation attachments, private history,
revoked/expired grants, policy changes, abandoned permits and replay. Its pane
transport is a local process fixture; the production planner, quiescence,
restart gates and receipt checks run against real process observations.

The separate cold-scope smoke requires an operator-selected absolute
`TERMDECK_REAL_CODEX_BINARY` path for the TermDeck runtime being qualified. It
does not search PATH or another agent's installation. Without that selection it
reports a skipped prerequisite; an invalid explicit selection fails. Version
and login-status probes use disposable homes and synthetic credentials. A
passing login-status probe checks local credential recognition, not a live
provider login. Production runtime compatibility and authentication still need
the selected installation and its explicitly approved account catalogue.
