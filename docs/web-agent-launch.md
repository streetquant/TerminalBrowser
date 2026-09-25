# Approved browser task launches

The authenticated management endpoint accepts `launch-options` and configured
`create-window` requests through the same enrolled-machine/staging bridge.
A configured creation includes a request ID, current policy revision, window
name, workspace ID, `shell` or configured `codex` runtime, 1–8 initial tab names,
optional Codex task text, and at most 16 immutable staged references. Shell task
commands and client-supplied argv are rejected.

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

With no policy, no server workspace is offered. There is no inferred grant from
an uploaded client path, browser label, current process directory, or login.
Workspace roots must have real, non-writable-by-others path components. Runtime
state and broad home/root mounts are rejected. The established repository
layout may contain `.termdeck/state`; `.termdeck` and `.beads` are masked inside
the task, then only specific approved attachments/private auth are mounted.

An operator can configure TermDeck's own compatible Codex executable using:

```json
"codex": {
  "executable": "/qualified/termdeck/codex-runtime",
  "sha256": "<64 lowercase hex digits from the qualified executable>",
  "account_source": "/qualified/termdeck/account-source"
}
```

The executable's owner, path, executable mode and digest are checked before
launch. This is explicit host policy, not permission to discover or modify an
unrelated local Codex or CloudMCP installation. Qualify the actual configured
runtime under the mount contract before production activation. Arbitrary
host-launcher dependencies are not automatically exposed through the sandbox.

The Codex adapter asks the existing TermDeck `SessionStore` to provision that
tab's private `CODEX_HOME` from the configured account source. It passes a private
`sqlite_home` override. Only that home, the selected credential when linked,
and explicitly shared non-auth configuration/assets are available. The account
catalogue and other tab homes are absent. `.env` is excluded from extra mounts.
Credential refresh can replace the tab-private `auth.json`; account lifecycle
synchronization remains the TermDeck account controller's responsibility.

## Attachment and process boundary

The internal resolver verifies account, enrolled machine, active login,
credential generation/revocation, target window, object generation/digest,
inode/owner/modes and bytes. Only canonical host paths enter the runtime's argv.
Original client paths remain display metadata. A refresh preserves previously
attached bytes and cannot change a prepared launch to newer content.

The task executes inside `/usr/bin/bwrap`: a private process/mount namespace,
public system files under `/usr`, minimal `/dev`, private `/tmp`, approved
workspace, read-only attachment directories, and the one optional private Codex
scope. Network access is retained for the chosen agent. Paths outside these
mounts are absent; attachment permission does not expose their parent namespace.
Runtime argv is fixed by the server. Codex task text is one data argument.

Durable creation receipts precede registry changes. Repeated and concurrent
submissions return the same window and tab identities. One-use permits precede
runtime execution; a replay cannot execute a task again. Partially started or
failed windows remain inspectable and require explicit recovery rather than
silently spawning replacement tasks. `started` means the tmux terminals were
started; it is not proof of successful agent login or task completion.

These tasks use `restart_policy=never`. The tmux launch path omits the usual
host-shell fallback for their `web-agent` tag: a successful exit or Ctrl-C cannot
drop outside the mount boundary. Machine revocation prevents new staging,
resolution and launches; it does not implicitly kill an already running user's
task. Explicit terminal controls retain their existing semantics.

Rollback preserves the separate staging metadata/launch receipts and the normal
session registry. Do not delete a partially created window, private scope or
attachment merely because process startup failed.

## Qualification

`tests/test_agent_launch.py` covers the real mount namespace, immutable original
bytes, target authorization, request replay, failed startup, policy changes,
concurrent refresh/launch, a disposable Codex-shaped runtime and private scope,
and the absence of an unrestricted fallback. The fixture does not perform a
real Codex login or prove production runtime compatibility.

`scripts/check_client_files_integration.py` exercises actual HTTP management,
the stdin bridge, disposable tmux creation, canonical file reads inside the
confined terminal, and same-request window reuse. Its input and cleanup address
only its newly created private tmux server.
