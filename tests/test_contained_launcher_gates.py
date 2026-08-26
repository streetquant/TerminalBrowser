import os
import runpy
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


GATE_HELPER = Path(__file__).parents[1] / "terminal-auth" / "codex-contained-gates.sh"
GATE_INSTALLER = Path(__file__).parents[1] / "terminal-auth" / "install_contained_gates.py"
INSTALLER_CONSTANTS = runpy.run_path(str(GATE_INSTALLER), run_name="termdeck_gate_installer_test")


def _known_legacy_launcher() -> str:
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
uid="$(id -u)"
readonly uid
user_home="/tmp/termdeck-test-user"
readonly user_home
{INSTALLER_CONSTANTS["LEGACY_ROOT_BLOCK"]}
: "$cache_root"

{INSTALLER_CONSTANTS["LEGACY_SESSION_BLOCK"]}
termdeck_interactive=true
readonly termdeck_interactive

{INSTALLER_CONSTANTS["LEGACY_GATE_BLOCK"]}

max_attempts=1
readonly max_attempts

{INSTALLER_CONSTANTS["LEGACY_LIFETIME_CALL_BLOCK"]}

attempt=1
while (( attempt <= max_attempts )); do
{INSTALLER_CONSTANTS["LEGACY_PACE_CALL_BLOCK"]}
    break
