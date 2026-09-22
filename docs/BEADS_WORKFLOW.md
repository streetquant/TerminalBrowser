# t1000 BR/BV handoff

BR is the authoritative task ledger in `.beads/`. BV is its read-only planning
view, not a second tracker. `todo.md` is retired. This guide is workflow and
migration provenance, not a competing live work queue.

## Continue in this checkout

Run from `/warp/projects/softwares/termdeck`, or the actual root of a deliberately
relocated checkout. Do not reuse a different project's `BEADS_DB` override.

```sh
git status --short
br where --json
br sync --status --json
br coordination status --json
br ready --json
br blocked --json
bv --robot-next --no-cache --format json
```

Verify BR resolves to this checkout. Inspect a recommendation with
`br show <id> --json`, including acceptance, blockers and evidence. Search active
and closed matching tasks before creating new work. BV cannot assign or authorize
work, and its result is stale after BR mutations.

Use a unique agent/session actor and an atomic claim:

```sh
br update <id> --claim --actor <unique-session-name> --json
br show <id> --json
```

`claim_exclusive: true` refuses competing claims. Do not share a default actor,
overwrite another assignee, or bypass blocked work with `--force`. Claims have no
automatic expiry; abandoned claims require coordination, not silent stealing.
An external blocker can remain even after dependencies close; clear it only when
its actual external condition is resolved.

Record evidence with `br update <id> --append-notes ...`; tick acceptance only when
verified. Close via `br close <id> --reason ...`, not a manual status/storage edit.
Before leaving, record changed files/commit, commands/results, blockers and the
next safe step. Explicitly release your unfinished ownership when appropriate
after recording the handoff; never release someone else's claim.

## Portability and fresh-checkout restore

Allowlisted portable files:

- `.beads/issues.jsonl`: exported issues, dependencies, criteria and notes.
- `.beads/config.yaml`: portable prefix and exclusive-claim configuration.
- `.beads/metadata.json`: relative database/export filenames.
- `.beads/.gitignore` and `.beads/README.md`: exclusions and entrypoint.

SQLite, WAL/SHM, engine sidecars, locks, local backups and BV caches stay ignored.
Never commit authentication/TLS files from `.termdeck/`. Do not edit issue storage
directly; use BR.

```sh
br sync --flush-only --json
br sync --status --json
br doctor --json
br dep cycles --json
br vcs-status --json
bv --robot-next --no-cache --format json
git diff --check
```

Flush before an authorized commit and include portable files in the normal
reviewed commit. An export is **not** a Git commit, push or cross-host sync.
Preserve dirty application work; do not stage unrelated edits indiscriminately.
Run the explicit flush even when automatic export reports no dirty issues; it
also refreshes BR's merge anchor. In this migration that cleared a stale-anchor
doctor warning without editing storage or forcing a repair.

For a fresh checkout containing the exported JSONL/config but no database:

```sh
br sync --import-only --json
br where --json
br doctor --json
br ready --json
bv --robot-next --no-cache --format json
```

Do not use `br init --force`, force-import or rebuild an existing ledger. After a
pull in an existing workspace, inspect sync state and use BR's reviewed
merge/reconciliation flow for divergence; never overwrite newer work. Tools
observed here are `br 0.6.0` and `bv 0.25.0`, installed on PATH, not vendored.
A different computer needs compatible tools.

The tested fresh import restored all 40 records and BV resolved actions to the
new checkout, not the old source path. With this installed BR build, the import
also retained its automatic vacuum preimages in `.beads/.br_recovery/`; fixture
`br doctor` returned a recovery-artifact warning despite healthy data/integrity
and matching counts. Preserve and review that evidence; do not force-repair or
delete backups simply to silence a warning. The primary project doctor is clean.

## Application handoff and boundaries

The completed UI/manager/settings implementation is in the **dirty source tree**.
[UI_UX_STATUS.md](UI_UX_STATUS.md) contains historical source evidence, hashes,
limits and reproduction commands. Browser artifacts under `/tmp` may disappear;
recapture evidence for a new release. Do not reimplement finished source features
just because the old live installation lacks them.

