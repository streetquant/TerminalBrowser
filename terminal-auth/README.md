# Contained Codex launcher support

`codex-contained-gates.sh` is the repository-owned gate implementation for the
user launcher at `~/.local/libexec/codex-contained-user.sh`. It deliberately
separates two concerns:

- The per-auth-scope lifetime lock is a correctness boundary for every private
  TermDeck Codex invocation, including non-interactive subcommands that may
  read or write shared credentials. A second Codex process cannot start while
  the first still owns that scope. Missing `flock` or an unsafe lock file fails
  closed with status 75.
- Startup pacing is advisory load smoothing. It uses small, runtime-backed
  shards, waits at most 0.75 seconds by default, and fails open if a pacing
  holder is wedged. A future-dated marker is ignored.
- The contained launcher snapshots the exact incoming terminal mode before
  Codex starts and restores that exact mode after the child returns and again
  from its normal EXIT cleanup. This repairs raw/TUI terminal modes left by an
  abruptly terminated child while preserving any legitimate caller-specific
  terminal settings. Recovery requires the supervising launcher itself to
  reach its cleanup path.

Managed TermDeck launches inject a private-scope-stable gate root at
`$CODEX_HOME/.termdeck-codex-lifetime` through the exact
`CODEX_TERMDECK_GATE_ROOT` variable shared with the Python backend. This keeps
the launcher lease and GUI auth transaction on the same
`scope.<sanitized-scope>.lock` inode. Outside a managed launch, the helper falls
back to `$XDG_RUNTIME_DIR/codex-contained-startup`, not `$XDG_CACHE_HOME`.
Runtime state remains appropriate for fallback process locks and avoids turning
a busy persistent disk into a machine-wide launch queue.

During the legacy-launcher transition, every new launcher retains two locks in
a fixed order for its complete lifetime: first the exact old cache lock at
`${XDG_CACHE_HOME:-$HOME/.cache}/codex-contained-startup/<sanitized-scope>.lock`,
then the canonical private `scope.<sanitized-scope>.lock`. The Python backend
uses the same order. This prevents already-running legacy launchers from being
bypassed while the canonical private lock becomes authoritative.

## Launcher integration

Use the repo-owned integrator rather than editing the live launcher by hand.
Its dry run performs exact-layout matching plus `bash -n` and, when installed,
`shellcheck` validation entirely in memory; it does not create a temporary file
or change the launcher:

```bash
python3 terminal-auth/install_contained_gates.py \
    --launcher /home/arch/.local/libexec/codex-contained-user.sh \
    --dry-run
```

After explicit authorization to change that live path, omit `--dry-run`:

```bash
python3 terminal-auth/install_contained_gates.py \
    --launcher /home/arch/.local/libexec/codex-contained-user.sh
```

The integrator accepts only the exact known legacy gate blocks or its own exact
already-integrated layout. Launcher and helper reads are bounded to 1 MiB and
256 KiB respectively. Unknown, mixed, oversized, or invalid layouts fail with
status 75 before any staging. It validates both desired files before mutation,
stages the launcher whenever the validated desired bytes differ from the live
file (including an older already-integrated generation that needs a later
additive migration such as exact TTY restoration), installs the helper first
with mode `0700`, and atomically replaces the launcher while preserving its
owner, group, and mode. An already-current launcher is left inode-stable. The
helper destination is always the launcher's sibling `codex-contained-gates.sh`.

The encoded integration contract makes these bounded changes to
`codex-contained-user.sh`:

1. Remove the `startup_gate_root=...` declaration near the top.
2. Delete the old `pace_termdeck_startup` and
   `acquire_termdeck_lifetime_lock` function bodies and the
   `termdeck_lifetime_fd` global.
3. Immediately after `termdeck_interactive` is made readonly, load the helper
   for every private TermDeck launch. `termdeck_session`, not
   `termdeck_interactive`, is the private-scope signal:

   ```bash
   gate_helper="${CODEX_TERMDECK_GATE_HELPER:-${user_home}/.local/libexec/codex-contained-gates.sh}"
   readonly gate_helper
   if [[ "$termdeck_session" == true ]]; then
       if [[ ! -r "$gate_helper" ]]; then
           printf 'codex: TermDeck launch gate helper is unavailable: %s\n' "$gate_helper" >&2
           exit 75
       fi
       # shellcheck source=/dev/null
       source "$gate_helper"
   fi
   ```

4. Replace the old lifetime-lock invocation with:

   ```bash
   lock_status=0
   if [[ "$termdeck_session" == true ]]; then
       set +e
       codex_contained_acquire_termdeck_lifetime_lock \
           "$uid" "${TERMDECK_AUTH_SCOPE_ID:-}" "$user_home"
       lock_status="$?"
       set -e
   fi
   if (( lock_status != 0 )); then
       exit "$lock_status"
   fi
   ```

5. Inside the retry loop, replace the old pacing invocation with the following
   interactive-only call:

   ```bash
   gate_status=0
   if [[ "$termdeck_interactive" == true ]]; then
       set +e
       codex_contained_pace_termdeck_startup \
           true "$uid" "${TERMDECK_AUTH_SCOPE_ID:-}"
       gate_status="$?"
       set -e
   fi
   if (( gate_status != 0 )); then
       exit "$gate_status"
   fi
   ```

Validate both files with `bash -n` and `shellcheck` before atomically replacing
the live launcher. The regression suite is
`tests/test_contained_launcher_gates.py`.
