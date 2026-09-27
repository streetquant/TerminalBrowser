# Server workspace and browser task launches

The authenticated management endpoint accepts `launch-options` and configured
`create-window` requests through the same enrolled-machine/staging bridge.
A configured creation includes a request ID, current policy revision, window
name, exactly one saved `workspace_id` or absolute server `workspace_path`,
`shell` or configured `codex` runtime, 1–8 initial tab names,
optional Codex task text, and at most 16 immutable staged references. Shell task
commands and client-supplied argv are rejected.

**Native shell** opens a terminal as the host user. **Codex (TermDeck private
account)** starts the configured native Codex runtime with the tab's own account
home. Both use the host filesystem, network and process permissions. The selected
server directory is the starting directory; the terminal can use other locations
and tools available to that host user.

## Host policy

`<session-state-directory>/web-launch-policy.json` must be an owned regular
0600 file. Its shape is:

```json
{
  "version": 1,
  "workspaces": [
    {"id": "ws_0123456789abcdef0123456789abcdef", "label": "Project", "path": "/approved/project"}
  ],
  "codex": null
}
```

Saved workspaces are convenient choices. An authenticated enrolled machine can
also select an existing server directory that the host account can search,
without adding it to the policy. With no policy, this directory option
and Native shell are available; Codex still requires explicit runtime
configuration. Upload display paths and browser labels never select host paths.

Selection resolves an initial directory alias to its canonical path and records
the directory's device/inode ancestry in the launch receipt. Start, execution
and configured resume revalidate that receipt and enter the pinned directory
through an open descriptor. Paths with spaces, symlink aliases, `..`, ordinary
homes, accessible group-writable projects and `/` are supported. The directory
must exist and be searchable by the host user. Normal operating-system
permissions determine subsequent reads and writes. TermDeck does not recursively
scan, mask or mount a restricted workspace tree.

New launch plans explicitly record version 2 with `execution=host-user`, and
workspace receipts use version 2. Cached launch options must refresh because the
policy revision includes the execution contract. An old confined plan requires a
fresh authenticated launch; starting or resuming it cannot silently broaden its
recorded authority. Already-running processes and their saved records remain
unchanged.

Managed installations start configured tabs through the verified
`t1000 agent-launch` entrypoint. Plans bind the managed root, launcher and Python
interpreter. A verified release upgrade can use those same identities; changing
the root, launcher or interpreter requires a new authenticated plan. Development
checkouts retain the Python module entrypoint.

An operator can configure the explicitly authorized system-native Codex
executable and its existing configuration/account source using:

```json
"codex": {
  "executable": "/qualified/system/codex-runtime",
  "sha256": "<64 lowercase hex digits from the qualified executable>",
  "account_source": "/qualified/existing/codex-home"
}
```

The executable's owner, canonical path, executable mode and digest are checked
before launch. The selected source may consume the existing system-native
configuration and `codex-auth` account catalogue under the user's authorization.
TermDeck uses its per-tab account adapter and private scope; it does not change
the global active login or make standalone Codex depend on TermDeck. Resolve any
configured executable/source alias to its actual host path. Qualify the runtime
and its normal host dependencies before production activation. TermDeck does not
add Codex sandbox or approval flags; the configured runtime retains its own
configuration and behavior.

The Codex adapter asks the existing TermDeck `SessionStore` to provision that
tab's private `CODEX_HOME` from the configured account source. It passes a private
`sqlite_home` override and a private `log_dir` override. Native processes keep
the host user's ordinary `HOME` and environment, with the intended per-tab
`CODEX_HOME` and private account commands. Credential refresh can replace the tab-private
`auth.json`; account lifecycle synchronization remains the TermDeck account
controller's responsibility.

The selected host shell's normal startup files provide its settings and tool
paths. Configured launches validate their saved authorization before login
profiles run and again before the runtime executes. When a tab has a private
account home, its account variables are restored after profile setup.

Private homes keep account selection, logs and mutable runtime state separate
between tabs. Processes running as the same host UID can still read or modify
other host-user-accessible files, including another tab's state or staged files.
Per-machine API authorization and private directory modes do not create separate
operating-system identities for terminals.

