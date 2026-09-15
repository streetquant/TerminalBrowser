from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "termdeck-web-prepare-runtime.sh"
LAN_IP = "192.168.50.9"


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    runtime: Path
    script: Path


def _make_layout(tmp_path: Path) -> RuntimeLayout:
    runtime = tmp_path / "runtime"
    tls = runtime / "tls"
    nginx = runtime / "nginx"
    binaries = runtime / "bin"
    tls.mkdir(parents=True)
    nginx.mkdir()
    binaries.mkdir()
    (nginx / "nginx.conf").write_text(
        "server {\n    listen 0.0.0.0:7443 ssl;\n}\n", encoding="utf-8"
    )
    server = binaries / "termdeck-web-server"
    server.write_bytes(b"TermDeck test web server\n")
    server.chmod(0o755)
    ca_key = tls / "ca.key"
    ca_cert = tls / "ca.crt"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=termdeck-test-ca",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "7",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ca_key.chmod(0o600)
    (tls / "ca.srl").write_text("1000\n", encoding="ascii")

    fake_ip = tmp_path / "ip"
    fake_ip.write_text(
        "#!/usr/bin/bash\nprintf '1.1.1.1 via 192.168.50.1 dev test0 src " + LAN_IP + "\\n'\n",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)
    script = tmp_path / "termdeck-web-prepare-runtime.sh"
    script_text = SOURCE.read_text(encoding="utf-8")
    script_text = script_text.replace(
        "runtime=/warp/runtime/termdeck/termdeck-lan",
        "runtime=" + shlex.quote(str(runtime)),
        1,
    ).replace("/usr/bin/ip", shlex.quote(str(fake_ip))).replace(
        "/usr/bin/python3 -I -", "/usr/bin/python3 -"
    )
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    return RuntimeLayout(tmp_path, runtime, script)


