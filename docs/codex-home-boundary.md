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
recovery-intent state described below. TermDeck also reports the Warp Drives
attestation tuple `CODEX_STORAGE_FABRIC=warp-drives-v1`,
`CODEX_WORKSPACE_STORAGE_CLASS=ssd-cell`, and
`CODEX_BULK_STORAGE_MASKED=1` as advisory evidence; mount/device proof remains
authoritative for TermDeck admission. Managed Codex workspace processes must
independently receive the exact tuple from the canonical launcher.

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

Wrong or broken links fail closed before new links or a wrapper are published.
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
