# t1000 BR ledger

BR is authoritative; BV reads the same ledger. Start with the repository
[AGENTS.md](../AGENTS.md) and [handoff guide](../docs/BEADS_WORKFLOW.md).

Run `br where --json`, `br ready --json` and
`bv --robot-next --no-cache --format json` from the repository root.
Claim via BR with a unique actor; exclusive claims are enabled.

Run `br sync --flush-only --json` before an authorized commit; include only
portable allowlisted files, not SQLite, locks, local history or runtime state.
In a fresh checkout restore via `br sync --import-only --json` and verify with
`br doctor --json`.

`todo.md` is retired. Its original bytes are frozen in
[the archive](../docs/archive/todo-2026-09-22.md); current status belongs in BR.
