#!/usr/bin/env python3
"""Exercise application-independent recovery against one disposable tmux server."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile
import time


def _wait(predicate, *, timeout: float = 20.0, detail: str) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # pragma: no cover - retained in failure detail
            last_error = exc
        time.sleep(0.05)
    suffix = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(f"{detail}{suffix}")


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _invocation(path: Path, expected: int) -> dict[str, object]:
    holder: dict[str, object] = {}

    def ready() -> bool:
        nonlocal holder
        if not path.is_file():
            return False
        holder = _read_json(path)
        return int(holder.get("invocation", 0)) >= expected

    _wait(ready, detail=f"worker did not reach invocation {expected}")
    return holder


def main() -> int:
    from termdeck.models import SessionRecord
    from termdeck.session_store import SessionStore
    from termdeck.tmux_backend import TmuxBackend, TmuxError

    # tmux uses AF_UNIX sockets whose pathname is normally limited to roughly
    # 108 bytes. Validation sandboxes may expose a very long XDG/TMP path, so the
    # disposable authority must use the short in-namespace /tmp spelling.
    runtime_parent = Path("/tmp")
    if not runtime_parent.is_dir() or not os.access(
        runtime_parent, os.W_OK | os.X_OK
    ):
        raise RuntimeError("short writable /tmp namespace is unavailable")

    with tempfile.TemporaryDirectory(prefix="tgr-") as tmp, tempfile.TemporaryDirectory(
        prefix="tgrs-", dir=runtime_parent
    ) as socket_tmp:
        root = Path(tmp)
        socket_root = Path(socket_tmp)
        socket_root.chmod(0o700)
        os.environ["TMUX_TMPDIR"] = str(socket_root)
        os.environ.pop("TMUX", None)

        worker = root / "worker.py"
        worker.write_text(
            "from pathlib import Path\n"
            "import json, os, sys, time\n"
            "state = Path(sys.argv[1])\n"
            "mode = sys.argv[2]\n"
            "try:\n"
            "    previous = json.loads(state.read_text())\n"
            "    invocation = int(previous.get('invocation', 0)) + 1\n"
            "except Exception:\n"
            "    invocation = 1\n"
            "payload = {\n"
            "    'invocation': invocation,\n"
            "    'pid': os.getpid(),\n"
            "    'cwd': os.getcwd(),\n"
            "    'mode': os.environ.get('MODE'),\n"
            "    'path': os.environ.get('PATH'),\n"
            "    'degraded': os.environ.get('TERMDECK_RECOVERY_DEGRADED'),\n"
            "    'original_cwd': os.environ.get('TERMDECK_RECOVERY_ORIGINAL_CWD'),\n"
            "}\n"
            "temporary = state.with_suffix('.tmp')\n"
            "temporary.write_text(json.dumps(payload, sort_keys=True))\n"
            "os.replace(temporary, state)\n"
            "if mode == 'exit0':\n"
            "    time.sleep(0.5)\n"
            "    raise SystemExit(0)\n"
            "while True:\n"
            "    time.sleep(0.2)\n",
            encoding="utf-8",
        )

        empty_path = root / "empty-bin"
        empty_path.mkdir()
        work = root / "work"
        work.mkdir()
        database = root / "sessions.sqlite3"

        class NoCheckpoint:
            @staticmethod
            def restart_command(_record):
                return None

            @staticmethod
            def wrap_launch_command(_record, command: str) -> str:
                return command

            @staticmethod
            def session_dir(_record) -> Path:
                return root / "unused-checkpoint"

            @staticmethod
            def clear(_record) -> None:
                return None

        class GenericBackend(TmuxBackend):
            @staticmethod
            def _exact_codex_recovery(_record):
                raise AssertionError("generic recovery consulted an optional native adapter")

            @staticmethod
            def _exact_claude_recovery(_record):
                raise AssertionError("generic recovery consulted an optional native adapter")

        backend = GenericBackend(
            executable="/usr/bin/tmux",
            command_timeout=12.0,
            query_timeout=2.0,
            failure_backoff=0.05,
            checkpoint_manager=NoCheckpoint(),
        )
        store = SessionStore(database)

        def command(state: Path, mode: str) -> str:
            return shlex.join(
                [
                    sys.executable,
                    "-B",
                    str(worker),
                    str(state),
                    mode,
                ]
            )

        def pane(record: SessionRecord) -> dict[str, object]:
            line = backend._run(
                [
                    "display-message",
                    "-p",
                    "-t",
                    backend.pane_target(record),
                    "#{pid}\t#{session_created}\t#{pane_id}\t#{pane_pid}\t"
                    "#{pane_dead}\t#{pane_dead_status}",
                ],
                timeout=5.0,
            ).stdout.rstrip("\n")
            server_pid, created, pane_id, pid, dead, status = line.split("\t", 5)
            return {
                "server_pid": int(server_pid),
                "session_created": created,
                "pane_id": pane_id,
                "pid": int(pid),
                "dead": dead == "1",
                "status": int(status) if status.strip() else None,
            }

        try:
            loop_state = root / "loop.json"
            record = store.create(
                "generic-loop",
                "smoke",
                command(loop_state, "loop"),
                cwd=str(work),
                environment={"MODE": "generic"},
                restart_policy="on-failure",
            )
            backend.create_or_resume(record)
            first_state = _invocation(loop_state, 1)
            running = replace(record, status="running")
            first_pane = pane(running)

            backend.create_or_resume(running)
            attached_pane = pane(running)
            if attached_pane != first_pane or _read_json(loop_state)["invocation"] != 1:
                raise AssertionError("live payload was relaunched instead of reattached")

            os.kill(int(first_pane["pid"]), signal.SIGKILL)
            _wait(
                lambda: bool(pane(running)["dead"]),
                detail="killed payload did not become a dead tmux pane",
            )
            backend.create_or_resume(running)
            second_state = _invocation(loop_state, 2)
            second_pane = pane(running)
            if (
                second_pane["dead"]
                or second_pane["server_pid"] != first_pane["server_pid"]
                or second_pane["session_created"] != first_pane["session_created"]
                or second_pane["pane_id"] != first_pane["pane_id"]
                or second_pane["pid"] == first_pane["pid"]
            ):
                raise AssertionError("dead pane was not safely respawned in place")
            backend.create_or_resume(running)
            if _read_json(loop_state)["invocation"] != 2 or pane(running) != second_pane:
                raise AssertionError("repeated dead-pane recovery was not idempotent")

            backend._run(["kill-server"], check=False, timeout=8.0)
            recovery = replace(running, status="recovery-pending")
            _wait(
                lambda: not backend.session_exists(recovery),
                detail="disposable tmux server did not stop",
            )
            backend.create_or_resume(recovery)
            third_state = _invocation(loop_state, 3)
            third_pane = pane(recovery)
            if (
                third_pane["dead"]
                or third_pane["pid"] == second_pane["pid"]
                or third_pane["server_pid"] == second_pane["server_pid"]
            ):
                raise AssertionError("missing tmux server was not recreated from recipe")
            backend.create_or_resume(recovery)
            if _read_json(loop_state)["invocation"] != 3 or pane(recovery) != third_pane:
                raise AssertionError("repeated missing-server recovery was not idempotent")

            clean_state = root / "clean-exit.json"
            clean = store.create(
                "clean-exit",
                "smoke",
                command(clean_state, "exit0"),
                cwd=str(work),
                environment={"MODE": "clean-exit"},
                restart_policy="on-failure",
            )
            # Deliberately bypass the new supervisor to emulate a pane
            # launched by pre-upgrade Termdeck. New sessions already return to
            # a shell on exit 0 and therefore never enter this bridge.
            backend._run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    clean.tmux_name,
                    "-e",
                    "MODE=clean-exit",
                    "-c",
                    str(work),
                    clean.command,
                ]
            )
            backend._run(
                ["set-option", "-t", clean.tmux_name, "remain-on-exit", "on"]
            )
            _invocation(clean_state, 1)
            clean_running = replace(clean, status="running")
            _wait(
                lambda: bool(pane(clean_running)["dead"]),
                detail="clean-exit pane did not become dead",
            )
            clean_before = pane(clean_running)
            backend.create_or_resume(clean_running)
            if pane(clean_running) != clean_before or _read_json(clean_state)["invocation"] != 1:
                raise AssertionError("on-failure restarted a successful normal exit")

            bridge = backend.recover_dead_payload(clean_running)
            clean_after = pane(clean_running)
            if (
                bridge.action != "shell"
                or not bridge.clear_recovery
                or clean_after["dead"]
                or clean_after["pane_id"] != clean_before["pane_id"]
                or clean_after["pid"] == clean_before["pid"]
                or clean_after["pid"] != bridge.pid
                or _read_json(clean_state)["invocation"] != 1
            ):
                raise AssertionError(
                    "successful legacy payload did not bridge to a shell in place"
                )
            clean_shell = store.update(
                clean.id,
                command=bridge.command,
                cwd=bridge.cwd,
                recovery_command=None,
                recovery_cwd=None,
                restart_policy="on-failure",
                status="running",
                last_pid=bridge.pid,
            )
            backend._run(
                [
                    "send-keys",
                    "-t",
                    backend.pane_target(clean_shell),
                    "-l",
                    "printf LEGACY_SHELL_READY",
                ]
            )
            backend._run(
                ["send-keys", "-t", backend.pane_target(clean_shell), "C-m"]
            )
            _wait(
                lambda: "LEGACY_SHELL_READY" in backend.capture(clean_shell, lines=30),
                detail="legacy shell bridge did not accept terminal input",
            )

            always_state = root / "always.json"
            always = store.create(
                "always",
                "smoke",
                command(always_state, "exit0"),
                cwd=str(work),
                environment={"MODE": "always"},
                restart_policy="always",
            )
            # Same pre-supervisor fixture for an explicit always policy.
            backend._run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    always.tmux_name,
                    "-e",
                    "MODE=always",
                    "-c",
                    str(work),
                    always.command,
                ]
            )
            backend._run(
                ["set-option", "-t", always.tmux_name, "remain-on-exit", "on"]
            )
            _invocation(always_state, 1)
            always_running = replace(always, status="running")
            _wait(
                lambda: bool(pane(always_running)["dead"]),
                detail="always-policy pane did not become dead",
            )
            backend.create_or_resume(always_running)
            _invocation(always_state, 2)

            missing_state = root / "missing-cwd.json"
            missing_cwd = root / "gone" / "project"
            missing = store.create(
                "missing-cwd",
                "smoke",
                command(missing_state, "loop"),
                cwd=str(missing_cwd),
                environment={"MODE": "missing"},
                restart_policy="on-failure",
            )
            backend.create_or_resume(missing)
            missing_payload = _invocation(missing_state, 1)
            if missing_cwd.exists():
                raise AssertionError("recovery silently recreated a missing working directory")
            if (
                missing_payload["cwd"] != str(root)
                or missing_payload["degraded"] != "missing-working-directory"
                or missing_payload["original_cwd"] != str(missing_cwd)
            ):
                raise AssertionError(f"missing-cwd degradation was incorrect: {missing_payload}")

            corrupt = store.create(
                "corrupt",
                "smoke",
                command(root / "corrupt.json", "loop"),
                cwd=str(work),
            )
            corrupt = replace(
                corrupt,
                status="recovery-pending",
                recovery_recipe="{}",
                recovery_recipe_sha256="0" * 64,
            )
            try:
                backend.create_or_resume(corrupt)
            except TmuxError:
                pass
            else:
                raise AssertionError("corrupt recovery recipe was accepted")
            if backend.session_exists(corrupt):
                raise AssertionError("corrupt recovery recipe mutated tmux")

            observed_modes = [
                payload.get("mode")
                for payload in (first_state, second_state, third_state)
            ]
            if observed_modes != ["generic", "generic", "generic"]:
                raise AssertionError(
                    "generic environment overlay was not preserved across recovery; "
                    f"observed={observed_modes!r}"
                )

            print(
                json.dumps(
                    {
                        "ok": True,
                        "exact_reattach": True,
                        "dead_pane_respawned": True,
                        "tmux_server_recreated": True,
                        "idempotent": True,
                        "clean_exit_preserved": True,
                        "clean_exit_shell_bridge": True,
                        "clean_exit_shell_input": True,
                        "always_policy_restarted": True,
                        "missing_cwd_degraded": True,
                        "corrupt_recipe_refused": True,
                        "generic_environment_overlay": True,
                        "optional_native_adapters_consulted": False,
                        "invocations": int(third_state["invocation"]),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            backend._run(["kill-server"], check=False, timeout=8.0)
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
