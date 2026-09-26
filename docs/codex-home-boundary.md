# Canonical Codex configuration with private terminal state

TermDeck gives each managed terminal a private `CODEX_HOME` so selecting a
Codex account in one tab cannot change another tab. That private home must not
become a second, stale Codex installation. The canonical Codex home remains the
authority for configuration and reusable assets.

## Warp Drives managed release state

The managed release launcher accepts an absolute `T1000_MANAGED_ROOT` and
fails closed unless the launcher, bootstrap, and root are real paths on the
expected non-rotational SSD cell. The default release root is
`/home/arch/.local/state/codex-project-homes/runtime/t1000-managed`. Mutable
TermDeck state is kept in the adjacent
`<T1000_MANAGED_ROOT parent>/termdeck/` directory, including the primary
`sessions.sqlite3` registry, managed `terminal-auth/` scopes, and the
recovery-intent state described below. TermDeck reports a direct-root attestation derived from the observed mount,
device, filesystem, and rotational checks. Environment labels are advisory
metadata only. The direct-root proof does not infer a workspace cell or bulk
storage masking. Managed Codex workspace processes receive the attestation
receipt from the canonical launcher when one is available.

### Recovery-intent managed state

| Managed-state sibling | Lifecycle and authority |
| --- | --- |
| `recovery-intents/` | Durable pending requests, one canonical sidecar per session. A matching repeat returns the same request id; the sidecar remains until a durable acknowledgement exists and consumption completes, or rejection moves it to quarantine. |
| `recovery-intent-acks/` | Durable terminal acknowledgements for consumed requests. An acknowledgement makes a matching retry idempotent only while the managed row still has the same identity and fingerprint with active recovery intent; stale acknowledgements are removed and malformed ones are quarantined. |
| `recovery-intent-locks/` | Private per-session coordination files. They serialize only the same session, retire when no pending sidecar remains, and are not recovery authority or durable backup material. |
| `recovery-intent-tmp/` | Transient staging for atomic intent and acknowledgement publication. Orphan cleanup is entry- and time-bounded; unsafe residue is quarantined rather than followed or trusted. |
| `recovery-intent-quarantine/` | Retained rejected, malformed, or unsafe artifacts for audit. Quarantined files are excluded from active recovery and are not automatically consumed as requests. |

A quiescent managed-state backup should keep `sessions.sqlite3`,
`recovery-intents/`, and `recovery-intent-acks/` as one logical set. Retain
`recovery-intent-quarantine/` when its audit evidence is required;
`recovery-intent-locks/` and `recovery-intent-tmp/` are reproducible
coordination and staging state, not durable backup inputs. After restore,
pending requests still require the managed fingerprint check and
acknowledgements still require the matching active database row, so a stale
backup artifact cannot authorize a changed session identity.

Legacy `~/.local/share/termdeck/` state is an import source, not a second
managed authority. Only the primary GUI may explicitly import the legacy
`sessions.sqlite3` registry into the managed database, and that operation does
not copy or move `terminal-auth`, Codex configuration, conversation roots, or
other legacy state. Existing private `CODEX_HOME` paths and auth-scope records
remain immutable during database migration. A separate guarded recovery path
may rebind an exact native Codex resume to a sparse managed-SSD scope only
after a durable recovery claim and independent missing/dead/quiescent backend
proof. It preserves the exact selected account, shares canonical session and
account roots, publishes the registry change by compare-and-swap, and records
a durable receipt. The legacy scope is never copied, moved, or deleted.

External sessions, `codex-opencode`, unknown account selections, inexact resume
recipes, incomplete lifecycle proof, and lost claims remain on the existing
binding and fail closed rather than receiving an implicit path rewrite.

`prepare_shared_codex_configuration()` applies this boundary:

| Shared from the canonical Codex home | Kept private per terminal |
| --- | --- |
| `config.toml` and top-level `*.config.toml` profiles | `auth.json` and `current` account selection |
| `AGENTS.md`, `AGENTS.override.md`, `.env`, and `hooks.json` | `state_*.sqlite` and their journal/WAL files |
| `agents/`, `rules/`, `skills/`, `plugins/`, `memories/`, `memories_extensions/` | the generated `bin/codex` wrapper |
| `hooks/`, `themes/`, `pets/`, and `prompts/` | `.termdeck-canonical-home`, retained migration backups, and other terminal-local runtime files |

Conversation roots are shared separately by the auth-scope preparation code;
they are not migrated by this helper.

The canonical `config.toml` may itself set `sqlite_home`. Each private scope's
`bin/codex` therefore executes the verified host launcher with this prefix:

```text
codex -c 'sqlite_home="<private-CODEX_HOME>"' <original arguments...>
```

Codex applies root `-c` overrides above user and profile config layers, so the
SQLite database stays private without copying the rest of the configuration.
The wrapper exports `CODEX_HOME`, does not replace the shell's `HOME`, and
rejects another TermDeck private wrapper as its launcher to prevent recursion.
It also rejects any user-supplied `-c`/`--config` value containing
`sqlite_home`; Codex applies duplicate CLI overrides in argument order, so
allowing a later duplicate would defeat terminal-private state.
In production, `TERMDECK_CODEX_BINARY` may select only the recognized managed
Codex launcher whose installed identity and helper contract pass attestation.
An arbitrary absolute executable, a binary merely named `codex`, or a PATH
position is not trust evidence and is rejected. An injected launcher attestor
exists for release verification and deterministic tests; it is not a
user-configurable production contract.