Recorded observations at migration: live v5, broken managed-launcher binding,
absent tmux socket, missing privileged prerequisites and unsafe shared-parent
ownership templates. These are imported observations, not a new runtime audit;
revalidate before repairs. Safe activation was approved, but administrator
execution was unavailable. Preserve live/saved sessions, windows/tabs, recovery
recipes, working directories, authentication and TLS. Never hand-edit managed
hashes, retry rejected escalation, apply unsafe ownership templates or replace
missing sessions with an empty tmux server. Test input/kill on disposable terminals.

The graph separates isolated source qualification from administrator prerequisites,
publication/recovery and live validation. Publication supplies the repaired
launcher/wrappers/recovery unit; verified terminal recovery follows, and the live
smoke depends on both. Publishing v6 alone does not prove usable terminals.
Source prerequisites must not require live deployment before qualification.
Cloudflare is deferred by the user;
Warp infrastructure is deferred for separate operator coordination. No Codex
runtime work is in scope.

## Frozen migration provenance

On 2026-09-22, the user requested retiring `todo.md` into BR/BV.
There was no project ledger; BR returned `NOT_INITIALIZED`.
Migration issue: `t1000-todo-migration-ppe`.

The original 192-line file is preserved byte-for-byte at
[archive/todo-2026-09-22.md](archive/todo-2026-09-22.md), SHA-256
`be1c184a799fde5fe6a7adce3f65da2bb6b18cb66fa39f8db36109b893c1bec8`.
It is historical provenance, not an editable work queue.

All 20 unfinished/context bullets map below. Repeated activation statements were
consolidated; the private-IP TLS constraint belongs to the tunnel issue.
The additional administrator blocker `t1000-admin-nbo` derives from the production
introduction and ownership item.

| Original section and item | BR record |
| --- | --- |
| UI/UX, item 1 | `t1000-deploy-xeh` |
| Production, item 2 | `t1000-deploy-xeh` |
| Production, item 9 | `t1000-deploy-xeh` |
| UI/UX, item 2 | `t1000-physical-qzr` |
| UI/UX, item 3 | `t1000-performance-vko` |
| Production, item 1 | `t1000-qualify-kfh` |
| Production, item 3 | `t1000-recover-el7` |
| Production, item 4 | `t1000-tmux-1gj` |
| Production, item 5 | `t1000-bootstrap-0fi` |
| Production, item 6 | `t1000-recovery-service-vn4` |
| Production, item 7 | `t1000-wrappers-wt9` |
| Production, item 8 | `t1000-privilege-x9q` |
| Production, item 10 | `t1000-smoke-y48` |
| Cloudflare, item 1 | `t1000-cloud-tunnel-tw9` |
| Cloudflare, item 4 | `t1000-cloud-tunnel-tw9` |
| Cloudflare, item 2 | `t1000-cloud-policy-kce` |
| Cloudflare, item 3 | `t1000-cloud-validate-0a7` |
| Warp, item 1 | `t1000-checkpoint-7og` |
| Warp, item 2 | `t1000-cutover-o2a` |
| Warp, item 3 | `t1000-paths-4hz` |

All 21 completed bullets became closed `imported-history` records with original
text and explicit evidence limits. Their closure does not claim this migration
reran tests or deployed the application. IDs in original item order:

1. `t1000-history-1-3ml`
2. `t1000-history-2-o0f`
3. `t1000-history-3-fvt`
4. `t1000-history-4-6wz`
5. `t1000-history-5-0lf`
6. `t1000-history-6-svu`
7. `t1000-history-7-14p`
8. `t1000-history-8-cok`
9. `t1000-history-9-iz6`
10. `t1000-history-10-beo`
11. `t1000-history-11-52v`
12. `t1000-history-12-iwi`
13. `t1000-history-13-qn9`
14. `t1000-history-14-bew`
15. `t1000-history-15-8nb`
16. `t1000-history-16-0md`
17. `t1000-history-17-2dr`
18. `t1000-history-18-mo0`
19. `t1000-history-19-b51`
20. `t1000-history-20-qkz`
21. `t1000-history-21-8sq`

Migration created 18 remaining tasks, 21 historical records and its own coordination
task. Six remaining tasks are deferred (Cloudflare and Warp cleanup). These counts
describe migration only; query live BR for current state.
