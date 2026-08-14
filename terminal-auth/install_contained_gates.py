#!/usr/bin/env python3
"""Atomically install TermDeck's gate helper into one known launcher layout."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_HELPER = Path(__file__).with_name("codex-contained-gates.sh")
REFUSED = 75
MAX_LAUNCHER_BYTES = 1024 * 1024
MAX_HELPER_BYTES = 256 * 1024

LEGACY_ROOT_BLOCK = """\
cache_root="${XDG_CACHE_HOME:-${user_home}/.cache}/codex-contained-dbus"
readonly cache_root
startup_gate_root="${XDG_CACHE_HOME:-${user_home}/.cache}/codex-contained-startup"
readonly startup_gate_root"""

INTEGRATED_ROOT_BLOCK = """\
cache_root="${XDG_CACHE_HOME:-${user_home}/.cache}/codex-contained-dbus"
readonly cache_root"""


TTY_GUARD_BLOCK = """\
termdeck_tty_state=""
capture_termdeck_tty() {
    termdeck_tty_state=""
    if [[ -t 0 ]] && command -v stty >/dev/null 2>&1; then
        termdeck_tty_state="$(stty -g <&0 2>/dev/null || true)"
    fi
}

restore_termdeck_tty() {
    local saved_state="${termdeck_tty_state:-}"
    [[ -n "$saved_state" && -t 0 ]] || return 0
    stty "$saved_state" <&0 2>/dev/null || true
}

capture_termdeck_tty"""

LEGACY_CLEANUP_TRAP = "trap cleanup_proxy EXIT"
INTEGRATED_CLEANUP_TRAP = "trap 'restore_termdeck_tty; cleanup_proxy' EXIT"
LEGACY_CHILD_STATUS_BLOCK = """\
    status="$?"
    set -e
    elapsed="$((SECONDS - started_at))"""
INTEGRATED_CHILD_STATUS_BLOCK = """\
    status="$?"
    set -e
    restore_termdeck_tty
    elapsed="$((SECONDS - started_at))"""

LEGACY_SESSION_BLOCK = """\
termdeck_session=false
case "${CODEX_HOME:-}" in
    "${user_home}/.local/share/termdeck/terminal-auth/auth-"*)
        if [[ -n "${TERMDECK_AUTH_SCOPE_ID:-}" ]]; then
            termdeck_session=true
        fi
        ;;
esac
readonly termdeck_session"""

INTEGRATED_SESSION_BLOCK = """\
termdeck_session=false
termdeck_scope_id="${TERMDECK_AUTH_SCOPE_ID:-}"
termdeck_gate_root="${CODEX_TERMDECK_GATE_ROOT:-}"
if [[ -n "${CODEX_HOME:-}" || -n "$termdeck_scope_id" || -n "$termdeck_gate_root" ]]; then
    if [[ -z "${CODEX_HOME:-}" || -z "$termdeck_scope_id" || -z "$termdeck_gate_root" ]]; then
        printf 'codex: incomplete TermDeck auth environment; refusing a partial private launch tuple\\n' >&2
        exit 75
    fi
    if [[ "$CODEX_HOME" != /* || -L "$CODEX_HOME" || ! -d "$CODEX_HOME" ]]; then
        printf 'codex: refusing an unsafe TermDeck CODEX_HOME: %s\\n' "$CODEX_HOME" >&2
        exit 75
    fi
    home_owner_mode="$(stat -Lc '%u:%a' -- "$CODEX_HOME")" || exit 75
    if [[ "$home_owner_mode" != "${uid}:700" ]]; then
        printf 'codex: refusing an unsafe TermDeck CODEX_HOME (expected owner/mode %s:700, found %s)\\n' \\
            "$uid" "$home_owner_mode" >&2
        exit 75
    fi
    if [[ ! "$termdeck_scope_id" =~ ^auth-[A-Za-z0-9_-]+$ || "$(basename -- "${CODEX_HOME%/}")" != "$termdeck_scope_id" ]]; then
        printf 'codex: TermDeck auth scope does not match CODEX_HOME: %s\\n' "$termdeck_scope_id" >&2
        exit 75
    fi
    expected_gate_root="${CODEX_HOME%/}/.termdeck-codex-lifetime"
    if [[ "$termdeck_gate_root" != "$expected_gate_root" || "$termdeck_gate_root" != /* || -L "$termdeck_gate_root" ]]; then
        printf 'codex: refusing an unsafe TermDeck lifetime gate root: %s\\n' "$termdeck_gate_root" >&2
        exit 75
    fi
    termdeck_session=true
fi
readonly termdeck_session"""

