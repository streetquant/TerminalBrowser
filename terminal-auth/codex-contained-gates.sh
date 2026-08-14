#!/usr/bin/env bash
# TermDeck-specific launch gates for codex-contained-user.sh.
#
# This file is sourced by the launcher for every private TermDeck Codex scope.
# The startup pace is deliberately advisory and interactive-only: correctness
# comes from the per-auth-scope lifetime lock, while the pace only spreads
# expensive interactive initialization across a few runtime-backed shards.  A
# wedged pacing holder must therefore never stop unrelated tabs.

codex_contained_gate_root() {
    local effective_uid="$1"
    local runtime_root="${XDG_RUNTIME_DIR:-/run/user/${effective_uid}}"

    if [[ -n "${CODEX_TERMDECK_GATE_ROOT:-}" ]]; then
        printf '%s\n' "$CODEX_TERMDECK_GATE_ROOT"
        return 0
    fi
    if [[ ! -d "$runtime_root" ]]; then
        runtime_root="${TMPDIR:-/tmp}/codex-contained-runtime-${effective_uid}"
    fi
    printf '%s/codex-contained-startup\n' "${runtime_root%/}"
}

codex_contained_legacy_gate_root() {
    local effective_user_home="$1"

    if [[ -z "$effective_user_home" || "$effective_user_home" != /* ]]; then
        printf 'codex: an absolute user home is required for the legacy TermDeck lifetime gate\n' >&2
        return 75
    fi
    printf '%s/codex-contained-startup\n' "${XDG_CACHE_HOME:-${effective_user_home}/.cache}"
}

codex_contained_ensure_private_directory() {
    local path="$1"
    local expected_uid="$2"
    local metadata
    local raw_mode
    local owner
    local permissions
    local previous_umask

    [[ ! -L "$path" ]] || {
        printf 'codex: refusing unsafe runtime directory symlink %s\n' "$path" >&2
        return 1
    }
    if [[ ! -d "$path" ]]; then
        previous_umask="$(umask)"
        umask 077
        mkdir -p -- "$path"
        umask "$previous_umask"
    fi
    [[ ! -L "$path" ]] || {
        printf 'codex: refusing unsafe runtime directory symlink %s\n' "$path" >&2
        return 1
    }
    metadata="$(stat -c '%f:%u:%a' -- "$path")" || return 1
    IFS=: read -r raw_mode owner permissions <<<"$metadata"
    if [[ ! "$raw_mode" =~ ^[0-9A-Fa-f]+$ ]] \
        || (( (16#$raw_mode & 0170000) != 0040000 )) \
        || [[ "$owner" != "$expected_uid" ]] \
        || [[ "$permissions" != 700 ]]; then
        printf 'codex: refusing unsafe runtime directory %s (expected owner/mode %s:700, found %s:%s)\n' \
            "$path" "$expected_uid" "$owner" "$permissions" >&2
        return 1
    fi
}

codex_contained_positive_integer() {
    local value="$1"
    local fallback="$2"
    local maximum="$3"
    local number

    if [[ "$value" =~ ^[0-9]{1,9}$ ]]; then
        number="$((10#$value))"
    else
        number="$fallback"
    fi
    if (( number < 1 )); then
        number="$fallback"
    fi
    if (( number > maximum )); then
        number="$maximum"
    fi
    printf '%s\n' "$number"
}

codex_contained_bounded_wait_seconds() {
    local value="$1"
    local fallback="$2"
    local maximum="$3"
    local whole
    local fraction

    if [[ ! "$value" =~ ^([0-9]{1,2})([.][0-9]{1,3})?$ ]]; then
        value="$fallback"
    fi
    [[ "$value" =~ ^([0-9]{1,2})([.][0-9]{1,3})?$ ]]
    whole="$((10#${BASH_REMATCH[1]}))"
    fraction="${BASH_REMATCH[2]:-}"
    if (( whole > maximum )) || { (( whole == maximum )) && [[ -n "$fraction" && ! "$fraction" =~ ^[.]0+$ ]]; }; then
        printf '%s\n' "$maximum"
        return 0
    fi
    printf '%s%s\n' "$whole" "$fraction"
}

codex_contained_epoch_milliseconds() {
    local value

    value="$(date +%s%3N)"
    [[ "$value" =~ ^[0-9]{10,16}$ ]] || return 1
    printf '%s\n' "$value"
}

codex_contained_scope_key() {
    local scope_id="$1"
    if [[ ! "$scope_id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
        printf 'codex: an unsafe TermDeck auth scope was supplied for a private launch\n' >&2
        return 75
    fi
    printf '%s\n' "$scope_id"
}

codex_contained_startup_shard() {
    local scope_id="$1"
    local shard_count="$2"
    local checksum

    checksum="$(printf '%s' "$scope_id" | cksum)"
    checksum="${checksum%% *}"
    [[ "$checksum" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$((checksum % shard_count))"
}

codex_contained_safe_lock_path() {
    local path="$1"

    if [[ -L "$path" ]] || { [[ -e "$path" ]] && [[ ! -f "$path" ]]; }; then
        return 1
    fi
}

codex_contained_safe_lock_fd() {
    local fd="$1"
    local path="$2"
    local expected_uid="$3"
    local metadata
    local raw_mode
    local owner
    local links
    local permissions
    local device
    local inode
    local path_mode
    local path_device
    local path_inode
    local path_identity

    codex_contained_safe_lock_path "$path" || return 1
    metadata="$(stat -Lc '%f:%u:%h:%a:%d:%i' -- "/proc/$$/fd/${fd}")" || return 1
    IFS=: read -r raw_mode owner links permissions device inode <<<"$metadata"
    path_identity="$(stat -c '%f:%d:%i' -- "$path")" || return 1
    IFS=: read -r path_mode path_device path_inode <<<"$path_identity"

    [[ "$raw_mode" =~ ^[0-9A-Fa-f]+$ ]] || return 1
    [[ "$path_mode" =~ ^[0-9A-Fa-f]+$ ]] || return 1
    [[ "$owner" == "$expected_uid" ]] || return 1
    [[ "$links" == 1 ]] || return 1
    [[ "$permissions" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (16#$raw_mode & 0170000) == 0100000 )) || return 1
    (( (16#$path_mode & 0170000) == 0100000 )) || return 1
    (( (8#$permissions & 0022) == 0 )) || return 1
    [[ "${device}:${inode}" == "${path_device}:${path_inode}" ]]
}

codex_contained_pace_termdeck_startup() {
    local interactive="$1"
    local effective_uid="$2"
    local scope_id="$3"

    [[ "$interactive" == true ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0

    local gate_root
    local shard_count
    local spacing_ms
    local wait_seconds
    local shard
    local gate_fd
    local last_launch=""
    local now_ms
    local elapsed_ms
    local remaining_ms
    local sleep_seconds

    gate_root="$(codex_contained_gate_root "$effective_uid")"
    codex_contained_ensure_private_directory "$gate_root" "$effective_uid" || return 75
    shard_count="$(codex_contained_positive_integer "${CODEX_TERMDECK_STARTUP_SHARDS:-8}" 8 64)"
    spacing_ms="$(codex_contained_positive_integer "${CODEX_TERMDECK_STARTUP_SPACING_MS:-250}" 250 5000)"
    wait_seconds="$(codex_contained_bounded_wait_seconds "${CODEX_TERMDECK_STARTUP_WAIT_SECONDS:-0.75}" 0.75 2)"
    shard="$(codex_contained_startup_shard "$scope_id" "$shard_count")" || return 0

    local lock_path="${gate_root}/startup.${shard}.lock"
    if ! codex_contained_safe_lock_path "$lock_path" || ! exec {gate_fd}>>"$lock_path"; then
        printf 'codex: startup pace shard %s is unavailable; continuing without the optional delay\n' \
            "$shard" >&2
        return 0
    fi
    if ! flock -w "$wait_seconds" "$gate_fd"; then
        # Pacing is only load smoothing.  The lifetime lock below remains the
        # fail-closed guard against two Codex processes sharing one auth scope.
        printf 'codex: startup pace shard %s is busy; continuing without the optional delay\n' \
            "$shard" >&2
        exec {gate_fd}>&-
        return 0
    fi

    now_ms="$(codex_contained_epoch_milliseconds)" || {
        flock -u "$gate_fd"
        exec {gate_fd}>&-
        return 0
    }
    if [[ -r "${gate_root}/startup.${shard}.last" ]]; then
        IFS= read -r last_launch <"${gate_root}/startup.${shard}.last" || true
    fi

    # A clock correction or corrupt/future marker cannot produce a long sleep.
    # Only a recent timestamp at or before the current wall clock is honored.
    if [[ "$last_launch" =~ ^[0-9]{10,16}$ ]] && (( 10#$last_launch <= 10#$now_ms )); then
        elapsed_ms="$((10#$now_ms - 10#$last_launch))"
        if (( elapsed_ms < spacing_ms )); then
            remaining_ms="$((spacing_ms - elapsed_ms))"
            printf -v sleep_seconds '%d.%03d' "$((remaining_ms / 1000))" "$((remaining_ms % 1000))"
            sleep "$sleep_seconds"
            now_ms="$(codex_contained_epoch_milliseconds)" || true
        fi
    fi
    if [[ "$now_ms" =~ ^[0-9]{10,16}$ ]]; then
        printf '%s\n' "$now_ms" >"${gate_root}/startup.${shard}.last"
    fi
    flock -u "$gate_fd"
    exec {gate_fd}>&-
}

CODEX_TERMDECK_LIFETIME_FD=""
CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
CODEX_TERMDECK_LIFETIME_SCOPE=""

codex_contained_acquire_termdeck_lifetime_lock() {
    local effective_uid="$1"
    local scope_id="$2"
    local effective_user_home="${3:-}"

    if ! command -v flock >/dev/null 2>&1; then
        printf 'codex: flock is required to guard a TermDeck auth scope\n' >&2
        return 75
    fi

    local gate_root
    local legacy_gate_root
    local scope_key
    local wait_seconds
    local previous_umask
    local lock_path
    local legacy_lock_path

    legacy_gate_root="$(codex_contained_legacy_gate_root "$effective_user_home")" || return 75
    gate_root="$(codex_contained_gate_root "$effective_uid")"
    codex_contained_ensure_private_directory "$legacy_gate_root" "$effective_uid" || return 75
    codex_contained_ensure_private_directory "$gate_root" "$effective_uid" || return 75
    scope_key="$(codex_contained_scope_key "$scope_id")" || return 75
    legacy_lock_path="${legacy_gate_root}/${scope_key}.lock"
    lock_path="${gate_root}/scope.${scope_key}.lock"
    if [[ -n "$CODEX_TERMDECK_LIFETIME_FD" || -n "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" ]]; then
        if [[ "$CODEX_TERMDECK_LIFETIME_SCOPE" != "$scope_id" ]]; then
            printf 'codex: launcher already holds a lifetime lock for another TermDeck auth scope\n' >&2
            return 75
        fi
        if [[ -e "/proc/$$/fd/${CODEX_TERMDECK_LEGACY_LIFETIME_FD}" ]] \
            && [[ -e "/proc/$$/fd/${CODEX_TERMDECK_LIFETIME_FD}" ]] \
            && codex_contained_safe_lock_fd \
                "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" "$legacy_lock_path" "$effective_uid" \
            && codex_contained_safe_lock_fd \
                "$CODEX_TERMDECK_LIFETIME_FD" "$lock_path" "$effective_uid"; then
            return 0
        fi
        if [[ -n "$CODEX_TERMDECK_LIFETIME_FD" ]]; then
            exec {CODEX_TERMDECK_LIFETIME_FD}>&-
        fi
        if [[ -n "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" ]]; then
            exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        fi
        CODEX_TERMDECK_LIFETIME_FD=""
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        CODEX_TERMDECK_LIFETIME_SCOPE=""
        printf 'codex: an existing lifetime lock for TermDeck auth scope %s is unsafe\n' "$scope_id" >&2
        return 75
    fi
    CODEX_TERMDECK_LIFETIME_FD=""
    CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
    CODEX_TERMDECK_LIFETIME_SCOPE=""

    wait_seconds="$(codex_contained_bounded_wait_seconds "${CODEX_TERMDECK_LIFETIME_WAIT_SECONDS:-10}" 10 30)"

    previous_umask="$(umask)"
    umask 077
    if ! codex_contained_safe_lock_path "$legacy_lock_path" \
        || ! exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>>"$legacy_lock_path"; then
        umask "$previous_umask"
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        printf 'codex: cannot open the legacy lifetime lock for TermDeck auth scope %s\n' "$scope_id" >&2
        return 75
    fi
    if ! codex_contained_safe_lock_fd \
            "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" "$legacy_lock_path" "$effective_uid"; then
        umask "$previous_umask"
        exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        printf 'codex: the legacy lifetime lock for TermDeck auth scope %s is unsafe\n' "$scope_id" >&2
        return 75
    fi
    if ! flock -n "$CODEX_TERMDECK_LEGACY_LIFETIME_FD"; then
        printf 'codex: waiting up to %s seconds for the previous legacy Codex process in this TermDeck terminal to exit...\n' \
            "$wait_seconds" >&2
        if ! flock -w "$wait_seconds" "$CODEX_TERMDECK_LEGACY_LIFETIME_FD"; then
            umask "$previous_umask"
            printf 'codex: the previous legacy Codex process is still running; refusing an overlapping launch\n' >&2
            exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
            CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
            return 75
        fi
    fi
    if ! codex_contained_safe_lock_fd \
        "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" "$legacy_lock_path" "$effective_uid"; then
        umask "$previous_umask"
        exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        printf 'codex: the acquired legacy lifetime lock for TermDeck auth scope %s became unsafe\n' "$scope_id" >&2
        return 75
    fi
    if ! codex_contained_safe_lock_path "$lock_path" || ! exec {CODEX_TERMDECK_LIFETIME_FD}>>"$lock_path"; then
        umask "$previous_umask"
        exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        CODEX_TERMDECK_LIFETIME_FD=""
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        CODEX_TERMDECK_LIFETIME_SCOPE=""
        printf 'codex: cannot open the lifetime lock for TermDeck auth scope %s\n' "$scope_id" >&2
        return 75
    fi
    umask "$previous_umask"
    if ! codex_contained_safe_lock_fd \
        "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" "$legacy_lock_path" "$effective_uid" \
        || ! codex_contained_safe_lock_fd \
            "$CODEX_TERMDECK_LIFETIME_FD" "$lock_path" "$effective_uid"; then
        exec {CODEX_TERMDECK_LIFETIME_FD}>&-
        exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        CODEX_TERMDECK_LIFETIME_FD=""
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        CODEX_TERMDECK_LIFETIME_SCOPE=""
        printf 'codex: the lifetime lock for TermDeck auth scope %s is unsafe\n' "$scope_id" >&2
        return 75
    fi
    if ! flock -n "$CODEX_TERMDECK_LIFETIME_FD"; then
        printf 'codex: waiting up to %s seconds for the previous Codex process in this TermDeck terminal to exit...\n' \
            "$wait_seconds" >&2
        if ! flock -w "$wait_seconds" "$CODEX_TERMDECK_LIFETIME_FD"; then
            printf 'codex: the previous Codex process is still running; refusing an overlapping launch\n' >&2
            exec {CODEX_TERMDECK_LIFETIME_FD}>&-
            exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
            CODEX_TERMDECK_LIFETIME_FD=""
            CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
            CODEX_TERMDECK_LIFETIME_SCOPE=""
            return 75
        fi
    fi
    if ! codex_contained_safe_lock_fd \
        "$CODEX_TERMDECK_LEGACY_LIFETIME_FD" "$legacy_lock_path" "$effective_uid" \
        || ! codex_contained_safe_lock_fd \
            "$CODEX_TERMDECK_LIFETIME_FD" "$lock_path" "$effective_uid"; then
        exec {CODEX_TERMDECK_LIFETIME_FD}>&-
        exec {CODEX_TERMDECK_LEGACY_LIFETIME_FD}>&-
        CODEX_TERMDECK_LIFETIME_FD=""
        CODEX_TERMDECK_LEGACY_LIFETIME_FD=""
        CODEX_TERMDECK_LIFETIME_SCOPE=""
        printf 'codex: an acquired lifetime lock for TermDeck auth scope %s became unsafe\n' "$scope_id" >&2
        return 75
    fi
    CODEX_TERMDECK_LIFETIME_SCOPE="$scope_id"
}
