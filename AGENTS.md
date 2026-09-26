# t1000 agent instructions

## Scope and safety

Use this repository's actual root, currently `/warp/projects/softwares/termdeck`.
Runtime state is colocated in `.termdeck/`. User-visible branding is t1000;
internal package/service/storage names remain unchanged unless authorized.
This is not a Codex runtime task.

Preserve dirty work, live terminals, saved tabs/windows, recovery recipes, working
directories, authentication and TLS. Use disposable terminals for input, kill and
bulk-action tests. Do not bypass attestation, hand-edit managed hashes, retry
rejected escalation, or apply the known unsafe shared-runtime ownership template.
Source tests do not prove installation. The user resumed the Cloudflare work on
2026-09-27 for the existing `https://maxchat.semiotech.org/t1000/` route. Inspect
and preserve its tunnel, origin protections and other shared-hostname routes;
record direct TLS, access-policy and WebSocket evidence in the existing BR tasks.
Warp storage or mount migration is not required for TermDeck and is not a
deployment prerequisite.

## BR is the sole work ledger; BV is advisory

`todo.md` is retired. Read [docs/BEADS_WORKFLOW.md](docs/BEADS_WORKFLOW.md) for
handoff, sync and fresh-checkout restore; [docs/UI_UX_STATUS.md](docs/UI_UX_STATUS.md)
is source evidence, not a replacement live task list.

Start from the repository root:

```sh
git status --short
br where --json
br sync --status --json
br coordination status --json
br ready --json
br blocked --json
bv --robot-next --no-cache --format json
```

Verify BR resolves to this checkout. Search active and closed tasks before creating
work. Inspect `br show <id> --json`, atomically claim with
`br update <id> --claim --actor <unique-session-name> --json`, then verify ownership.
Never overwrite another claim. Keep acceptance, dependencies, blockers and
verification in BR. `imported-history` records preserve prior completion, not new
runtime proof. Close only when the selected issue's acceptance passes.

Before handoff, record changed paths/commit, commands/results, blockers and next
step; release your unfinished ownership explicitly when appropriate; run
`br sync --flush-only --json` and `br sync --status --json`.
Portable JSONL/config must accompany an authorized Git commit. Databases, locks,
secrets and caches stay local. Do not commit/push unrelated changes. An exported
ledger alone is not external publication.

## Skill routing

For substantial or specialized work when no already-visible exact skill clearly
matches, run `codex-skill search --json --top 3 "<one-sentence task>"`. If one
result clearly fits, run `codex-skill activate --json "<qualified_id>"` and
follow that one skill. Load another full skill only for a separate material
domain. Skip routing for trivial work. Never enumerate the full catalog or
execute skill scripts during discovery. When installing, updating, or removing a
skill or plugin, do not report success until `codex-skill post-install --json`
has passed. Explicit lifecycle prompts arm the managed turn-end verifier; there
is no managed PostToolUse hook on ordinary tool calls.
