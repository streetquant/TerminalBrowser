#!/usr/bin/env python3
"""Verify managed-GUI inventory against stable fields in the real procfs.

The release gate must never infer GUI ownership from ``pgrep -f`` or mutable
scheduler state.  This smoke exercises the source scanner against the configured
managed release and proves it ignores its own CLI-shaped process.  A direct
observer independently rechecks every reported identity tuple; a restricted
observer instead requires the authenticated two-sample systemd-user-manager
receipt and verifies that two brokered calls agree exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _start_ticks(raw: str) -> int:
    end = raw.rfind(")")
    fields = raw[end + 2 :].split() if end >= 0 else []
    if len(fields) <= 19:
        raise AssertionError("proc stat has no start-time field")
    return int(fields[19])


def _effective_uid(raw: str) -> int:
    for line in raw.splitlines():
        if line.startswith("Uid:"):
            values = line.split(":", 1)[1].split()
            return int(values[1])
    raise AssertionError("proc status has no Uid field")


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not value:
        raise AssertionError("procfs exposed an empty boot ID")
    return value


def _read_identity(pid: int, *, boot_id: str) -> dict[str, object]:
    process = Path("/proc") / str(pid)
    stat_before = (process / "stat").read_text(encoding="utf-8")
    cmdline_before = (process / "cmdline").read_bytes()
    status = (process / "status").read_text(encoding="utf-8")
    executable = (process / "exe").resolve(strict=True)
    stat_after = (process / "stat").read_text(encoding="utf-8")
    cmdline_after = (process / "cmdline").read_bytes()
    if _start_ticks(stat_before) != _start_ticks(stat_after):
        raise AssertionError(f"PID {pid} changed identity during verification")
    if cmdline_before != cmdline_after:
        raise AssertionError(f"PID {pid} changed argv during verification")
    return {
        "boot_id": boot_id,
        "pid": pid,
        "process_start_ticks": _start_ticks(stat_after),
        "uid": _effective_uid(status),
        "executable": str(executable),
        "argument_count": len(tuple(part for part in cmdline_after.split(b"\0") if part)),
        "cmdline_sha256": hashlib.sha256(cmdline_after).hexdigest(),
    }


def main() -> int:
    from termdeck.release_manager import managed_gui_process_status, managed_release_status

    first = managed_gui_process_status()
    second = managed_gui_process_status()
    observed_boot_id = _boot_id()
    for payload in (first, second):
        if payload["complete"] is not True:
            raise AssertionError(f"managed process scan is incomplete: {payload!r}")
        if payload["posture"] not in {"stopped", "single-gui"}:
            raise AssertionError(f"unsafe managed GUI posture: {payload!r}")
        if payload["activation"]["safe"] is not True:
            raise AssertionError(f"activation decision is not safe: {payload!r}")
        if payload["gui_count"] not in (0, 1):
            raise AssertionError(f"unexpected GUI count: {payload!r}")
        if payload["boot_id"] != observed_boot_id:
            raise AssertionError(
                "managed process inventory is not bound to the current boot ID"
            )
        observer = payload.get("observer")
        if not isinstance(observer, dict):
            raise AssertionError("managed process inventory has no observer receipt")
        if observer.get("mode") == "direct":
            if observer != {
                "mode": "direct",
                "direct_complete": True,
                "broker_attempted": False,
                "broker_result": "not-needed",
            }:
                raise AssertionError(f"invalid direct observer receipt: {observer!r}")
        elif observer.get("mode") == "systemd-user-manager":
            if (
                observer.get("direct_complete") is not False
                or observer.get("broker_attempted") is not True
                or observer.get("broker_result") != "accepted"
                or observer.get("stable_samples") != 2
                or not observer.get("direct_issues")
            ):
                raise AssertionError(f"invalid broker observer receipt: {observer!r}")
        else:
            raise AssertionError(f"unknown process observer mode: {observer!r}")
    if first["processes"] != second["processes"]:
        raise AssertionError("managed GUI identity changed between stable scans")
    if first["observer"]["mode"] != second["observer"]["mode"]:
        raise AssertionError("managed GUI observer mode changed between stable scans")

    verified = []
    independent_procfs_recheck = first["observer"]["mode"] == "direct"
    if independent_procfs_recheck:
        for record in first["processes"]:
            observed = _read_identity(int(record["pid"]), boot_id=observed_boot_id)
            for key, value in observed.items():
                if record[key] != value:
                    raise AssertionError(
                        f"managed process {record['pid']} mismatch for {key}: "
                        f"reported={record[key]!r}, observed={value!r}"
                    )
            verified.append(observed)
    else:
        verified = list(first["processes"])

    release = managed_release_status(verify=True)
    if release["ok"] is not True or release["verified"] is not True:
        raise AssertionError(f"managed release verification failed: {release!r}")
    integrated = release["runtime_processes"]
    if integrated["complete"] is not True:
        raise AssertionError(f"integrated process scan is incomplete: {integrated!r}")
    if integrated["processes"] != first["processes"]:
        raise AssertionError("release status and process scanner disagree on GUI identity")
    if integrated["observer"]["mode"] != first["observer"]["mode"]:
        raise AssertionError("release status and process scanner disagree on observer mode")

    encoded = json.dumps(first, sort_keys=True)
    for forbidden in ("--workspace", "--db", "cmdline\"", "argv\""):
        if forbidden in encoded:
            raise AssertionError(f"process inventory exposed private argv data: {forbidden}")

    print(
        json.dumps(
            {
                "ok": True,
                "posture": first["posture"],
                "gui_count": first["gui_count"],
                "complete": first["complete"],
                "activation": first["activation"],
                "verified_processes": verified,
                "release_status_integrated": True,
                "self_cli_ignored": True,
                "private_argv_absent": True,
                "observer": first["observer"],
                "independent_procfs_recheck": independent_procfs_recheck,
                "effective_uid": os.getuid(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
