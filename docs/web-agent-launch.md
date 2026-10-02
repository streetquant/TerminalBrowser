# Server workspace and browser task launches

The authenticated management endpoint accepts `launch-options` and configured
`create-window` requests through the same enrolled-machine/staging bridge.
A configured creation includes a request ID, current policy revision, window
name, exactly one saved `workspace_id` or absolute server `workspace_path`,
`shell` or configured `codex` runtime, 1–8 initial tab names,
optional Codex task text, and at most 16 immutable staged references. Shell task
commands and client-supplied argv are rejected.

The web command palette lists a selected server directory's immediate child
directories. Its dropdown offers each child and an explicit **All** choice.
Submitting a prompt creates one authenticated Codex window per selected child;
the prompt is one data argument, and each create request has its own durable
replay identity. The palette's `--yolo` mode is an explicit Codex runtime flag,
never shell syntax or an arbitrary client-supplied command. Existing launches
without that flag keep their configured runtime behavior.
The directory picker can list up to 768 immediate children within a 128 KiB
response budget. A single directory remains selectable from a larger list;
**All** is enabled for at most 32 children to bound simultaneous agent starts.
Choose a narrower parent for a larger batch.

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
and its normal host dependencies before production activation. Ordinary
configured launches do not add Codex sandbox or approval flags; the explicit
command-palette `--yolo` choice changes only those palette launches.

The Codex adapter asks the existing TermDeck `SessionStore` to provision that
tab's private `CODEX_HOME` from the configured account source. It passes a private
`sqlite_home` override and a private `log_dir` override. Native processes keep
the host user's ordinary `HOME` and environment, with the intended per-tab
`CODEX_HOME` and private account commands. Credential refresh can replace the tab-private
`auth.json`; account lifecycle synchronization remains the TermDeck account
controller's responsibility.

New configured Codex tasks use the installed CLI's `--no-alt-screen` mode, so
ordinary terminal scrollback can retain their output. They also pass
`--no-daemon` on initial launch and resume: a shared daemon can retain another
home's startup account and state. The configured executable must advertise both
arguments. TermDeck does not restart an existing Codex process or rewrite
commands typed manually into a shell tab.

### Reviewed native policy upgrades

After a global `npm install -g @openai/codex` changes the native executable, the
next policy admission automatically runs the same native qualification used by
the policy producer. For a stored npm runtime it first runs the pinned shared
preflight helper at `/home/arch/.local/libexec/codex-npm-platform-preflight.py`
(SHA-256 `f50a5b962bd651fe215ebbf96792e74cfbe7c14f497bb1952d8f1dea583890db`)
against the same prefix's `bin/codex` entrypoint. That helper uses Node's current
`createRequire` result, so it selects a same-prefix nested or flat platform
package, or the supported bundled fallback. It does not follow a stale native
path when an npm update leaves the old package behind, and it can resolve the
replacement when that old file has been removed. TermDeck then requires the
resolved path to match one of those layouts under the already approved prefix,
qualifies an executable x86_64 ELF, checks `--version` and the required help
flags, and uses a locked compare-and-swap to change only the saved executable
path and digest. It probes in fresh private homes without inherited credentials
or configuration. If the helper is missing, changed, or rejects the package,
the saved policy stays unchanged and configured launches fail closed. A policy
revision change invalidates plans made against the old digest; retry the policy
read and create a fresh launch plan.

Policies that explicitly pin an executable outside these npm layouts retain
their existing exact-digest admission and do not use the automatic npm refresh.
An executable inside a recognized npm layout cannot fall back to that behavior
when the shared preflight rejects its entrypoint or package ancestry.

Each successful automatic refresh retains a private `.web-launch-policy-auto-*`
bundle inside the TermDeck state directory with the exact before/after policy,
native source receipts, validation copy, and manifest. This local qualification
checks the installed package layout and executable behavior. It does not
authenticate the npm publisher, prove provider login, or establish successful
interactive execution. Existing terminals remain pinned to their running
processes. The refresh does not edit npm's installation, select an account, or
restart tabs or services.

Rollback restores the exact previous policy and writes a private pause record
bound to that policy digest, executable path, qualified native digest, and
version. The same installed binary will remain held after rollback instead of
being immediately refreshed again. A different package binary can be qualified
on a later policy read. To intentionally re-admit the held binary, use the
operator-reviewed `launch-policy prepare` and `apply` route below.

The `t1000 launch-policy` command also supports an operator-reviewed policy
upgrade. It does not install Codex, select an account, start a terminal, restart
a service, or change managed release attestations. Build and install the
TermDeck CLI through the normal verified release workflow before using the
command in an installed system. This explicit route is useful when an operator
wants to qualify a package before the next launch admission.

Preparation requires the exact existing policy digest, the absolute native ELF
path currently selected by the same-prefix `bin/codex` entrypoint, and a new
output directory inside an owned 0700 directory. It uses the pinned shared
preflight helper and accepts only its nested, flat or bundled Linux x64 result.
It discovers the stable version from the current main npm package and requires
the native version/help probes to agree. An optional `--version` pins an
independently reviewed expected version. It saves the exact policy backup,
candidate and review manifest without changing the target policy:

```sh
t1000 launch-policy prepare \
  --state-dir /absolute/session-state \
  --expected-policy-sha256 <reviewed-current-policy-digest> \
  --native /absolute/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex \
  --output /absolute/private-review/new-bundle
```

Review `before.json`, `after.json` and `manifest.json`. Only the Codex executable
path and digest may differ; workspaces and the account source must remain the
same. The shared preflight checks the main and Linux x64 npm package names,
versions, dependency relationship and physical ancestry. TermDeck checks file
owners and permissions, descriptor/path identity, and a native ELF header.
Version/help probes execute the pinned descriptor in fresh private homes
without inherited credentials or configuration. Those checks
establish local identity and argument support; they do not authenticate a package
publisher, prove provider login, or establish successful interactive execution.
If the main and platform package metadata disagree, the executable probes fail,
or the reviewed preimage has changed, prepare refuses to create a publishable
manifest. This is a digest refresh after qualification, not a way to bless an
arbitrary executable or bypass the pinned package layout.

After reviewing the manifest digest returned by preparation, publication uses:

```sh
t1000 launch-policy apply --state-dir /absolute/session-state \
  --bundle /absolute/private-review/new-bundle \
  --manifest-sha256 <reviewed-manifest-digest>
```

The command requalifies the native files and the existing launch reader before
publication. It uses a fixed native flock, checks the exact old bytes and file
identity, fsyncs an exclusively created temporary file, atomically replaces the
policy, and fsyncs its parent. Changes to the old policy, review files, package
files or directory identities require a fresh preparation. Symlinks, hardlinks,
foreign ownership, writable-by-others package paths and ambiguous paths are
rejected. The flock serializes this supported producer; unrelated processes
running as the same host UID must not write the policy concurrently.

For an exact rollback, use the same bundle and reviewed digest:

```sh
t1000 launch-policy rollback --state-dir /absolute/session-state \
  --bundle /absolute/private-review/new-bundle \
  --manifest-sha256 <reviewed-manifest-digest>
```

Rollback requires the current policy to equal the reviewed candidate and restores
the backup bytes, including an old digest that may intentionally fail native
admission. It does not downgrade the executable or overwrite later operator
edits. The exact-binary pause record prevents an automatic refresh from undoing
that rollback. Retain the bundle until acceptance and any rollback are complete.
A failed publication, especially a post-rename durability failure, requires
inspecting the retained evidence and actual policy; an error does not promise
that the old policy remains active. No automatic reset or service restart is
attempted.

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