LEGACY_GATE_BLOCK = """\
pace_termdeck_startup() {
    [[ "$termdeck_interactive" == true ]] || return 0
    # Correctness is enforced below by the per-auth-scope lifetime lock.  A
    # former global, disk-backed pacing lock made every restored TermDeck tab
    # wait behind every other tab; a large restore could therefore take many
    # minutes or wedge completely during home-directory I/O pressure.  Keep
    # this hook as a no-op so existing callers retain their control flow while
    # independent terminals start without a machine-wide convoy.
    return 0
}

termdeck_lifetime_fd=""
acquire_termdeck_lifetime_lock() {
    [[ "$termdeck_interactive" == true ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0

    ensure_private_directory "$startup_gate_root" || return 75

    local scope_key="${TERMDECK_AUTH_SCOPE_ID//[^A-Za-z0-9_.-]/_}"
    exec {termdeck_lifetime_fd}>"${startup_gate_root}/${scope_key}.lock"
    if ! flock -n "$termdeck_lifetime_fd"; then
        printf 'codex: waiting up to 10 seconds for the previous Codex process in this TermDeck terminal to exit...\\n' >&2
        if ! flock -w 10 "$termdeck_lifetime_fd"; then
            printf 'codex: the previous Codex process is still running; refusing an overlapping launch\\n' >&2
            exec {termdeck_lifetime_fd}>&-
            termdeck_lifetime_fd=""
            return 75
        fi
    fi
}"""

INTEGRATED_GATE_BLOCK = """\
gate_helper="${CODEX_TERMDECK_GATE_HELPER:-${user_home}/.local/libexec/codex-contained-gates.sh}"
readonly gate_helper
if [[ "$termdeck_session" == true ]]; then
    if [[ ! -r "$gate_helper" ]]; then
        printf 'codex: TermDeck launch gate helper is unavailable: %s\\n' "$gate_helper" >&2
        exit 75
    fi
    # shellcheck source=/dev/null
    source "$gate_helper"
fi"""

LEGACY_LIFETIME_CALL_BLOCK = """\
set +e
acquire_termdeck_lifetime_lock
lock_status="$?"
set -e
if (( lock_status != 0 )); then
    exit "$lock_status"
fi"""

INTEGRATED_LIFETIME_CALL_BLOCK = """\
lock_status=0
if [[ "$termdeck_session" == true ]]; then
    set +e
    codex_contained_acquire_termdeck_lifetime_lock \\
        "$uid" "${TERMDECK_AUTH_SCOPE_ID:-}" "$user_home"
    lock_status="$?"
    set -e
fi
if (( lock_status != 0 )); then
    exit "$lock_status"
fi"""

LEGACY_PACE_CALL_BLOCK = """\
    set +e
    pace_termdeck_startup
    gate_status="$?"
    set -e
    if (( gate_status != 0 )); then
        exit "$gate_status"
    fi"""

INTEGRATED_PACE_CALL_BLOCK = """\
    gate_status=0
    if [[ "$termdeck_interactive" == true ]]; then
        set +e
        codex_contained_pace_termdeck_startup \\
            true "$uid" "${TERMDECK_AUTH_SCOPE_ID:-}"
        gate_status="$?"
        set -e
    fi
    if (( gate_status != 0 )); then
        exit "$gate_status"
    fi"""