Reconciliation is intentionally non-destructive. Missing canonical assets are
linked, and existing links must resolve to the expected canonical target. An
existing regular file, directory, or custom `bin/codex` is left untouched and
returned in `migration_required_paths`; in that case `status.ready` is false.
The canonical source is also persisted as a private
`.termdeck-canonical-home` binding, so a later lifecycle migration uses the
same resolved authority and rejects an unexpected source.

`migrate_quiescent_codex_configuration(private_home)` is the only destructive
consumer of that migration list. Its caller must already hold the scope's auth
transaction lock and must have exact backend evidence that the tmux session is
absent. Under the per-scope configuration lock, it rechecks every legacy path,
moves it to a unique mode-`0700` directory below
`.termdeck-config-backups/`, publishes the canonical links and managed wrapper,
and verifies parity. A failure rolls all moved paths and newly published links
back; an incomplete rollback fails closed and preserves the backup evidence.
The original backup is retained after success. Repeating the migration is an
idempotent no-op with no new backup.

### Explicit repair of a stale canonical binding

After an authorized system configuration move, an operator can repair one exact
saved terminal's existing scope without starting it:

```sh
t1000 auth-rebind-config SESSION_ID \
  --expected-source /8tb/codex-home \
  --source /warp/codex
```

`--expected-source` must exactly match the saved absolute canonical binding and
each existing non-auth configuration link. The old source may be absent. The
new source is explicit; neither the agent's `CODEX_HOME` nor a launcher override
chooses it. An optional `--db` selects the registry under the usual managed
database boundary. SessionStore is opened with auth provisioning disabled.

The command verifies the saved scope, refuses a still-present saved PID or live
tmux session, and holds the auth transaction lock plus both Codex lifetime
fences. Under a registry write transaction it compares the entire saved row,
then changes only the configuration links. The final canonical binding is
replaced atomically after the other links are ready. Original links are kept in
a mode-`0700` private backup; caught failures restore them without overwriting
an unexpected concurrent destination. Changed or unsafe sources fail closed.
All session fields, credentials, selected/pending-account markers, private
SQLite state, history files, and conversation-root links remain unchanged.

By default the exact existing managed private launcher is preserved, including
its bytes, inode, and mode. The result reports `launcher_preserved: true` and
`launcher_qualified: false`: repairing configuration does not attest the host
executable or prove that a saved terminal or conversation has recovered.
`--refresh-launcher` additionally replaces the launcher only when the existing
host resolver passes its unchanged attestation policy. A successful refresh
retains the original launcher as `codex-launcher` inside the same backup.

The operation does not alter recovery intent, including for inactive or new
records. A missing saved private home is always refused; neither this command
nor `migrate_quiescent_codex_configuration` fabricates an anonymous replacement.
An unrecognized wrapper or copied private configuration requires separate
review. Replaying the command with the old expected binding after success is
refused without changing the completed migration. The Python API is
`auth_config_rebind.rebind_terminal_configuration`; its
`inspect_configuration_rebind` preflight is read-only and does not prove writer
quiescence or launch readiness.

### Restoring an existing private Bash terminal

After explicit configuration repair, the ordinary `TmuxBackend.create_or_resume`
path can restore a saved direct Bash recipe without a currently attested Codex
executable. Admission requires an absolute registered system Bash executable,
with no argument or only the single `-i` flag, no recovery command/directory,
and no configured-agent or checkpoint launch. Scripts, shell operators, other
flags, relative commands, and basename impostors retain the existing checks.

The saved home and wrapper directory must already exist with safe ownership.
The canonical binding and every configuration asset must be shared correctly;
the existing wrapper must exactly match the managed bytes for this private
home. The backend holds its normal transaction and lifetime fences while it
checks these properties. It preserves the wrapper, credentials, account
markers, private state, and history references. It writes only the managed
Bash rc file, which restores the private scope environment after the user's rc.
It neither creates missing history/state nor recreates a missing saved home.

This admission proves restoration of the shell only. Native Codex, configured
agents, native resume and checkpoints keep their existing qualification gates.
The coordinator still requires active recovery intent and proves a live pane
before updating runtime fields; configuration repair alone never starts a tab.

During ordinary preparation or automatic lifecycle migration, wrong or broken
links fail closed before new links or a wrapper are published.
If ordinary preparation's wrapper publication fails, links created by that
same reconciliation call are rolled back; previously shared links are left
untouched.

An absent or non-directory canonical home is an explicit no-op with
`canonical_available == false` and `status.ready == false`. This keeps an
anonymous terminal usable when no saved Codex home exists without implying
that configuration parity has been established. A broken canonical-home
symlink remains an error. The private home itself must be a direct directory.
The canonical home may be a symlink (the normal host layout uses one), but its
resolved directory must be distinct from and outside the private home.
Reconciliation is serialized per private home so one failed caller cannot roll
back another caller's links.

The safety boundary assumes the per-terminal home remains a mode-`0700`,
current-user-owned directory. Publication revalidates relevant paths and uses
the per-home lock; it is not intended to defend against the same OS user
concurrently renaming the entire private home behind TermDeck.