done
"""


def _old_integrated_launcher_without_tty_guard() -> str:
    """Model the gate-integrated launcher generation deployed before tty restore."""

    launcher = _known_legacy_launcher().replace(
        "umask 077\n",
        "umask 077\n\ncleanup_proxy() { :; }\ntrap cleanup_proxy EXIT\n",
        1,
    )
    launcher = launcher.replace(
        "    break\n",
        "    started_at=\"$SECONDS\"\n"
        "    set +e\n"
        "    /usr/bin/bwrap -- /bin/true\n"
        "    status=\"$?\"\n"
        "    set -e\n"
        "    elapsed=\"$((SECONDS - started_at))\"\n"
        "    break\n",
        1,
    )
    for _name, old, new in INSTALLER_CONSTANTS["LEGACY_REPLACEMENTS"]:
        assert launcher.count(old) == 1
        launcher = launcher.replace(old, new, 1)
    # The real launcher consumes retry status/timing after the loop. Keep the
    # compact fixture shellcheck-clean while preserving the old integrated
    # tty lifecycle being exercised.
    launcher = launcher.replace(
        "done\n",
        'done\n: "${status:-}" "${elapsed:-}"\n',
        1,
    )
    assert launcher.count(INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"]) == 0
    assert launcher.count(INSTALLER_CONSTANTS["LEGACY_CLEANUP_TRAP"]) == 1
    return launcher


def _run_installer(launcher: Path, *, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-B", str(GATE_INSTALLER), "--launcher", str(launcher)]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def _gate_env(root: Path, **overrides: str) -> dict[str, str]:
    user_home = root.parent / "home"
    user_home.mkdir(mode=0o700, exist_ok=True)
    user_home.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "CODEX_TERMDECK_GATE_ROOT": str(root),
            "CODEX_TERMDECK_STARTUP_SHARDS": "1",
            "CODEX_TERMDECK_STARTUP_SPACING_MS": "300",
            "CODEX_TERMDECK_STARTUP_WAIT_SECONDS": "0.05",
            "CODEX_TERMDECK_LIFETIME_WAIT_SECONDS": "0.05",
            "XDG_CACHE_HOME": str(root.parent / "cache"),
            "TEST_USER_HOME": str(user_home),
        }
    )
    env.update(overrides)
    return env


def _run_pace(root: Path, scope: str = "scope-one", **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; codex_contained_pace_termdeck_startup true "$(id -u)" "$2"',
            "gate-test",
            str(GATE_HELPER),
            scope,
        ],
        text=True,
        capture_output=True,
        timeout=5,
        env=_gate_env(root, **overrides),
        check=False,
    )


def _run_lifetime(
    root: Path, scope: str = "scope-one", **overrides: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; codex_contained_acquire_termdeck_lifetime_lock "$2" "$3" "$4"',
            "gate-test",
            str(GATE_HELPER),
            str(os.geteuid()),
            scope,
            str(root.parent / "home"),
        ],
        text=True,
        capture_output=True,
        timeout=2,
        env=_gate_env(root, **overrides),
        check=False,
    )


def _lifetime_call(scope: str) -> str:
    return (
        'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" '
        f'{scope} "$TEST_USER_HOME"'
    )


def _readline_with_timeout(process: subprocess.Popen[str], timeout: float = 2) -> str:
    assert process.stdout is not None
    readable, _writable, _exceptional = select.select([process.stdout], [], [], timeout)
    if not readable:
        process.terminate()
        process.wait(timeout=5)
        raise AssertionError("lock holder did not become ready")
    return process.stdout.readline().strip()


def _tty_state(fd: int) -> str:
    return subprocess.run(
        ["stty", "-g"],
        stdin=fd,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout.strip()


def _run_tty_guard_child(exit_code: int, *, explicit_restore: bool) -> tuple[int, str, str] | None:
    try:
        master_fd, slave_fd = os.openpty()
    except OSError:
        return None
    try:
        before = _tty_state(slave_fd)
        child_code = (
            "import sys, tty; "
            "tty.setraw(0); "
            f"sys.exit({int(exit_code)})"
        )
        restore = "restore_termdeck_tty" if explicit_restore else ":"
        script = "\n".join(
            (
                "set -Eeuo pipefail",
                INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"],
                "cleanup_proxy() { :; }",
                INSTALLER_CONSTANTS["INTEGRATED_CLEANUP_TRAP"],
                "set +e",
                f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}",
                'status="$?"',
                "set -e",
                restore,
                'exit "$status"',
            )
        )
        result = subprocess.run(
            ["bash", "-c", script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            timeout=10,
            check=False,
        )
        after = _tty_state(slave_fd)
        return result.returncode, before, after
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_tty_guard_restores_exact_state_after_normal_child_exit() -> None:
    observation = _run_tty_guard_child(0, explicit_restore=True)
    if observation is None:
        return
    status, before, after = observation
    assert status == 0
    assert after == before


def test_tty_guard_restores_exact_state_after_nonzero_child_exit() -> None:
    observation = _run_tty_guard_child(7, explicit_restore=True)
    if observation is None:
        return
    status, before, after = observation
    assert status == 7
    assert after == before


def test_tty_guard_exit_trap_restores_exact_state_without_explicit_restore() -> None:
    observation = _run_tty_guard_child(9, explicit_restore=False)
    if observation is None:
        return
    status, before, after = observation
    assert status == 9
    assert after == before


def test_launcher_integrator_adds_tty_guard_to_known_proxy_lifecycle() -> None:
    launcher = _known_legacy_launcher().replace(
        "umask 077\n",
        "umask 077\n\ncleanup_proxy() { :; }\ntrap cleanup_proxy EXIT\n",
        1,
    )
    launcher = launcher.replace(
        "    break\n",
        "    started_at=\"$SECONDS\"\n"
        "    set +e\n"
        "    /usr/bin/bwrap -- /bin/true\n"
        "    status=\"$?\"\n"
        "    set -e\n"
        "    elapsed=\"$((SECONDS - started_at))\"\n"
        "    break\n",
        1,
    )

    state, desired = INSTALLER_CONSTANTS["integrate_launcher"](launcher)

    assert state == "legacy"
    assert desired.count(INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"]) == 1
    assert desired.count(INSTALLER_CONSTANTS["INTEGRATED_CLEANUP_TRAP"]) == 1
    assert desired.count(INSTALLER_CONSTANTS["INTEGRATED_CHILD_STATUS_BLOCK"]) == 1
    assert INSTALLER_CONSTANTS["LEGACY_CLEANUP_TRAP"] not in desired


def test_launcher_integrator_rejects_partial_tty_guard_lifecycle() -> None:
    launcher = _known_legacy_launcher().replace(
        "umask 077\n",
        "umask 077\n\ncleanup_proxy() { :; }\ntrap cleanup_proxy EXIT\n",
        1,
    )
    launcher = launcher.replace(
        "    break\n",
        "    started_at=\"$SECONDS\"\n"
        "    set +e\n"
        "    /usr/bin/bwrap -- /bin/true\n"
        "    status=\"$?\"\n"
        "    set -e\n"
        "    elapsed=\"$((SECONDS - started_at))\"\n"
        "    break\n",
        1,
    )
    # Create a mixed state: helper present, old trap/status still present.
    launcher = launcher.replace(
        "umask 077\n",
        "umask 077\n\n" + INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"] + "\n",
        1,
    )

    try:
        INSTALLER_CONSTANTS["integrate_launcher"](launcher)
    except INSTALLER_CONSTANTS["IntegrationError"] as exc:
        assert "tty guard layout is unknown" in str(exc)
    else:
        raise AssertionError("partial tty guard lifecycle was accepted")


def test_startup_pacing_uses_runtime_directory_instead_of_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "runtime"
        cache = Path(tmp) / "cache"
        runtime.mkdir(mode=0o700)
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; codex_contained_gate_root "$(id -u)"',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=5,
            env={
                **os.environ,
                "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CACHE_HOME": str(cache),
            },
            check=True,
        )

        assert result.stdout.strip() == str(runtime / "codex-contained-startup")
        assert str(cache) not in result.stdout


def test_gate_helper_runs_under_launcher_strict_shell_options() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        result = subprocess.run(
            [
                "bash",
                "-Eeuo",
                "pipefail",
                "-c",
                'source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" strict-scope "$TEST_USER_HOME"; '
                'codex_contained_pace_termdeck_startup true "$(id -u)" strict-scope',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=5,
            env=_gate_env(root),
            check=False,
        )

        assert result.returncode == 0, result.stderr


def test_gate_helper_does_not_shadow_launcher_readonly_identity_variables() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        result = subprocess.run(
            [
                "bash",
                "-Eeuo",
                "pipefail",
                "-c",
                'uid="$(id -u)"; readonly uid; '
                'user_home="$TEST_USER_HOME"; readonly user_home; '
                'source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$uid" readonly-scope "$user_home"; '
                'codex_contained_pace_termdeck_startup true "$uid" readonly-scope',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=5,
            env=_gate_env(root),
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "readonly variable" not in result.stderr


def test_mass_launches_do_not_form_a_serial_startup_convoy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        env = _gate_env(root)
        command = [
            "bash",
            "-c",
            'source "$1"; codex_contained_pace_termdeck_startup true "$(id -u)" "$2"',
            "gate-test",
            str(GATE_HELPER),
        ]
        started = time.monotonic()
        processes = [
            subprocess.Popen(
                [*command, f"mass-{index}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            # Match the real recovery set: forty-eight independently scoped
            # Codex conversations may be relaunched in one TermDeck restore.
            for index in range(48)
        ]
        results = [process.communicate(timeout=5) + (process.returncode,) for process in processes]
        elapsed = time.monotonic() - started

        assert all(returncode == 0 for _stdout, _stderr, returncode in results)
        # The removed gate required roughly 2 seconds per launch.  Even on a
        # loaded host, the sharded advisory gate must remain a bounded burst.
        assert elapsed < 5
        assert any("continuing without the optional delay" in stderr for _stdout, stderr, _returncode in results)


def test_startup_pace_uses_bounded_flock_wait_and_fails_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        root = directory / "gates"
        fake_bin = directory / "bin"
        log = directory / "flock-argv"
        root.mkdir(mode=0o700)
        fake_bin.mkdir(mode=0o700)
        fake_flock = fake_bin / "flock"
        fake_flock.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$FLOCK_LOG\"\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_flock.chmod(0o755)

        result = _run_pace(
            root,
            PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            FLOCK_LOG=str(log),
        )

        assert result.returncode == 0
        assert "continuing without the optional delay" in result.stderr
        arguments = shlex.split(log.read_text(encoding="utf-8").strip())
        assert arguments[:2] == ["-w", "0.05"]
        assert len(arguments) == 3
        assert arguments[2].isdigit()


def test_future_startup_timestamp_is_ignored_and_replaced() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        root.mkdir(mode=0o700)
        future = int(time.time() * 1000) + 86_400_000
        marker = root / "startup.0.last"
        marker.write_text(f"{future}\n", encoding="ascii")

        started = time.monotonic()
        result = _run_pace(root, CODEX_TERMDECK_STARTUP_SPACING_MS="5000")
        elapsed = time.monotonic() - started

        assert result.returncode == 0
        assert elapsed < 1
        replacement = int(marker.read_text(encoding="ascii").strip())
        assert replacement < future
        assert abs(replacement - int(time.time() * 1000)) < 5_000


def test_noninteractive_private_launch_keeps_lifetime_lock_without_pacing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        scope = "private-subcommand"
        env = _gate_env(root)
        holder = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                'set -e; source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$2" "$3" "$TEST_USER_HOME"; '
                'codex_contained_pace_termdeck_startup false "$2" "$3"; '
                'printf "ready\\n"; sleep 5',
                "gate-test",
                str(GATE_HELPER),
                str(os.geteuid()),
                scope,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            assert _readline_with_timeout(holder) == "ready"
            assert (root / "scope.private-subcommand.lock").is_file()
            assert not list(root.glob("startup.*"))
            contender = _run_lifetime(root, scope)
        finally:
            holder.terminate()
            holder.wait(timeout=5)

        assert contender.returncode == 75
        assert "refusing an overlapping launch" in contender.stderr


def test_private_lifetime_lock_fails_closed_without_flock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        temporary = Path(tmp)
        empty_path = temporary / "empty-path"
        empty_path.mkdir()

        result = _run_lifetime(
            temporary / "gates",
            "private-subcommand",
            PATH=str(empty_path),
        )

        assert result.returncode == 75
        assert "flock is required" in result.stderr


def test_lifetime_lock_file_is_backend_safe_even_with_permissive_umask() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'umask 000; source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$2" safe-mode "$TEST_USER_HOME"; '
                'stat -Lc "%a" -- "$CODEX_TERMDECK_GATE_ROOT/scope.safe-mode.lock"',
                "gate-test",
                str(GATE_HELPER),
                str(os.geteuid()),
            ],
            text=True,
            capture_output=True,
            timeout=2,
            env=_gate_env(root),
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "600"


def test_lifetime_lock_fails_closed_for_backend_unsafe_permissions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        root.mkdir(mode=0o700)
        lock_path = root / "scope.unsafe.lock"
        lock_path.write_text("", encoding="ascii")
        lock_path.chmod(0o620)

        result = _run_lifetime(root, "unsafe")

        assert result.returncode == 75
        assert "lifetime lock for TermDeck auth scope unsafe is unsafe" in result.stderr


def test_lifetime_lock_revalidates_expected_path_on_idempotent_acquire() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$2" renamed "$TEST_USER_HOME"; '
                'mv -- "$CODEX_TERMDECK_GATE_ROOT/scope.renamed.lock" '
                '"$CODEX_TERMDECK_GATE_ROOT/displaced.lock"; '
                'ln -s -- displaced.lock '
                '"$CODEX_TERMDECK_GATE_ROOT/scope.renamed.lock"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$2" renamed "$TEST_USER_HOME"',
                "gate-test",
                str(GATE_HELPER),
                str(os.geteuid()),
            ],
            text=True,
            capture_output=True,
            timeout=2,
            env=_gate_env(root),
            check=False,
        )

        assert result.returncode == 75
        assert "existing lifetime lock for TermDeck auth scope renamed is unsafe" in result.stderr


def test_lifetime_lock_revalidates_path_after_flock_acquisition() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        temporary = Path(tmp)
        root = temporary / "gates"
        fake_bin = temporary / "fake-bin"
        fake_bin.mkdir()
        fake_flock = fake_bin / "flock"
        fake_flock.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            "if [[ ${1:-} == -n ]]; then shift; fi\n"
            "if [[ ${1:-} == -w ]]; then shift 2; fi\n"
            'fd="$1"\n'
            'path="$(readlink -- "/proc/$$/fd/${fd}")"\n'
            'mv -- "$path" "${path}.displaced"\n',
            encoding="utf-8",
        )
        fake_flock.chmod(0o700)

        result = _run_lifetime(
            root,
            "raced",
            PATH=f"{fake_bin}:/usr/bin:/bin",
        )

        assert result.returncode == 75
        assert "acquired legacy lifetime lock for TermDeck auth scope raced became unsafe" in result.stderr


def test_lifetime_lock_remains_fail_closed_for_the_same_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        env = _gate_env(root)
        holder = subprocess.Popen(
            [
                "bash",
                "-c",
                'set -e; source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" shared "$TEST_USER_HOME"; '
                'printf "ready\\n"; sleep 5',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            assert _readline_with_timeout(holder) == "ready"
            contender = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" shared "$TEST_USER_HOME"',
                    "gate-test",
                    str(GATE_HELPER),
                ],
                text=True,
                capture_output=True,
                timeout=2,
                env=env,
                check=False,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=5)

        assert contender.returncode == 75
        assert "refusing an overlapping launch" in contender.stderr


def test_lifetime_lock_is_sharded_by_auth_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        env = _gate_env(root)
        holder = subprocess.Popen(
            [
                "bash",
                "-c",
                'set -e; source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" alpha "$TEST_USER_HOME"; '
                'printf "ready\\n"; sleep 5',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            assert _readline_with_timeout(holder) == "ready"
            other_scope = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" beta "$TEST_USER_HOME"',
                    "gate-test",
                    str(GATE_HELPER),
                ],
                text=True,
                capture_output=True,
                timeout=2,
                env=env,
                check=False,
            )
        finally:
            holder.terminate()
            holder.wait(timeout=5)

        assert other_scope.returncode == 0


def test_lifetime_lock_is_idempotent_within_one_launcher_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -e; source "$1"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" same "$TEST_USER_HOME"; '
                'codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" same "$TEST_USER_HOME"',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=2,
            env=_gate_env(root, CODEX_TERMDECK_LIFETIME_WAIT_SECONDS="1"),
            check=False,
        )

        assert result.returncode == 0, result.stderr


def test_lifetime_lock_fails_closed_when_lock_path_cannot_be_opened() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        root.mkdir(mode=0o700)
        (root / "scope.blocked.lock").mkdir()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" blocked "$TEST_USER_HOME"',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=2,
            env=_gate_env(root),
            check=False,
        )

        assert result.returncode == 75
        assert "cannot open the lifetime lock" in result.stderr


def test_startup_pace_fails_open_when_lock_path_cannot_be_opened() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        root.mkdir(mode=0o700)
        (root / "startup.0.lock").mkdir()

        result = _run_pace(root)

        assert result.returncode == 0
        assert "continuing without the optional delay" in result.stderr


def test_fifo_lock_paths_cannot_defeat_gate_time_bounds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gates"
        root.mkdir(mode=0o700)
        os.mkfifo(root / "startup.0.lock", mode=0o600)
        os.mkfifo(root / "scope.fifo.lock", mode=0o600)

        started = time.monotonic()
        pace = _run_pace(root)
        lifetime = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; codex_contained_acquire_termdeck_lifetime_lock "$(id -u)" fifo "$TEST_USER_HOME"',
                "gate-test",
                str(GATE_HELPER),
            ],
            text=True,
            capture_output=True,
            timeout=2,
            env=_gate_env(root),
            check=False,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1
        assert pace.returncode == 0
        assert "continuing without the optional delay" in pace.stderr
        assert lifetime.returncode == 75
        assert "cannot open the lifetime lock" in lifetime.stderr


def test_installer_dry_run_accepts_known_layout_without_writing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        launcher = directory / "codex-contained-user.sh"
        helper = directory / "codex-contained-gates.sh"
        original = _known_legacy_launcher()
        launcher.write_text(original, encoding="utf-8")
        launcher.chmod(0o751)
        before = launcher.stat()

        result = _run_installer(launcher, dry_run=True)

        after = launcher.stat()
        assert result.returncode == 0, result.stderr
        assert "accepted legacy launcher gate layout" in result.stdout
        assert "validated launcher and helper" in result.stdout
        assert launcher.read_text(encoding="utf-8") == original
        assert (after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_ino,
            before.st_uid,
            before.st_gid,
            0o751,
        )
        assert not helper.exists()
        assert not list(directory.glob(".*.termdeck-*"))


def test_installer_atomically_integrates_and_preserves_launcher_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        launcher = directory / "codex-contained-user.sh"
        helper = directory / "codex-contained-gates.sh"
        launcher.write_text(_known_legacy_launcher(), encoding="utf-8")
        launcher.chmod(0o751)
        before = launcher.stat()

        result = _run_installer(launcher)

        after = launcher.stat()
        helper_metadata = helper.stat()
        installed = launcher.read_text(encoding="utf-8")
        assert result.returncode == 0, result.stderr
        assert "updated launcher atomically" in result.stdout
        assert INSTALLER_CONSTANTS["INTEGRATED_GATE_BLOCK"] in installed
        assert INSTALLER_CONSTANTS["INTEGRATED_LIFETIME_CALL_BLOCK"] in installed
        assert INSTALLER_CONSTANTS["INTEGRATED_PACE_CALL_BLOCK"] in installed
        assert "startup_gate_root=" not in installed
        assert "acquire_termdeck_lifetime_lock()" not in installed
        assert helper.read_bytes() == GATE_HELPER.read_bytes()
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid,
            before.st_gid,
            0o751,
        )
        assert (
            helper_metadata.st_uid,
            helper_metadata.st_gid,
            stat.S_IMODE(helper_metadata.st_mode),
        ) == (before.st_uid, before.st_gid, 0o700)
        subprocess.run(["bash", "-n", str(launcher)], check=True, timeout=5)
        subprocess.run(["bash", "-n", str(helper)], check=True, timeout=5)

        integrated_inode = after.st_ino
        repeated = _run_installer(launcher)

        assert repeated.returncode == 0, repeated.stderr
        assert "launcher already current" in repeated.stdout
        assert launcher.stat().st_ino == integrated_inode


def test_installer_upgrades_old_integrated_launcher_with_tty_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        launcher = directory / "codex-contained-user.sh"
        helper = directory / "codex-contained-gates.sh"
        old_integrated = _old_integrated_launcher_without_tty_guard()
        launcher.write_text(old_integrated, encoding="utf-8")
        launcher.chmod(0o751)
        before = launcher.stat()

        state, desired = INSTALLER_CONSTANTS["integrate_launcher"](old_integrated)
        assert state == "integrated"
        assert desired != old_integrated
        assert desired.count(INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"]) == 1
        assert desired.count(INSTALLER_CONSTANTS["INTEGRATED_CLEANUP_TRAP"]) == 1
        assert desired.count(INSTALLER_CONSTANTS["INTEGRATED_CHILD_STATUS_BLOCK"]) == 1

        result = _run_installer(launcher)

        assert result.returncode == 0, result.stderr
        installed = launcher.read_text(encoding="utf-8")
        after = launcher.stat()
        assert installed == desired
        assert installed.count(INSTALLER_CONSTANTS["TTY_GUARD_BLOCK"]) == 1
        assert "updated launcher atomically" in result.stdout
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid,
            before.st_gid,
            0o751,
        )
        assert helper.is_file()
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700

        integrated_inode = after.st_ino
        repeated = _run_installer(launcher)
        assert repeated.returncode == 0, repeated.stderr
        assert "launcher already current" in repeated.stdout
        assert launcher.stat().st_ino == integrated_inode


def test_installer_rejects_unknown_gate_layout_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        launcher = directory / "codex-contained-user.sh"
        helper = directory / "codex-contained-gates.sh"
        known_root = INSTALLER_CONSTANTS["LEGACY_ROOT_BLOCK"]
        unknown_root = known_root.replace("XDG_CACHE_HOME", "TMPDIR", 1)
        original = _known_legacy_launcher().replace(known_root, unknown_root, 1)
        launcher.write_text(original, encoding="utf-8")
        launcher.chmod(0o751)
        before = launcher.stat()

        result = _run_installer(launcher)

        after = launcher.stat()
        assert result.returncode == 75
        assert "launcher gate layout is unknown" in result.stderr
        assert launcher.read_text(encoding="utf-8") == original
        assert (after.st_ino, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (
            before.st_ino,
            before.st_mtime_ns,
            0o751,
        )
        assert not helper.exists()
        assert not list(directory.glob(".*.termdeck-*"))


def test_installer_rejects_invalid_integrated_syntax_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        launcher = directory / "codex-contained-user.sh"
        helper = directory / "codex-contained-gates.sh"
        original = f"{_known_legacy_launcher()}if [[\n"
        launcher.write_text(original, encoding="utf-8")
        launcher.chmod(0o751)
        before = launcher.stat()

        result = _run_installer(launcher)

        after = launcher.stat()
        assert result.returncode == 75
        assert "bash -n rejected integrated launcher" in result.stderr
        assert launcher.read_text(encoding="utf-8") == original
        assert (after.st_ino, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (
            before.st_ino,
            before.st_mtime_ns,
            0o751,
        )
        assert not helper.exists()
        assert not list(directory.glob(".*.termdeck-*"))
