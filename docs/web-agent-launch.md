# Server workspace and browser task launches

The authenticated management endpoint accepts `launch-options` and configured
`create-window` requests through the same enrolled-machine/staging bridge.
A configured creation includes a request ID, current policy revision, window
name, exactly one saved `workspace_id` or absolute server `workspace_path`,
`shell` or configured `codex` runtime, 1–8 initial tab names,
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

Saved workspaces are convenient choices. An authenticated enrolled machine can
also select an existing server directory that the host account can read and
search, without adding it to the policy. With no policy, this directory option
and the confined shell are available; Codex still requires explicit runtime
configuration. Upload display paths and browser labels never select host paths.

Selection resolves an initial directory alias to its canonical path and records
the directory's device/inode ancestry in the immutable launch receipt. Start,
execution and configured resume revalidate that receipt. Bubblewrap binds an
open directory descriptor, preventing a final path substitution from redirecting
the grant. Paths with spaces and accessible group-writable projects work.
Readable directories without write permission are mounted read-only; a receipt
prepared read-only cannot later gain write authority.

Broad parent directories, including ordinary home directories, are supported.
Nested `.termdeck`, `.beads`, private authentication and account catalogues,
TermDeck release/control namespaces, and host sockets/devices/FIFOs are masked.
Reserved private-directory aliases such as `.codex` have their canonical target
masked too. Only exact authorized attachment copies and the selected tab's
private scope are mounted back beneath those masks. A bounded metadata scan
fails clearly if it cannot establish containment (at most 1,000,000 entries,
128 levels and 4,096 masks). The filesystem root `/` and kernel/runtime control
trees `/proc`, `/sys`, `/dev` and `/run` cannot be selected as workspaces.
Old prepared receipts retain their original saved path and validation contract.

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
configured executable/source alias to its actual host path. Qualify that runtime
under the mount contract before production activation. Host-launcher dependencies
are not automatically exposed through the sandbox.

The Codex adapter asks the existing TermDeck `SessionStore` to provision that
tab's private `CODEX_HOME` from the configured account source. It passes a private
`sqlite_home` and `log_dir` overrides. Only that home, the selected credential when linked,
and explicitly shared non-auth configuration/assets are available. The account
catalogue and other tab homes are absent. `.env` is excluded from extra mounts.
Credential refresh can replace the tab-private `auth.json`; account lifecycle
synchronization remains the TermDeck account controller's responsibility.

Prepared non-auth assets, including additional `*.config.toml` files, are mounted
read-only at their validated canonical and link destinations. This keeps an
external skills-directory alias reachable without exposing its parent or
rewriting host/private links. Conflicting mount destinations and aliases into
protected storage are rejected. The private log directory must be owned by the
host account, mode 0700, and a real directory rather than a symlink.

This does not recursively grant targets of nested skill/plugin symlinks or
executables named by configuration. A configured hook or MCP command outside
the admitted mounts needs separate dependency qualification. Successful
`--version` or `features list` diagnostics do not establish those integrations
or a completed agent task; do not publish a runtime as qualified on that basis.

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
Runtime argv is fixed by the server. Codex task text follows an explicit `--`
option terminator and is one data argument.

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

`tests/test_server_workspaces.py` covers arbitrary directory selection, canonical
identity replacement, read-only grants, broad parent masking and real descriptor
mounts. `tests/test_confined_account_resume.py` verifies the configured native
process and exact confined account restart, including directory revalidation.

`scripts/check_client_files_integration.py` exercises actual HTTP management,
the stdin bridge, disposable tmux creation, canonical file reads inside the
confined terminal, and same-request window reuse. Its input and cleanup address
only its newly created private tmux server.
