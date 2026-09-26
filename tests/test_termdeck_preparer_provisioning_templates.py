from __future__ import annotations

import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "systemd"
SYSUSERS = DEPLOY / "termdeck-preparer.sysusers.conf"
TMPFILES = DEPLOY / "termdeck-preparer.tmpfiles.conf"
ATTESTOR = DEPLOY / "termdeck-runtime-attestd.service"
BROKER_SERVICE = DEPLOY / "termdeck-preparer-broker.service"
BROKER_SOCKET = DEPLOY / "termdeck-preparer-broker.socket"


def _active_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _unit_values(path: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in _active_lines(path):
        if line.startswith("["):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid unit directive: {line}"
        values.setdefault(key, []).append(value)
    return values


def test_sysusers_declares_only_the_dedicated_disabled_preparer() -> None:
    lines = [shlex.split(line, comments=True) for line in _active_lines(SYSUSERS)]

    assert lines == [
        ["g", "termdeck-preparer", "-"],
        [
            "u",
            "termdeck-preparer",
            "-:termdeck-preparer",
            "TermDeck runtime preparer",
            "/var/lib/termdeck-preparer",
            "/usr/bin/nologin",
        ],
    ]
    assert not any("max" in fields for fields in lines)


def test_tmpfiles_separates_preparer_state_from_root_signing_state() -> None:
    lines = [shlex.split(line, comments=True) for line in _active_lines(TMPFILES)]

    assert lines == [
        ["d", "/var/lib/termdeck-preparer", "0700", "termdeck-preparer", "termdeck-preparer", "-"],
        [
            "d",
            "/var/lib/termdeck-preparer/runtime",
            "0700",
            "termdeck-preparer",
            "termdeck-preparer",
            "-",
        ],
        [
            "z",
            "/var/lib/termdeck-preparer/.termdeck-tls-recovery-key",
            "0600",
            "termdeck-preparer",
            "termdeck-preparer",
            "-",
        ],
        ["d", "/etc/termdeck", "0755", "root", "root", "-"],
        ["d", "/etc/termdeck/control", "0710", "root", "termdeck-preparer", "-"],
        ["d", "/etc/termdeck/control/records", "0700", "root", "root", "-"],
    ]
    assert not any(
        "runtime-attestd.key" in fields
        or fields[0] in {"f", "f+", "w", "w+"}
        for fields in lines
    )
    assert not any(
        "/warp/projects/softwares/termdeck/.termdeck" in fields for fields in lines
    )


def test_attestor_is_root_only_and_sees_only_the_canonical_receipt() -> None:
    values = _unit_values(ATTESTOR)

    assert values["User"] == ["root"]
    assert values["Group"] == ["root"]
    assert values["ExecStart"] == ["/usr/libexec/termdeck-runtime-attestd daemon"]
    assert "ExecStartPre" not in values
    assert values["ReadWritePaths"] == ["/etc/termdeck/control"]
    assert values["ReadOnlyPaths"] == [
        "/etc/termdeck/control/runtime-attestd.key /etc/termdeck/runtime-attestd.pub"
    ]
    assert "TemporaryFileSystem" not in values
    assert values["BindReadOnlyPaths"] == ["/var/lib/termdeck-preparer/runtime"]
    assert values["ConditionPathIsDirectory"] == [
        "/etc/termdeck/control",
        "/etc/termdeck/control/records",
        "/var/lib/termdeck-preparer/runtime",
    ]
    assert "/var/lib/termdeck-preparer/runtime/build-receipt.json" not in values.get(
        "ConditionPathExists", []
    )
    assert values["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert values["PrivateNetwork"] == ["yes"]
    assert values["NoNewPrivileges"] == ["yes"]
    assert values["CapabilityBoundingSet"] == ["CAP_CHOWN CAP_DAC_READ_SEARCH"]
    assert values["AmbientCapabilities"] == [""]
    assert values["ProtectSystem"] == ["strict"]
    assert values["ProtectHome"] == ["yes"]
    assert values["MemoryDenyWriteExecute"] == ["yes"]
    assert not any(key.startswith("Socket") for key in values)
    assert "/warp/projects/softwares/termdeck/.termdeck" not in ATTESTOR.read_text(
        encoding="utf-8"
    )


def test_broker_socket_is_private_to_the_fixed_max_caller() -> None:
    values = _unit_values(BROKER_SOCKET)

    assert values["ListenStream"] == ["/run/termdeck-preparer-broker/request.sock"]
    assert values["SocketUser"] == ["max"]
    # SocketGroup is intentionally omitted: systemd resolves max's numeric
    # NSS primary pw_gid (users on the deployment host), and the broker checks
    # that same value before accepting a request.
    assert "SocketGroup" not in values
    assert values["SocketMode"] == ["0600"]
    assert values["DirectoryMode"] == ["0711"]
    assert values["Accept"] == ["no"]
    assert values["FileDescriptorName"] == ["termdeck-preparer-broker"]
    assert values["Service"] == ["termdeck-preparer-broker.service"]
    assert values["Backlog"] == ["8"]
    assert values["RemoveOnStop"] == ["yes"]
    assert values["FlushPending"] == ["no"]


def test_broker_is_socket_only_root_dispatch_with_fixed_child_state_paths() -> None:
    values = _unit_values(BROKER_SERVICE)

    assert values["User"] == ["root"]
    assert values["Group"] == ["root"]
    assert values["ExecStart"] == ["/usr/libexec/termdeck-preparer-broker"]
    assert values["Sockets"] == ["termdeck-preparer-broker.socket"]
    assert values["RefuseManualStart"] == ["yes"]
    assert values["Restart"] == ["no"]
    assert values["TimeoutStartSec"] == ["200s"]
    assert values["RuntimeMaxSec"] == ["200s"]
    assert values["RuntimeDirectory"] == ["termdeck-preparer-broker"]
    assert values["RuntimeDirectoryMode"] == ["0711"]
    assert values["ReadWritePaths"] == [
        "/var/lib/termdeck-preparer /warp/projects/softwares/termdeck/.termdeck"
    ]
    assert values["TemporaryFileSystem"] == ["/warp:ro"]
    assert values["BindPaths"] == ["/warp/projects/softwares/termdeck/.termdeck"]
    assert "ConditionPathExists" not in values
    assert values["CapabilityBoundingSet"] == [
        "CAP_CHOWN CAP_DAC_OVERRIDE CAP_SETUID CAP_SETGID CAP_KILL"
    ]
    assert values["AmbientCapabilities"] == ["CAP_SETUID"]
    assert values["NoNewPrivileges"] == ["yes"]
    assert values["RestrictAddressFamilies"] == ["AF_UNIX AF_NETLINK"]
    assert "PrivateNetwork" not in values
    assert values["ProtectSystem"] == ["strict"]
    assert values["ProtectHome"] == ["yes"]
    assert values["MemoryDenyWriteExecute"] == ["yes"]
    assert "Environment" not in values


def test_templates_do_not_grant_generic_privilege_escalation() -> None:
    names = {path.name for path in DEPLOY.iterdir() if path.is_file()}
    added = {
        SYSUSERS.name,
        TMPFILES.name,
        ATTESTOR.name,
        BROKER_SERVICE.name,
        BROKER_SOCKET.name,
    }
    assert added <= names
    assert not any("sudo" in name or "polkit" in name for name in added)
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (SYSUSERS, TMPFILES, ATTESTOR, BROKER_SERVICE, BROKER_SOCKET)
    )
    assert "NOPASSWD" not in text
    assert "pkexec" not in text
