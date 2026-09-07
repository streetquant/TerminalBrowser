#!/usr/bin/env python3
"""Build, install, verify, tamper-test, repair, and roll back a release.

Everything except the read-only semantic session snapshots is confined to a
fresh temporary home.  The smoke never replaces the real launcher, restarts the
GUI, or mutates a tmux/Codex session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Running a script by path puts only its scripts directory on sys.path; pin
# release-manager imports to this checkout so the wheel and installer share one source.
_REPOSITORY = Path(__file__).resolve().parents[1]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from termdeck.release_manager import (
    build_release,
    install_release,
    managed_release_status,
    rollback_release,
    semantic_runtime_snapshot,
    verify_installed_tree,
    verify_wheel,
)


def _invoke(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=180,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"command returned {completed.returncode}, expected {expected}: {argv!r}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    return completed


def _json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"command did not emit one JSON object: {completed.stdout!r}"
        ) from exc


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    semantic_before = semantic_runtime_snapshot()
    with tempfile.TemporaryDirectory(prefix="t1000-release-workflow-") as temporary:
        root = Path(temporary)
        output = root / "artifacts"
        managed = root / "managed"
        bin_dir = root / "bin"
        home = root / "home"
        outside = root / "outside"
        for directory in (bin_dir, home, outside):
            directory.mkdir(parents=True)

        legacy = bin_dir / "t1000"
        legacy.write_text("#!/bin/sh\nprintf '%s\\n' legacy-t1000\n", encoding="utf-8")
        legacy.chmod(0o755)

        built = build_release(repository, output, allow_dirty=True)
        wheel = Path(str(built["wheel"]))
        manifest = Path(str(built["manifest"]))
        verified = verify_wheel(wheel, manifest_path=manifest)
        installed = install_release(
            wheel,
            manifest,
            root=managed,
            bin_dir=bin_dir,
            python=sys.executable,
        )
        release_id = str(installed["release_id"])
        release_dir = managed / "releases" / release_id
        installed_tree = verify_installed_tree(release_dir)

        environment = os.environ.copy()
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "T1000_MANAGED_ROOT",
            "T1000_RELEASE_MANIFEST",
            "T1000_RELEASE_STATE",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        launcher = bin_dir / "t1000"
        release_info = _json_stdout(
            _invoke(
                [
                    str(launcher),
                    "release",
                    "info",
                    "--verify",
                    "--root",
                    str(managed),
                    "--bin-dir",
                    str(bin_dir),
                ],
                cwd=outside,
                env=environment,
            )
        )
        if not release_info.get("ok"):
            raise AssertionError(f"managed release info failed: {release_info!r}")
        runtime = release_info.get("runtime")
        selected = release_info.get("managed")
        if not isinstance(runtime, dict) or runtime.get("release_id") != release_id:
            raise AssertionError(f"managed runtime identity mismatch: {runtime!r}")
        if not isinstance(selected, dict) or not selected.get("verified"):
            raise AssertionError(f"managed installed-tree verification missing: {selected!r}")

        doctor = _json_stdout(
            _invoke([str(launcher), "doctor"], cwd=outside, env=environment)
        )
        if doctor.get("release", {}).get("release_id") != release_id:
            raise AssertionError(f"doctor lost release identity: {doctor!r}")

        # The bootstrap authenticates a descriptor derived from wheel RECORD and
        # verifies its exact site entries before importing termdeck. Prove that a
        # modified module is rejected, restore it, then prove normal launch resumes.
        cli_path = release_dir / "site" / "termdeck" / "cli.py"
        original_cli = cli_path.read_bytes()
        original_mode = cli_path.stat().st_mode & 0o777
        cli_path.chmod(0o644)
        cli_path.write_bytes(original_cli + b"\n# tamper sentinel\n")
        cli_path.chmod(original_mode)
        rejected = _invoke(
            [str(launcher), "--help"],
            cwd=outside,
            env=environment,
            expected=78,
        )
        if "installed site member" not in rejected.stderr:
            raise AssertionError(f"tampered managed site was not rejected: {rejected.stderr!r}")
        cli_path.chmod(0o644)
        cli_path.write_bytes(original_cli)
        cli_path.chmod(original_mode)
        _invoke([str(launcher), "--help"], cwd=outside, env=environment)

        # The stable launcher, bootstrap, and managed state form one activation
        # identity. Modifying either executable must fail before importing the
        # selected release, then exact restoration must resume normal launch.
        for label, target in (
            ("bootstrap", managed / "bootstrap.py"),
            ("launcher", launcher),
        ):
            original = target.read_bytes()
            mode = target.stat().st_mode & 0o777
            target.write_bytes(original + f"\n# {label} tamper sentinel\n".encode())
            rejected_activation = _invoke(
                [str(launcher), "--help"],
                cwd=outside,
                env=environment,
                expected=78,
            )
            if "managed launcher refused" not in rejected_activation.stderr:
                raise AssertionError(
                    f"tampered {label} was not rejected before import: "
                    f"{rejected_activation.stderr!r}"
                )
            target.write_bytes(original)
            target.chmod(mode)
            _invoke([str(launcher), "--help"], cwd=outside, env=environment)

        # A repeated install repairs a missing stable launcher without changing
        # the selected release or starting/stopping the GUI.
        launcher.unlink()
        repaired = install_release(
            wheel,
            manifest,
            root=managed,
            bin_dir=bin_dir,
            python=sys.executable,
        )
        if not repaired.get("idempotent") or not repaired.get("repaired"):
            raise AssertionError(f"idempotent install did not repair launcher: {repaired!r}")
        _invoke([str(launcher), "--help"], cwd=outside, env=environment)

        status = managed_release_status(
            root=managed,
            bin_dir=bin_dir,
            verify=True,
        )
        if not status.get("ok") or not status.get("verified"):
            raise AssertionError(f"managed status failed full verification: {status!r}")

        rolled_back = rollback_release(root=managed, bin_dir=bin_dir, to_legacy=True)
        legacy_output = _invoke([str(launcher)], cwd=outside, env=environment)
        if legacy_output.stdout.strip() != "legacy-t1000":
            raise AssertionError("legacy launcher was not restored exactly")

        semantic_after = semantic_runtime_snapshot()
        if semantic_after != semantic_before:
            raise AssertionError(
                "release workflow changed persistent tmux/Codex semantic state: "
                f"before={semantic_before!r}; after={semantic_after!r}"
            )

        print(
            json.dumps(
                {
                    "ok": True,
                    "release_id": release_id,
                    "build_mode": built["source"]["build_mode"],
                    "source_commit": built["source"]["commit"],
                    "source_tree_sha256": built["source"]["tree_sha256"],
                    "wheel_sha256": verified.sha256,
                    "wheel_members": len(verified.members),
                    "installed_members": installed_tree["installed_members"],
                    "outside_checkout_release_info": True,
                    "doctor_release_identity": True,
                    "tamper_rejected_before_import": True,
                    "activation_tamper_rejected_before_import": True,
                    "idempotent_launcher_repair": True,
                    "legacy_rollback": rolled_back["target"],
                    "semantic_runtime_sha256": semantic_after["sha256"],
                    "semantic_runtime_rows": semantic_after["row_count"],
                    "live_processes_restarted": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