LEGACY_REPLACEMENTS = (
    ("cache-root", LEGACY_ROOT_BLOCK, INTEGRATED_ROOT_BLOCK),
    ("session-detection", LEGACY_SESSION_BLOCK, INTEGRATED_SESSION_BLOCK),
    ("gate-functions", LEGACY_GATE_BLOCK, INTEGRATED_GATE_BLOCK),
    ("lifetime-call", LEGACY_LIFETIME_CALL_BLOCK, INTEGRATED_LIFETIME_CALL_BLOCK),
    ("pace-call", LEGACY_PACE_CALL_BLOCK, INTEGRATED_PACE_CALL_BLOCK),
)


def _integrate_tty_guard(text: str) -> str:
    """Install exact tty save/restore only in the known proxy launcher layout."""

    helper_count = text.count(TTY_GUARD_BLOCK)
    legacy_trap_count = text.count(LEGACY_CLEANUP_TRAP)
    integrated_trap_count = text.count(INTEGRATED_CLEANUP_TRAP)
    legacy_status_count = text.count(LEGACY_CHILD_STATUS_BLOCK)
    integrated_status_count = text.count(INTEGRATED_CHILD_STATUS_BLOCK)

    # Minimal test/legacy launchers without the proxy+bwrap lifecycle have no
    # child tty ownership to integrate here. Preserve them byte-for-byte.
    if not any(
        (helper_count, legacy_trap_count, integrated_trap_count, legacy_status_count, integrated_status_count)
    ):
        return text

    if (
        helper_count == 1
        and legacy_trap_count == 0
        and integrated_trap_count == 1
        and legacy_status_count == 0
        and integrated_status_count == 1
    ):
        return text

    if (
        helper_count == 0
        and legacy_trap_count == 1
        and integrated_trap_count == 0
        and legacy_status_count == 1
        and integrated_status_count == 0
    ):
        header = "umask 077\n"
        if text.count(header) != 1:
            raise IntegrationError("launcher tty guard has no unique umask insertion point")
        desired = text.replace(header, header + "\n" + TTY_GUARD_BLOCK + "\n", 1)
        desired = desired.replace(LEGACY_CLEANUP_TRAP, INTEGRATED_CLEANUP_TRAP, 1)
        desired = desired.replace(LEGACY_CHILD_STATUS_BLOCK, INTEGRATED_CHILD_STATUS_BLOCK, 1)
        return desired

    raise IntegrationError(
        "launcher tty guard layout is unknown "
        f"(helper={helper_count}, legacy_trap={legacy_trap_count}, "
        f"integrated_trap={integrated_trap_count}, legacy_status={legacy_status_count}, "
        f"integrated_status={integrated_status_count})"
    )


class IntegrationError(RuntimeError):
    """A fail-closed integration refusal."""


def integrate_launcher(text: str) -> tuple[str, str]:
    """Return (state, desired text), accepting only known legacy/integrated gates."""

    if not text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n"):
        raise IntegrationError("launcher header is not the known strict Bash layout")

    counts = [(name, text.count(old)) for name, old, _new in LEGACY_REPLACEMENTS]
    if all(count == 1 for _name, count in counts):
        positions = [text.index(old) for _name, old, _new in LEGACY_REPLACEMENTS]
        if positions != sorted(positions):
            raise IntegrationError("legacy gate blocks are not in the known order")
        desired = text
        for _name, old, new in LEGACY_REPLACEMENTS:
            desired = desired.replace(old, new, 1)
        return "legacy", _integrate_tty_guard(desired)

    integrated_markers = (
        INTEGRATED_SESSION_BLOCK,
        INTEGRATED_GATE_BLOCK,
        INTEGRATED_LIFETIME_CALL_BLOCK,
        INTEGRATED_PACE_CALL_BLOCK,
    )
    if (
        all(count == 0 for _name, count in counts)
        and all(text.count(marker) == 1 for marker in integrated_markers)
        and "startup_gate_root=" not in text
        and "acquire_termdeck_lifetime_lock()" not in text
        and "pace_termdeck_startup()" not in text
    ):
        positions = [text.index(marker) for marker in integrated_markers]
        if positions == sorted(positions):
            return "integrated", _integrate_tty_guard(text)

    details = ", ".join(f"{name}={count}" for name, count in counts)
    raise IntegrationError(f"launcher gate layout is unknown ({details})")