Scope preparation validates the configuration/assets it copies or links and
rejects unsafe aliases or conflicting identities. Normal host symlinks, hooks,
plugins and helper executables remain accessible under host permissions. The
private log directory must be owned by the host account, mode 0700, and a real
directory rather than a symlink.

For a configured native task, TermDeck initializes a writable 0600 `config.toml`
inside that tab's private home from the validated system defaults. This lets the
native trust prompt save its decision without writing the system configuration.
Preparation leaves the canonical defaults unchanged. A private receipt and a
comment marker bind the view to its home and canonical source; ordinary scope
preparation recognizes only that validated view. It preserves native edits and private
history through restart and account switching. Symlinks, hardlinks, unsafe
permissions, foreign receipts and unreceipted regular configurations are refused.
The canonical config target is checked against the same protected-storage policy
on every launch and resume, including when the tab already has a regular view.

When system defaults change, an untouched private view is refreshed atomically
before the next launch. If the tab also has configuration edits, TermDeck keeps
both versions and reports that the private configuration needs reconciliation.
Resume and account-switch planning check this conflict before stopping a live
task or changing its account. No automatic merge or reset is attempted. Interrupted
initialization or refresh can continue only while its recorded private bytes are
unchanged; otherwise it also requires reconciliation. Reconciliation is an
explicit operator action while the tab is stopped: preserve the edited view and
receipt, review the changed defaults, and authorize a new view before reapplying
the intended private edits. Do not hand-edit provenance fields to suppress a
conflict. Configuration values are omitted from failure messages.

Host tools, including configured hooks and MCP commands, use their normal host
paths and environment. Successful `--version` or `features list` diagnostics do
not establish those integrations or a completed agent task; do not publish a
runtime as qualified on that basis.

## Attachment and process boundary

The internal resolver verifies account, enrolled machine, active login,
credential generation/revocation, target window, object generation/digest,
inode/owner/modes and bytes. Only canonical host paths enter the runtime's argv.
Original client paths remain display metadata. A refresh preserves previously
attached bytes and cannot change a prepared launch to newer content.

The task executes directly as the host user from its pinned starting directory.
There is no TermDeck filesystem or process namespace sandbox. Staging reference
checks govern API resolution and the attachment paths TermDeck passes to a
launch. An authenticated host terminal has the host user's broader filesystem
access. Runtime argv is selected by the server. Codex task text follows an
explicit `--` option terminator and is one data argument; ordinary terminal input
continues to support the user's shell commands.

Durable creation receipts precede registry changes. Repeated and concurrent
submissions return the same window and tab identities. One-use permits precede
runtime execution; a replay cannot execute a task again. Partially started or
failed windows remain inspectable and require explicit recovery rather than
silently spawning replacement tasks. `started` means the tmux terminals were
started; it is not proof of successful agent login or task completion.

These tasks use `restart_policy=never`. A rejected launch or resume grant does
not fall through to a generic shell. Machine revocation prevents new staging,
resolution and launches; it does not implicitly kill an already running user's
task. Explicit terminal controls retain their existing semantics.

Rollback preserves the separate staging metadata/launch receipts and the normal
session registry. Do not delete a partially created window, private scope or
attachment merely because process startup failed.

## Qualification

`tests/test_agent_launch.py` covers native host execution, immutable original
bytes, target authorization, request replay, failed startup, policy changes,
concurrent refresh/launch, a disposable Codex-shaped runtime and private scope,
and rejection of stale or unauthorized launch plans. The fixture does not
perform a real Codex login or prove production runtime compatibility.

`tests/test_server_workspaces.py` covers arbitrary directory selection, canonical
identity replacement, normal host permissions and descriptor-based directory
selection. `tests/test_confined_account_resume.py` retains its historical filename
and verifies the configured native process and exact account restart, including
directory revalidation and refusing configuration conflicts before a live
account handoff.
`tests/test_native_config_view.py` covers private trust persistence, independent
tabs, source refresh, interruption recovery, unsafe objects and provenance.

`scripts/check_client_files_integration.py` exercises actual HTTP management,
the stdin bridge, disposable tmux creation, canonical attachment reads, a real
read of an owned file outside the starting directory, and same-request window
reuse. Its browser pass verifies the Native shell/Codex choices and their task
field behavior while retaining login, CSRF, machine and staging negatives.
Its input and cleanup address only its newly created private tmux server.