def _run(layout: RuntimeLayout, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        ["/usr/bin/bash", str(layout.script)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=60,
    )


def _host_map() -> bytes:
    return (
        "localhost 1;\n"
        "127.0.0.1 1;\n"
        "termdeck.local 1;\n"
        "max.local 1;\n"
        f"{LAN_IP} 1;\n"
        "maxchat.semiotech.org 1;\n"
        "semiotech.org 1;\n"
        "termdeck.semiotech.org 1;\n"
    ).encode("ascii")


def _assert_published(layout: RuntimeLayout) -> None:
    runtime = layout.runtime
    hosts = runtime / "nginx" / "allowed-hosts.map"
    current = runtime / "current-lan-ip"
    receipt_path = runtime / "build-receipt.json"
    assert hosts.read_bytes() == _host_map()
    assert current.read_bytes() == (LAN_IP + "\n").encode("ascii")
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert stat.S_IMODE(current.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o644
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "termdeck.lan-runtime-build.v1"
    assert receipt["ok"] is True
    assert receipt["lan_ip"] == LAN_IP
    assert receipt["https_port"] == 7443
    for relative, expected in receipt["files"].items():
        assert expected == hashlib.sha256((runtime / relative).read_bytes()).hexdigest()


def _hook_environment(tmp_path: Path, source: str) -> dict[str, str]:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(source, encoding="utf-8")
    inherited = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": str(hooks) + (os.pathsep + inherited if inherited else "")}


def test_prepare_runtime_publishes_exact_durable_runtime_files(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    first = _run(layout)
    assert first.returncode == 0, first.stderr
    assert first.stdout == ""
    _assert_published(layout)

    # The second run exercises the tombstone/no-replace replacement route.
    second = _run(layout)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""
    _assert_published(layout)
    assert not list(layout.runtime.rglob(".termdeck-runtime-stage-*"))
    assert not list(layout.runtime.glob(".termdeck-runtime-transaction-*"))


def test_prepare_runtime_retains_private_rotation_work_without_recursive_cleanup(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)

    result = _run(layout)

    assert result.returncode == 0, result.stderr
    rotations = list((layout.runtime / "tls").glob(".rotate.*"))
    assert len(rotations) == 1
    assert stat.S_IMODE(rotations[0].stat().st_mode) == 0o700
    assert (rotations[0] / "server.csr").is_file()
    source = SOURCE.read_text(encoding="utf-8")
    assert 'rm -rf -- "$work"' not in source
    assert "-CAserial" not in source


@pytest.mark.parametrize(
    "relative",
    ("nginx/allowed-hosts.map", "current-lan-ip", "build-receipt.json"),
)
def test_prepare_runtime_refuses_final_symlink_without_touching_target(
    tmp_path: Path, relative: str
) -> None:
    layout = _make_layout(tmp_path)
    outside = tmp_path / "outside-symlink"
    outside.write_bytes(b"foreign symlink target\n")
    target = layout.runtime / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside)

    result = _run(layout)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert outside.read_bytes() == b"foreign symlink target\n"
    assert target.is_symlink()


@pytest.mark.parametrize(
    "relative",
    ("nginx/allowed-hosts.map", "current-lan-ip", "build-receipt.json"),
)
def test_prepare_runtime_refuses_final_hardlink_without_touching_target(
    tmp_path: Path, relative: str
) -> None:
    layout = _make_layout(tmp_path)
    outside = tmp_path / "outside-hardlink"
    outside.write_bytes(b"foreign hardlink target\n")
    target = layout.runtime / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(outside, target)
    outside_before = outside.stat()

    result = _run(layout)

    assert result.returncode != 0
    assert "hard links" in result.stderr
    assert outside.read_bytes() == b"foreign hardlink target\n"
    assert target.stat().st_ino == outside_before.st_ino
    assert outside.stat().st_nlink == 2


def test_prepare_runtime_preserves_foreign_regular_receipt(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    receipt = layout.runtime / "build-receipt.json"
    receipt.write_bytes(b"foreign regular receipt\n")
    host_map = layout.runtime / "nginx" / "allowed-hosts.map"
    host_map.write_bytes(b"map must remain untouched\n")

    result = _run(layout)

    assert result.returncode != 0
    assert "not a managed TermDeck runtime receipt" in result.stderr
    assert receipt.read_bytes() == b"foreign regular receipt\n"
    assert host_map.read_bytes() == b"map must remain untouched\n"


def test_prepare_runtime_refuses_parent_swap_without_touching_foreign_map(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"before parent swap\n")
    foreign_parent = tmp_path / "foreign-nginx"
    foreign_parent.mkdir()
    foreign = foreign_parent / target.name
    foreign.write_bytes(b"foreign parent map\n")
    detached = layout.runtime / "nginx-detached"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

root = {str(layout.runtime)!r}
foreign = {str(foreign_parent)!r}
detached = {str(detached)!r}
original_fsync = os.fsync
swapped = False

def fsync(descriptor):
    global swapped
    try:
        target = os.readlink(f\"/proc/self/fd/{{descriptor}}\")
    except OSError:
        target = ""
    if len(sys.argv) == 4 and not swapped and target == root + "/nginx":
        os.rename(root + "/nginx", detached)
        os.symlink(foreign, root + "/nginx")
        swapped = True
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert foreign.read_bytes() == b"foreign parent map\n"
    assert (detached / target.name).read_bytes() == b"before parent swap\n"
    assert (layout.runtime / "nginx").is_symlink()


def test_prepare_runtime_refuses_real_parent_swap_after_preflight(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"managed map before real-parent swap\n")
    foreign_parent = tmp_path / "foreign-real-nginx"
    foreign_parent.mkdir()
    foreign = foreign_parent / target.name
    foreign.write_bytes(b"foreign real-parent map\n")
    detached = layout.runtime / "nginx-before-real-swap"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

root = {str(layout.runtime)!r}
foreign = {str(foreign_parent)!r}
detached = {str(detached)!r}

# The short IPv4-validation helper runs after directory preflight and before
# the final descriptor-rooted publication helper.
if len(sys.argv) == 2:
    os.rename(root + "/nginx", detached)
    os.rename(foreign, root + "/nginx")
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert "identity changed since preflight" in result.stderr
    assert (layout.runtime / "nginx" / target.name).read_bytes() == b"foreign real-parent map\n"
    assert (detached / target.name).read_bytes() == b"managed map before real-parent swap\n"


def test_prepare_runtime_refuses_parent_mode_change_after_preflight(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"managed map before mode change\n")
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

if len(sys.argv) == 2:
    os.chmod({str(layout.runtime / "nginx")!r}, 0o750)
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert "identity changed since preflight" in result.stderr
    assert target.read_bytes() == b"managed map before mode change\n"


def test_prepare_runtime_refuses_tls_parent_swap_before_rotation(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    foreign_tls = tmp_path / "foreign-tls"
    foreign_tls.mkdir()
    marker = foreign_tls / "foreign-marker"
    marker.write_bytes(b"foreign TLS parent\n")
    detached = layout.runtime / "tls-before-swap"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

if len(sys.argv) == 2:
    os.rename({str(layout.runtime / "tls")!r}, {str(detached)!r})
    os.rename({str(foreign_tls)!r}, {str(layout.runtime / "tls")!r})
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert "runtime directory guard refused" in result.stderr
    assert (layout.runtime / "tls" / "foreign-marker").read_bytes() == b"foreign TLS parent\n"
    assert (detached / "ca.crt").is_file()
    assert not (layout.runtime / "nginx" / "allowed-hosts.map").exists()


def test_prepare_runtime_refuses_symlinked_nginx_parent_without_publishing_map(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    nginx = layout.runtime / "nginx"
    foreign_parent = tmp_path / "foreign-nginx-parent"
    foreign_parent.mkdir()
    foreign = foreign_parent / "allowed-hosts.map"
    foreign.write_bytes(b"foreign symlinked-parent map\n")
    (nginx / "nginx.conf").unlink()
    nginx.rmdir()
    nginx.symlink_to(foreign_parent, target_is_directory=True)

    result = _run(layout)

    assert result.returncode != 0
    assert "runtime directory preparation refused" in result.stderr
    assert foreign.read_bytes() == b"foreign symlinked-parent map\n"
    assert not (foreign_parent / "logs").exists()
    assert not (foreign_parent / "temp").exists()


def test_prepare_runtime_restores_foreign_leaf_that_wins_tombstone_race(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"managed before race\n")
    foreign_source = tmp_path / "foreign-source"
    foreign_source.write_bytes(b"foreign leaf won race\n")
    foreign_identity = foreign_source.stat().st_ino
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
target_path = runtime + "/nginx/allowed-hosts.map"
foreign_source = {str(foreign_source)!r}
original_fsync = os.fsync
replaced = False

def fsync(descriptor):
    global replaced
    try:
        current = os.readlink(f\"/proc/self/fd/{{descriptor}}\")
    except OSError:
        current = ""
    if len(sys.argv) == 4 and not replaced and current == runtime + "/nginx":
        os.replace(foreign_source, target_path)
        replaced = True
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert target.read_bytes() == b"foreign leaf won race\n"
    assert target.stat().st_ino == foreign_identity
    assert list((layout.runtime / "nginx").glob(".termdeck-runtime-stage-*"))


def test_prepare_runtime_staged_write_failure_preserves_public_target(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"before write failure\n")
    environment = _hook_environment(
        tmp_path,
        """
import errno
import os
import sys

original_write = os.write

def write(descriptor, content):
    try:
        path = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError:
        path = ""
    if len(sys.argv) == 4 and ".termdeck-runtime-stage-" in path and path.endswith("/payload"):
        raise OSError(errno.EIO, "injected staged write failure")
    return original_write(descriptor, content)

os.write = write
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert target.read_bytes() == b"before write failure\n"
    assert list((layout.runtime / "nginx").glob(".termdeck-runtime-stage-*"))


def test_prepare_runtime_preserves_foreign_stage_replacement_during_cleanup(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-empty-stage"
    foreign.mkdir()
    (foreign / "foreign-marker").write_bytes(b"do not delete foreign stage\n")
    detached = tmp_path / "detached-managed-stage"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign)!r}
detached = {str(detached)!r}
original_stat = os.stat
swapped = False

def stat(path, *args, **kwargs):
    global swapped
    result = original_stat(path, *args, **kwargs)
    if (
        len(sys.argv) == 4
        and not swapped
        and isinstance(path, str)
        and path.startswith(".termdeck-runtime-stage-")
        and kwargs.get("dir_fd") is not None
    ):
        parent = runtime + "/nginx"
        stages = [name for name in os.listdir(parent) if name.startswith(".termdeck-runtime-stage-")]
        disposals = [name for name in os.listdir(parent) if name.startswith(".termdeck-runtime-dispose-")]
        if len(stages) == 1 and disposals and not os.listdir(parent + "/" + stages[0]):
            os.rename(parent + "/" + stages[0], detached)
            os.rename(foreign, parent + "/" + stages[0])
            swapped = True
    return result

os.stat = stat
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert (layout.runtime / "nginx" / "allowed-hosts.map").read_bytes() == _host_map()
    assert (detached).is_dir()
    recovered = [
        candidate / "stage" / "foreign-marker"
        for candidate in (layout.runtime / "nginx").glob(".termdeck-runtime-dispose-*")
        if (candidate / "stage" / "foreign-marker").is_file()
    ]
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"do not delete foreign stage\n"


def test_prepare_runtime_retains_foreign_child_after_isolated_cleanup_check(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-isolated-child"
    foreign.mkdir()
    (foreign / "foreign-marker").write_bytes(b"do not remove isolated foreign child\n")
    detached = tmp_path / "detached-verified-stage"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign)!r}
detached = {str(detached)!r}
original_stat = os.stat
swapped = False

def stat(path, *args, **kwargs):
    global swapped
    result = original_stat(path, *args, **kwargs)
    if len(sys.argv) == 4 and not swapped and path == "stage" and kwargs.get("dir_fd") is not None:
        try:
            parent = os.readlink(f"/proc/self/fd/{{kwargs['dir_fd']}}")
        except OSError:
            parent = ""
        if parent.startswith(runtime + "/nginx/.termdeck-runtime-dispose-"):
            os.rename(parent + "/stage", detached)
            os.rename(foreign, parent + "/stage")
            swapped = True
    return result

os.stat = stat
""",
    )

    result = _run(layout, environment)

    assert result.returncode == 0, result.stderr
    _assert_published(layout)
    assert detached.is_dir()
    recovered = [
        candidate / "stage" / "foreign-marker"
        for candidate in (layout.runtime / "nginx").glob(".termdeck-runtime-dispose-*")
        if (candidate / "stage" / "foreign-marker").is_file()
    ]
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"do not remove isolated foreign child\n"


def test_prepare_runtime_final_parent_fsync_failure_retains_recovery_evidence(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    target.write_bytes(b"before final fsync failure\n")
    initial_inode = target.stat().st_ino
    environment = _hook_environment(
        tmp_path,
        f"""
import errno
import os
import sys

runtime = {str(layout.runtime)!r}
target_path = runtime + "/nginx/allowed-hosts.map"
initial_inode = {initial_inode}
original_fsync = os.fsync

def fsync(descriptor):
    try:
        path = os.readlink(f\"/proc/self/fd/{{descriptor}}\")
        current_inode = os.lstat(target_path).st_ino
    except OSError:
        path = ""
        current_inode = initial_inode
    if len(sys.argv) == 4 and path == runtime + "/nginx" and current_inode != initial_inode:
        raise OSError(errno.EIO, "injected final parent fsync failure")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert target.read_bytes() == _host_map()
    stages = list((layout.runtime / "nginx").glob(".termdeck-runtime-stage-*"))
    assert len(stages) == 1
    assert (stages[0] / "prior").read_bytes() == b"before final fsync failure\n"


def test_prepare_runtime_invalidates_receipt_before_partial_update_and_recovers_on_retry(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()

    # An unrelated look-alike must survive both the failed transaction and its
    # later retry; recovery never sweeps a matching prefix from the runtime.
    foreign_journal = layout.runtime / ".termdeck-runtime-transaction-foreign"
    foreign_journal.mkdir()
    (foreign_journal / "foreign-marker").write_bytes(b"foreign journal\n")

    current = layout.runtime / "current-lan-ip"
    outside = tmp_path / "foreign-current-hardlink"
    outside.write_bytes(b"foreign current-lan-ip\n")
    current.unlink()
    os.link(outside, current)

    failed = _run(layout)

    assert failed.returncode != 0
    assert "hard links" in failed.stderr
    assert not (layout.runtime / "build-receipt.json").exists()
    assert outside.read_bytes() == b"foreign current-lan-ip\n"
    journals = [
        candidate
        for candidate in layout.runtime.glob(".termdeck-runtime-transaction-*")
        if (candidate / "prior").is_file()
    ]
    assert len(journals) == 1
    assert (journals[0] / "prior").read_bytes() == old_receipt
    assert (foreign_journal / "foreign-marker").read_bytes() == b"foreign journal\n"

    current.unlink()
    retried = _run(layout)

    assert retried.returncode == 0, retried.stderr
    _assert_published(layout)
    assert (journals[0] / "prior").read_bytes() == old_receipt
    assert (foreign_journal / "foreign-marker").read_bytes() == b"foreign journal\n"