def _read_regular(
    path: Path, *, label: str, maximum_bytes: int
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise IntegrationError(f"cannot open {label} {path}: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise IntegrationError(f"{label} must be one regular, single-link file: {path}")
        if metadata.st_size < 0 or metadata.st_size > maximum_bytes:
            raise IntegrationError(
                f"{label} exceeds the {maximum_bytes}-byte integration bound: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise IntegrationError(
                    f"{label} grew past the {maximum_bytes}-byte integration bound: {path}"
                )
            chunks.append(chunk)
        return b"".join(chunks), metadata
    finally:
        os.close(fd)


def _decode_shell(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationError(f"{label} is not UTF-8 text") from exc


def _validator_output(result: subprocess.CompletedProcess[bytes]) -> str:
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    return output[-4000:]


def validate_shell(data: bytes, *, label: str) -> bool:
    """Validate Bash from stdin; return whether optional shellcheck ran."""

    bash = shutil.which("bash")
    if bash is None:
        raise IntegrationError("bash is required to validate staged launcher files")
    try:
        syntax = subprocess.run(
            [bash, "-n"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(f"bash -n could not validate {label}: {exc}") from exc
    if syntax.returncode != 0:
        raise IntegrationError(f"bash -n rejected {label}: {_validator_output(syntax)}")

    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        return False
    try:
        static = subprocess.run(
            [shellcheck, "-s", "bash", "-"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError(f"shellcheck could not validate {label}: {exc}") from exc
    if static.returncode != 0:
        raise IntegrationError(f"shellcheck rejected {label}: {_validator_output(static)}")
    return True


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _current_fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return _fingerprint(metadata)


def _stage_file(path: Path, data: bytes, *, uid: int, gid: int, mode: int) -> Path:
    try:
        fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.termdeck-", dir=path.parent)
    except OSError as exc:
        raise IntegrationError(f"cannot stage {path}: {exc}") from exc
    staged = Path(raw_path)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("staged write made no progress")
            offset += written
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (uid, gid):
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    except OSError as exc:
        try:
            staged.unlink()
        except OSError:
            pass
        raise IntegrationError(f"cannot prepare staged file for {path}: {exc}") from exc
    finally:
        os.close(fd)
    return staged


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _helper_destination_state(path: Path, *, uid: int, gid: int) -> tuple[int, ...] | None:
    fingerprint = _current_fingerprint(path)
    if fingerprint is None:
        return None
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (metadata.st_uid, metadata.st_gid) != (uid, gid)
    ):
        raise IntegrationError(
            f"existing helper destination is unsafe or has different ownership: {path}"
        )
    return fingerprint


def install(launcher: Path, *, dry_run: bool) -> None:
    launcher = launcher.expanduser().absolute()
    helper_destination = launcher.with_name("codex-contained-gates.sh")

    launcher_data, launcher_metadata = _read_regular(
        launcher,
        label="launcher",
        maximum_bytes=MAX_LAUNCHER_BYTES,
    )
    launcher_text = _decode_shell(launcher_data, label="launcher")
    state, desired_text = integrate_launcher(launcher_text)
    desired_launcher = desired_text.encode("utf-8")

    helper_data, _helper_metadata = _read_regular(
        SOURCE_HELPER,
        label="source helper",
        maximum_bytes=MAX_HELPER_BYTES,
    )
    launcher_shellcheck = validate_shell(desired_launcher, label="integrated launcher")
    helper_shellcheck = validate_shell(helper_data, label="gate helper")

    launcher_fingerprint = _fingerprint(launcher_metadata)
    helper_fingerprint = _helper_destination_state(
        helper_destination,
        uid=launcher_metadata.st_uid,
        gid=launcher_metadata.st_gid,
    )
    launcher_mode = stat.S_IMODE(launcher_metadata.st_mode)
    validation = "bash -n and shellcheck" if launcher_shellcheck and helper_shellcheck else "bash -n"
    if dry_run:
        print(f"dry-run: accepted {state} launcher gate layout: {launcher}")
        print(f"dry-run: validated launcher and helper with {validation}")
        print(
            "dry-run: launcher metadata will remain "
            f"uid={launcher_metadata.st_uid} gid={launcher_metadata.st_gid} mode={launcher_mode:04o}"
        )
        print(f"dry-run: helper destination will be {helper_destination} with mode=0700")
        return

    helper_stage: Path | None = None
    launcher_stage: Path | None = None
    try:
        helper_stage = _stage_file(
            helper_destination,
            helper_data,
            uid=launcher_metadata.st_uid,
            gid=launcher_metadata.st_gid,
            mode=0o700,
        )
        if desired_launcher != launcher_data:
            # Gate integration is versioned by desired bytes, not by the
            # coarse legacy/integrated classifier. An already-integrated older
            # launcher can still require a later additive migration (for
            # example exact tty save/restore). Stage whenever validation
            # produced different bytes; an idempotent current launcher stages
            # nothing.
            launcher_stage = _stage_file(
                launcher,
                desired_launcher,
                uid=launcher_metadata.st_uid,
                gid=launcher_metadata.st_gid,
                mode=launcher_mode,
            )

        if _current_fingerprint(launcher) != launcher_fingerprint:
            raise IntegrationError("launcher changed after validation; refusing installation")
        if _current_fingerprint(helper_destination) != helper_fingerprint:
            raise IntegrationError("helper destination changed after validation; refusing installation")

        os.replace(helper_stage, helper_destination)
        helper_stage = None
        _fsync_directory(helper_destination.parent)
        if launcher_stage is not None:
            os.replace(launcher_stage, launcher)
            launcher_stage = None
            _fsync_directory(launcher.parent)
    except OSError as exc:
        raise IntegrationError(f"atomic installation failed: {exc}") from exc
    finally:
        for staged in (helper_stage, launcher_stage):
            if staged is not None:
                try:
                    staged.unlink()
                except OSError:
                    pass

    installed_launcher, installed_metadata = _read_regular(
        launcher,
        label="installed launcher",
        maximum_bytes=MAX_LAUNCHER_BYTES,
    )
    installed_helper, helper_metadata = _read_regular(
        helper_destination,
        label="installed helper",
        maximum_bytes=MAX_HELPER_BYTES,
    )
    if installed_launcher != desired_launcher or (
        installed_metadata.st_uid,
        installed_metadata.st_gid,
        stat.S_IMODE(installed_metadata.st_mode),
    ) != (launcher_metadata.st_uid, launcher_metadata.st_gid, launcher_mode):
        raise IntegrationError("installed launcher failed byte or metadata readback")
    if installed_helper != helper_data or (
        helper_metadata.st_uid,
        helper_metadata.st_gid,
        stat.S_IMODE(helper_metadata.st_mode),
    ) != (launcher_metadata.st_uid, launcher_metadata.st_gid, 0o700):
        raise IntegrationError("installed helper failed byte or metadata readback")

    print(f"installed helper: {helper_destination} sha256={hashlib.sha256(helper_data).hexdigest()}")
    if desired_launcher != launcher_data:
        print(f"updated launcher atomically: {launcher} sha256={hashlib.sha256(desired_launcher).hexdigest()}")
    else:
        print(f"launcher already current: {launcher}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install TermDeck's gate helper into one exact known contained launcher layout."
    )
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report without creating or replacing any file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        install(args.launcher, dry_run=args.dry_run)
    except IntegrationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
