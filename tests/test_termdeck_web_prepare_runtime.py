from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import sys
import shlex
import signal
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
    attest_helper: Path


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
    attest_helper = tmp_path / "termdeck-preparer-state-fixture"
    attest_helper.write_text(
        """#!/usr/bin/python3
import argparse,json,os,sys
parser=argparse.ArgumentParser()
parser.add_argument('--receipt',required=True)
mode=parser.add_mutually_exclusive_group(required=True)
mode.add_argument('--transaction-id')
mode.add_argument('--verify-existing',action='store_true')
args=parser.parse_args()
flags=os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
fd=os.open(args.receipt,flags)
try:
    before=os.fstat(fd)
    os.lseek(fd,0,os.SEEK_SET)
    raw=os.read(fd,96*1024)
    value=json.loads(raw)
    if args.verify_existing:
        if 'producer_attestation' not in value or 'producer_commitment' in value:
            raise SystemExit('fixture rejected unauthenticated receipt')
    else:
        if 'producer_attestation' in value or 'producer_commitment' in value:
            raise SystemExit('fixture rejected non-core receipt')
        value['producer_attestation']={
            'schema':'termdeck.runtime-attestation.v1',
            'transaction_id':args.transaction_id,
            'receipt_path':args.receipt,
            'receipt_core_sha256':'0'*64,
            'receipt_core_identity':[
                before.st_dev,before.st_ino,before.st_mode&0o7777,before.st_nlink,
                before.st_size,before.st_mtime_ns,before.st_ctime_ns,
            ],
            'request_sha256':'0'*64,
            'issued_at_ns':1,
            'attestation':{'algorithm':'test-ed25519','key_id':'0'*64,'signature':'A'*86},
        }
        payload=(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\\n').encode()
        os.ftruncate(fd,0)
        os.lseek(fd,0,os.SEEK_SET)
        view=memoryview(payload)
        while view:
            written=os.write(fd,view)
            if written<=0:
                raise SystemExit('fixture write failed')
            view=view[written:]
        os.fsync(fd)
finally:
    os.close(fd)
""",
        encoding="utf-8",
    )
    attest_helper.chmod(0o755)
    # Match the managed-wrapper deployment contract: the preparer derives its
    # runtime root from this exact installed leaf, rather than silently using
    # the canonical /warp path when tests supply a custom layout.
    script = binaries / "termdeck-web-prepare"
    script_text = SOURCE.read_text(encoding="utf-8")
    script_text = script_text.replace("/usr/bin/ip", shlex.quote(str(fake_ip))).replace(
        "/usr/bin/python3 -I -", "/usr/bin/python3 -"
    )
    script_text = script_text.replace(
        "/usr/libexec/termdeck-preparer-state", str(attest_helper)
    )
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    return RuntimeLayout(tmp_path, runtime, script, attest_helper)


def _run(
    layout: RuntimeLayout,
    extra_env: dict[str, str] | None = None,
    arguments: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        ["/usr/bin/bash", str(layout.script), *(arguments or [])],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=60,
    )


def _host_map(lan_ip: str = LAN_IP) -> bytes:
    return (
        "localhost 1;\n"
        "127.0.0.1 1;\n"
        "termdeck.local 1;\n"
        "max.local 1;\n"
        f"{lan_ip} 1;\n"
        "maxchat.semiotech.org 1;\n"
        "semiotech.org 1;\n"
        "termdeck.semiotech.org 1;\n"
    ).encode("ascii")


def _assert_published(layout: RuntimeLayout, lan_ip: str = LAN_IP) -> None:
    runtime = layout.runtime
    hosts = runtime / "nginx" / "allowed-hosts.map"
    current = runtime / "current-lan-ip"
    receipt_path = runtime / "build-receipt.json"
    assert hosts.read_bytes() == _host_map(lan_ip)
    assert current.read_bytes() == (lan_ip + "\n").encode("ascii")
    assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
    assert stat.S_IMODE(current.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o644
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {
        "files",
        "https_port",
        "lan_ip",
        "ok",
        "producer_attestation",
        "runtime_publication",
        "schema",
        "tls_publication",
    }
    assert "producer_commitment" not in receipt
    assert receipt["schema"] == "termdeck.lan-runtime-build.v2"
    assert receipt["ok"] is True
    assert receipt["lan_ip"] == lan_ip
    assert receipt["https_port"] == 7443
    for relative, expected in receipt["files"].items():
        assert expected == hashlib.sha256((runtime / relative).read_bytes()).hexdigest()
    publication = receipt["runtime_publication"]
    assert set(publication) == {"generation", "outputs"}
    assert len(publication["generation"]) == 32
    assert all(character in "0123456789abcdef" for character in publication["generation"])
    outputs = publication["outputs"]
    assert set(outputs) == {"current-lan-ip", "nginx/allowed-hosts.map"}
    for relative, target in {
        "current-lan-ip": current,
        "nginx/allowed-hosts.map": hosts,
    }.items():
        entry = outputs[relative]
        observed = target.stat()
        assert set(entry) == {"path", "sha256", "mode", "identity"}
        assert entry["path"] == relative
        assert entry["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        assert entry["mode"] == 0o600
        assert entry["identity"] == [
            observed.st_dev,
            observed.st_ino,
            stat.S_IMODE(observed.st_mode),
            observed.st_nlink,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ]


def _set_fake_ip(layout: RuntimeLayout, lan_ip: str) -> None:
    fake_ip = layout.root / "ip"
    fake_ip.write_text(
        "#!/usr/bin/bash\nprintf '1.1.1.1 via 192.168.50.1 dev test0 src "
        + lan_ip
        + "\\n'\n",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)


def _assert_tls_contract(layout: RuntimeLayout, lan_ip: str) -> None:
    tls = layout.runtime / "tls"
    key_public = subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(tls / "server.key"),
            "-pubout",
            "-outform",
            "DER",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    certificate_public_pem = subprocess.run(
        ["/usr/bin/openssl", "x509", "-in", str(tls / "server.crt"), "-pubkey", "-noout"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    certificate_public = subprocess.run(
        ["/usr/bin/openssl", "pkey", "-pubin", "-pubout", "-outform", "DER"],
        input=certificate_public_pem,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    assert key_public == certificate_public
    assert (tls / "server-chain.crt").read_bytes() == (tls / "server.crt").read_bytes() + (tls / "ca.crt").read_bytes()
    verified = subprocess.run(
        ["/usr/bin/openssl", "verify", "-CAfile", str(tls / "ca.crt"), str(tls / "server.crt")],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert verified.returncode == 0, verified.stderr.decode("utf-8", "replace")
    extension = (tls / "server.ext").read_text(encoding="ascii")
    assert f"IP.1={lan_ip}\n" in extension


def _tls_extension(lan_ip: str) -> bytes:
    return (
        "[v3_req]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=@alt_names\n"
        "[alt_names]\n"
        "DNS.1=termdeck.local\n"
        "DNS.2=max.local\n"
        f"IP.1={lan_ip}\n"
        "IP.2=127.0.0.1\n"
    ).encode("ascii")


def _write_private_key(path: Path) -> None:
    subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-out", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    path.chmod(0o600)


def _replace_with_valid_manifest_mismatched_tls(tls: Path, lan_ip: str, workspace: Path) -> None:
    """Install a valid CA-signed bundle that differs from a signed manifest."""
    workspace.mkdir()
    key = workspace / "server.key"
    extension = workspace / "server.ext"
    request = workspace / "server.csr"
    certificate = workspace / "server.crt"
    chain = workspace / "server-chain.crt"
    _write_private_key(key)
    extension.write_bytes(_tls_extension(lan_ip))
    extension.chmod(0o600)
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-new",
            "-key",
            str(key),
            "-subj",
            "/CN=termdeck.local",
            "-out",
            str(request),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(tls / "ca.crt"),
            "-CAkey",
            str(tls / "ca.key"),
            "-set_serial",
            "0x7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f",
            "-days",
            "825",
            "-sha256",
            "-extfile",
            str(extension),
            "-extensions",
            "v3_req",
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    certificate.chmod(0o644)
    chain.write_bytes(certificate.read_bytes() + (tls / "ca.crt").read_bytes())
    chain.chmod(0o644)
    for source, name in (
        (key, "server.key"),
        (certificate, "server.crt"),
        (chain, "server-chain.crt"),
        (extension, "server.ext"),
    ):
        os.replace(source, tls / name)


def _hook_environment(tmp_path: Path, source: str) -> dict[str, str]:
    hooks = tmp_path / "hooks"
    hooks.mkdir(parents=True)
    marker = tmp_path / "runtime-fault-marker"
    marker_helper = (
        "import builtins as _runtime_fault_builtins\n\n"
        "def _record_runtime_fault(phase):\n"
        f"    with _runtime_fault_builtins.open({str(marker)!r}, 'w', encoding='utf-8') as stream:\n"
        "        stream.write(phase)\n\n"
    )
    (hooks / "sitecustomize.py").write_text(marker_helper + source, encoding="utf-8")
    inherited = os.environ.get("PYTHONPATH", "")
    return {
        "PYTHONPATH": str(hooks) + (os.pathsep + inherited if inherited else ""),
        "TERMDECK_TEST_RUNTIME_FAULT_MARKER": str(marker),
    }


def _assert_runtime_fault(environment: dict[str, str], phase: str) -> None:
    assert Path(environment["TERMDECK_TEST_RUNTIME_FAULT_MARKER"]).read_text(encoding="utf-8") == phase


def _tls_boundary_fault_environment(
    tmp_path: Path,
    layout: RuntimeLayout,
    *,
    boundary: str,
    mode: str,
    old_tls_inode: int,
) -> dict[str, str]:
    assert boundary in {"receipt", "exchange", "retention"}
    assert mode in {"interrupt", "kill", "fsync"}
    environment = _hook_environment(
        tmp_path,
        f"""
import errno
import os
import signal
import sys

runtime = {str(layout.runtime)!r}
boundary = {boundary!r}
mode = {mode!r}
old_tls_inode = {old_tls_inode!r}
original_fsync = os.fsync
injected = False
fault_marker = os.environ.get("TERMDECK_TEST_TLS_FAULT_MARKER", "")

def reached_boundary():
    try:
        journals = [
            name for name in os.listdir(runtime)
            if (
                name.startswith(".termdeck-tls-transaction-")
                and os.path.isfile(os.path.join(runtime, name, "ready"))
                and not os.path.exists(os.path.join(runtime, name, "complete"))
            )
        ]
        if len(journals) != 1:
            return False
        journal = os.path.join(runtime, journals[0])
        transaction_id = journals[0][len(".termdeck-tls-transaction-"):]
        candidate = os.path.join(runtime, ".termdeck-tls-candidate-" + transaction_id)
        prior_receipt = os.path.join(journal, "prior-build-receipt.json")
        prior_tls = os.path.join(journal, "prior-tls")
        if not os.path.isfile(os.path.join(journal, "ready")) or not os.path.isfile(prior_receipt):
            return False
        if boundary == "receipt":
            return os.path.isdir(candidate) and not os.path.exists(prior_tls)
        if boundary == "exchange":
            return (
                os.path.isdir(candidate)
                and os.lstat(candidate).st_ino == old_tls_inode
                and not os.path.exists(prior_tls)
            )
        return os.path.isdir(prior_tls)
    except OSError:
        return False

def fsync(descriptor):
    global injected
    if (
        not injected
        and len(sys.argv) == 6
        and sys.argv[1] == "publish"
        and reached_boundary()
    ):
        # Receipt invalidation and prior-generation retention each fsync the
        # journal first and the runtime parent second.  The interruption and
        # SIGKILL cases model a crash after the whole rename boundary is
        # durable, so wait for the latter.  The EIO case deliberately keeps
        # exercising that earlier journal fsync failure separately.
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{{descriptor}}")
        except OSError:
            descriptor_path = ""
        if mode in {{"interrupt", "kill"}} and descriptor_path != runtime:
            return original_fsync(descriptor)
        injected = True
        if fault_marker:
            with open(fault_marker, "w", encoding="ascii") as marker:
                marker.write(boundary + ":" + mode + ":" + descriptor_path + "\\n")
        if mode == "fsync":
            raise OSError(errno.EIO, "injected TLS transaction fsync failure")
        # BaseException and SIGKILL model the window after the directory
        # metadata has reached the storage stack, rather than a pre-fsync
        # exception that merely skips durability.
        original_fsync(descriptor)
        if mode == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "interrupt":
            raise KeyboardInterrupt("injected TLS transaction interruption")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )
    environment["TERMDECK_TEST_TLS_FAULT_MARKER"] = str(
        tmp_path / f"tls-{boundary}-{mode}-fault-marker"
    )
    return environment


def _tls_rename_race_environment(
    tmp_path: Path,
    layout: RuntimeLayout,
    *,
    phase: str,
    foreign: Path,
    detached: Path,
) -> dict[str, str]:
    assert phase in {"receipt", "exchange", "candidate", "candidate_postexchange", "retention"}
    return _hook_environment(
        tmp_path,
        f"""
import ctypes
import os
import sys

runtime = {str(layout.runtime)!r}
phase = {phase!r}
foreign = {str(foreign)!r}
detached = {str(detached)!r}
original_cdll = ctypes.CDLL
injected = False

class RenameAt2:
    def __init__(self, function):
        object.__setattr__(self, "function", function)

    def __call__(self, source_fd, source, destination_fd, destination, flags):
        global injected
        source_name = os.fsdecode(source)
        destination_name = os.fsdecode(destination)
        is_exchange = source_name == "tls" and destination_name.startswith(".termdeck-tls-candidate-") and flags == 2
        if (
            not injected
            and len(sys.argv) == 6
            and sys.argv[1] == "publish"
            and (
                (phase == "receipt" and source_name == "build-receipt.json" and destination_name == "prior-build-receipt.json" and flags == 1)
                or (phase in {{"exchange", "candidate", "candidate_postexchange"}} and is_exchange)
                or (phase == "retention" and source_name.startswith(".termdeck-tls-candidate-") and destination_name == "prior-tls" and flags == 1)
            )
        ):
            injected = True
            if phase == "receipt":
                os.replace(foreign, os.path.join(runtime, source_name))
            elif phase == "exchange":
                os.rename(os.path.join(runtime, "tls"), detached)
                os.rename(foreign, os.path.join(runtime, "tls"))
            elif phase == "candidate":
                os.rename(os.path.join(runtime, destination_name), detached)
                os.rename(foreign, os.path.join(runtime, destination_name))
            elif phase == "candidate_postexchange":
                result = self.function(source_fd, source, destination_fd, destination, flags)
                os.rename(os.path.join(runtime, destination_name), detached)
                os.rename(foreign, os.path.join(runtime, destination_name))
                return result
            else:
                os.rename(os.path.join(runtime, source_name), detached)
                os.rename(foreign, os.path.join(runtime, source_name))
        return self.function(source_fd, source, destination_fd, destination, flags)

    def __setattr__(self, name, value):
        if name == "function":
            object.__setattr__(self, name, value)
        else:
            setattr(self.function, name, value)


class Library:
    def __init__(self, library):
        self.library = library

    def __getattr__(self, name):
        value = getattr(self.library, name)
        if name == "renameat2":
            return RenameAt2(value)
        return value


def cdll(*args, **kwargs):
    return Library(original_cdll(*args, **kwargs))


ctypes.CDLL = cdll
""",
    )


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
    # The transaction retains only its signed work directory at journal root.
    # Leaving it under `prior-tls` would make a later inverse restore an
    # inventory that the runtime receipt did not authorize.
    rotations = list(layout.runtime.glob(".termdeck-tls-transaction-*/rotation-evidence-*"))
    assert len(rotations) == 1
    assert stat.S_IMODE(rotations[0].stat().st_mode) == 0o700
    assert (rotations[0] / "server.csr").is_file()
    source = SOURCE.read_text(encoding="utf-8")
    assert 'rm -rf -- "$work"' not in source
    assert "-CAserial" not in source


@pytest.mark.parametrize("leaf", ("server.key", "server.crt", "server-chain.crt", "server.ext"))
def test_prepare_runtime_refuses_tls_final_directory_symlink_without_touching_foreign_target(
    tmp_path: Path, leaf: str
) -> None:
    layout = _make_layout(tmp_path)
    outside = tmp_path / "outside-tls-directory-symlink"
    outside.mkdir()
    destination = layout.runtime / "tls" / leaf
    destination.symlink_to(outside, target_is_directory=True)

    result = _run(layout)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert destination.is_symlink()
    assert not (outside / leaf).exists()
    assert not (layout.runtime / "build-receipt.json").exists()


@pytest.mark.parametrize("leaf", ("server.key", "server.crt", "server-chain.crt", "server.ext"))
def test_prepare_runtime_refuses_tls_final_hardlink_without_touching_foreign_target(
    tmp_path: Path, leaf: str
) -> None:
    layout = _make_layout(tmp_path)
    outside = tmp_path / "outside-tls-hardlink"
    outside.write_bytes(b"foreign TLS hardlink target\n")
    destination = layout.runtime / "tls" / leaf
    os.link(outside, destination)
    outside_before = outside.stat()

    result = _run(layout)

    assert result.returncode != 0
    assert "hard links" in result.stderr
    assert outside.read_bytes() == b"foreign TLS hardlink target\n"
    assert destination.stat().st_ino == outside_before.st_ino
    assert outside.stat().st_nlink == 2
    assert not (layout.runtime / "build-receipt.json").exists()


def test_prepare_runtime_mismatched_tls_key_forces_complete_generation_rotation(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    key = layout.runtime / "tls" / "server.key"
    subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-out", str(key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    mismatched_key = key.read_bytes()

    rotated = _run(layout)

    assert rotated.returncode == 0, rotated.stderr
    _assert_published(layout)
    _assert_tls_contract(layout, LAN_IP)
    assert key.read_bytes() != mismatched_key
    journals = list(layout.runtime.glob(".termdeck-tls-transaction-*"))
    assert any(
        (journal / "prior-build-receipt.json").is_file()
        and (journal / "prior-build-receipt.json").read_bytes() == old_receipt
        for journal in journals
    )
    assert any(
        (journal / "prior-tls" / "server.key").is_file()
        and (journal / "prior-tls" / "server.key").read_bytes() == mismatched_key
        for journal in journals
    )


def test_prepare_runtime_tls_candidate_write_failure_preserves_valid_prior_generation(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    _set_fake_ip(layout, "192.168.50.10")
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
    if (
        len(sys.argv) == 6
        and sys.argv[1] == "publish"
        and ".termdeck-tls-candidate-" in path
        and path.endswith("/server.key")
    ):
        raise OSError(errno.EIO, "injected TLS candidate write failure")
    return original_write(descriptor, content)

os.write = write
""",
    )

    failed = _run(layout, environment)

    assert failed.returncode != 0
    assert (layout.runtime / "build-receipt.json").read_bytes() == old_receipt
    assert (layout.runtime / "tls").stat().st_ino == old_tls_inode
    _assert_tls_contract(layout, LAN_IP)
    assert list(layout.runtime.glob(".termdeck-tls-transaction-*"))
    assert list(layout.runtime.glob(".termdeck-tls-candidate-*"))


@pytest.mark.parametrize("boundary", ("receipt", "exchange", "retention"))
@pytest.mark.parametrize("mode", ("interrupt", "kill", "fsync"))
def test_prepare_runtime_recovers_durable_tls_transaction_after_each_publication_boundary(
    tmp_path: Path,
    boundary: str,
    mode: str,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    new_ip = "192.168.50.10"
    _set_fake_ip(layout, new_ip)

    failed = _run(
        layout,
        _tls_boundary_fault_environment(
            tmp_path,
            layout,
            boundary=boundary,
            mode=mode,
            old_tls_inode=old_tls_inode,
        ),
    )

    assert failed.returncode != 0
    marker = tmp_path / f"tls-{boundary}-{mode}-fault-marker"
    assert marker.is_file()
    recorded = marker.read_text(encoding="ascii")
    assert recorded.startswith(f"{boundary}:{mode}:")
    if mode in {"interrupt", "kill"}:
        assert recorded == f"{boundary}:{mode}:{layout.runtime}\n"
    assert not (layout.runtime / "build-receipt.json").exists()
    journals = list(layout.runtime.glob(".termdeck-tls-transaction-*"))
    assert len(journals) == 2
    journal = next(
        candidate
        for candidate in journals
        if (candidate / "prior-build-receipt.json").is_file()
    )
    assert (journal / "prior-build-receipt.json").read_bytes() == old_receipt

    retried = _run(layout)

    assert retried.returncode == 0, retried.stderr
    _assert_published(layout, new_ip)
    _assert_tls_contract(layout, new_ip)
    assert (journal / "complete").read_bytes() == b"complete\n"
    assert (journal / "prior-tls").is_dir()
    assert (journal / "prior-build-receipt.json").read_bytes() == old_receipt


@pytest.mark.parametrize(
    "phase", ("receipt", "exchange", "candidate", "candidate_postexchange", "retention")
)
def test_prepare_runtime_restores_or_retains_foreign_entry_that_wins_tls_final_name_race(
    tmp_path: Path,
    phase: str,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_map = (layout.runtime / "nginx" / "allowed-hosts.map").read_bytes()
    old_current = (layout.runtime / "current-lan-ip").read_bytes()
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    detached = tmp_path / f"detached-{phase}"
    if phase == "receipt":
        foreign = tmp_path / "foreign-receipt"
        foreign.write_bytes(b"foreign receipt won final-name race\n")
        foreign_inode = foreign.stat().st_ino
    else:
        foreign = tmp_path / f"foreign-{phase}-directory"
        foreign.mkdir()
        (foreign / "foreign-marker").write_bytes(f"foreign {phase} directory\n".encode("ascii"))
        foreign_inode = foreign.stat().st_ino
    _set_fake_ip(layout, "192.168.50.10")

    failed = _run(
        layout,
        _tls_rename_race_environment(
            tmp_path,
            layout,
            phase=phase,
            foreign=foreign,
            detached=detached,
        ),
    )

    assert failed.returncode != 0
    assert (layout.runtime / "nginx" / "allowed-hosts.map").read_bytes() == old_map
    assert (layout.runtime / "current-lan-ip").read_bytes() == old_current
    if phase == "receipt":
        receipt = layout.runtime / "build-receipt.json"
        assert receipt.read_bytes() == b"foreign receipt won final-name race\n"
        assert receipt.stat().st_ino == foreign_inode
        # Descriptor-link restoration keeps the moved foreign inode at its
        # private name as evidence while restoring its vacant public name.
        retained = list(layout.runtime.glob(".termdeck-tls-transaction-*/prior-build-receipt.json"))
        assert len(retained) == 1
        assert retained[0].stat().st_ino == foreign_inode
        assert retained[0].read_bytes() == receipt.read_bytes()
        assert retained[0].stat().st_nlink == receipt.stat().st_nlink == 2
        assert not (retained[0].parent / "complete").exists()
        assert (layout.runtime / "tls").stat().st_ino == old_tls_inode
        return
    if phase == "exchange":
        restored = layout.runtime / "tls"
        assert restored.stat().st_ino != foreign_inode
        assert (restored / "server.key").is_file()
        assert (detached / "ca.crt").is_file()
        candidates = list(layout.runtime.glob(".termdeck-tls-candidate-*"))
        assert len(candidates) == 1
        assert candidates[0].stat().st_ino == foreign_inode
        assert (candidates[0] / "foreign-marker").read_bytes() == b"foreign exchange directory\n"
        assert not (layout.runtime / "build-receipt.json").exists()
        return
    if phase == "candidate":
        restored = layout.runtime / "tls"
        assert restored.stat().st_ino == foreign_inode
        assert (restored / "foreign-marker").read_bytes() == b"foreign candidate directory\n"
        candidates = list(layout.runtime.glob(".termdeck-tls-candidate-*"))
        assert len(candidates) == 1
        assert candidates[0].stat().st_ino == old_tls_inode
        assert (candidates[0] / "ca.crt").is_file()
        assert (detached / "server.key").is_file()
        assert not (layout.runtime / "build-receipt.json").exists()
        return
    if phase == "candidate_postexchange":
        published = layout.runtime / "tls"
        assert published.stat().st_ino != foreign_inode
        assert (published / "server.key").is_file()
        candidates = list(layout.runtime.glob(".termdeck-tls-candidate-*"))
        assert len(candidates) == 1
        assert candidates[0].stat().st_ino == foreign_inode
        assert (candidates[0] / "foreign-marker").read_bytes() == b"foreign candidate_postexchange directory\n"
        assert detached.stat().st_ino == old_tls_inode
        assert (detached / "ca.crt").is_file()
        assert not (layout.runtime / "build-receipt.json").exists()
        return
    candidates = list(layout.runtime.glob(".termdeck-tls-candidate-*"))
    assert not candidates
    retained = [
        path for path in layout.runtime.glob(".termdeck-tls-transaction-*/prior-tls")
        if path.stat().st_ino == foreign_inode
    ]
    assert len(retained) == 1
    assert (retained[0] / "foreign-marker").read_bytes() == b"foreign retention directory\n"
    assert not (retained[0].parent / "complete").exists()
    assert detached.stat().st_ino == old_tls_inode
    assert (detached / "ca.crt").is_file()
    assert (layout.runtime / "tls").stat().st_ino != foreign_inode
    assert (layout.runtime / "tls" / "server.key").is_file()
    assert not (layout.runtime / "build-receipt.json").exists()


@pytest.mark.parametrize("mismatched_name", (False, True))
def test_prepare_runtime_rejects_forged_pending_tls_journal_without_mutating_valid_generation(
    tmp_path: Path,
    mismatched_name: bool,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    journal_id = "a" * 32
    intent_id = "b" * 32 if mismatched_name else journal_id
    journal = layout.runtime / f".termdeck-tls-transaction-{journal_id}"
    candidate = layout.runtime / f".termdeck-tls-candidate-{intent_id}"
    journal.mkdir(mode=0o700)
    candidate.mkdir(mode=0o700)
    receipt_stat = (layout.runtime / "build-receipt.json").stat()
    tls_stat = (layout.runtime / "tls").stat()
    receipt = json.loads(old_receipt)
    previous_intent = json.loads(next(layout.runtime.glob(".termdeck-tls-transaction-*/intent.json")).read_text())
    intent = {
        "auth": "0" * 64,
        "candidate": candidate.name,
        "candidate_identity": [candidate.stat().st_dev, candidate.stat().st_ino, 0o700, candidate.stat().st_uid],
        "candidate_manifest": receipt["tls_publication"]["current"]["manifest"],
        "id": intent_id,
        "lan_ip": LAN_IP,
        "receipt_previous": [receipt_stat.st_dev, receipt_stat.st_ino, stat.S_IMODE(receipt_stat.st_mode), receipt_stat.st_nlink, receipt_stat.st_size, receipt_stat.st_mtime_ns, receipt_stat.st_ctime_ns],
        "rotation_work_identity": previous_intent["rotation_work_identity"],
        "schema": "termdeck.tls-directory-transaction.v1",
        "tls_previous": [tls_stat.st_dev, tls_stat.st_ino, stat.S_IMODE(tls_stat.st_mode), tls_stat.st_uid],
    }
    (journal / "ready").write_bytes(b"ready\n")
    (journal / "ready").chmod(0o600)
    recovery_key = (layout.root / ".termdeck-tls-recovery-key").read_bytes()
    ready_authentication = hmac.new(
        recovery_key,
        b"termdeck.tls-marker.v1\0" + journal.name.encode("ascii") + b"\0ready\0ready\n",
        hashlib.sha256,
    ).hexdigest().encode("ascii") + b"\n"
    (journal / "ready.auth").write_bytes(ready_authentication)
    (journal / "ready.auth").chmod(0o600)
    (journal / "intent.json").write_text(json.dumps(intent, sort_keys=True), encoding="utf-8")
    (journal / "intent.json").chmod(0o600)

    rejected = _run(layout)

    assert rejected.returncode != 0
    if mismatched_name:
        assert "does not bind its journal name" in rejected.stderr
    else:
        assert "authentication failed" in rejected.stderr
    assert (layout.runtime / "build-receipt.json").read_bytes() == old_receipt
    assert (layout.runtime / "tls").stat().st_ino == old_tls_inode
    assert not (journal / "complete").exists()
    key = layout.root / ".termdeck-tls-recovery-key"
    assert key.is_file()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_prepare_runtime_refuses_tampered_crash_candidate_before_any_tls_exchange(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    _set_fake_ip(layout, "192.168.50.10")
    interrupted = _run(
        layout,
        _tls_boundary_fault_environment(
            tmp_path,
            layout,
            boundary="receipt",
            mode="interrupt",
            old_tls_inode=old_tls_inode,
        ),
    )
    assert interrupted.returncode != 0
    journals = [entry for entry in layout.runtime.glob(".termdeck-tls-transaction-*") if (entry / "prior-build-receipt.json").is_file()]
    assert len(journals) == 1
    journal = journals[0]
    transaction_id = journal.name.removeprefix(".termdeck-tls-transaction-")
    candidate = layout.runtime / f".termdeck-tls-candidate-{transaction_id}"
    outside = tmp_path / "tampered-candidate-key"
    outside.write_bytes(b"foreign candidate key\n")
    (candidate / "server.key").unlink()
    (candidate / "server.key").symlink_to(outside)

    retried = _run(layout)

    assert retried.returncode != 0
    assert "symbolic link" in retried.stderr
    assert (layout.runtime / "tls").stat().st_ino == old_tls_inode
    assert not (layout.runtime / "build-receipt.json").exists()
    assert (journal / "prior-build-receipt.json").read_bytes() == old_receipt
    assert not (journal / "exchanged").exists()
    assert (candidate / "server.key").is_symlink()
    assert outside.read_bytes() == b"foreign candidate key\n"


def test_prepare_runtime_refuses_forged_complete_after_exchange_crash_with_valid_manifest_mismatch(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_tls_inode = (layout.runtime / "tls").stat().st_ino
    old_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    new_ip = "192.168.50.10"
    _set_fake_ip(layout, new_ip)

    interrupted = _run(
        layout,
        _tls_boundary_fault_environment(
            tmp_path,
            layout,
            boundary="exchange",
            mode="interrupt",
            old_tls_inode=old_tls_inode,
        ),
    )
    assert interrupted.returncode != 0
    assert not (layout.runtime / "build-receipt.json").exists()
    journals = [
        entry
        for entry in layout.runtime.glob(".termdeck-tls-transaction-*")
        if (entry / "prior-build-receipt.json").is_file()
    ]
    assert len(journals) == 1
    journal = journals[0]

    # The public TLS directory is the durable candidate after RENAME_EXCHANGE.
    # Replace all served leaves with another independently valid CA-signed
    # generation, then forge a terminal phase marker without the parent-held
    # authenticator.  A retry must preserve this evidence and never skip to a
    # new receipt merely because `complete` exists.
    _replace_with_valid_manifest_mismatched_tls(
        layout.runtime / "tls", new_ip, tmp_path / "valid-manifest-mismatch"
    )
    _assert_tls_contract(layout, new_ip)
    (journal / "complete").write_bytes(b"complete\n")
    (journal / "complete").chmod(0o600)
    (journal / "complete.auth").write_bytes(b"0" * 64 + b"\n")
    (journal / "complete.auth").chmod(0o600)

    retried = _run(layout)

    assert retried.returncode != 0
    assert "complete.auth" in retried.stderr
    assert not (layout.runtime / "build-receipt.json").exists()
    assert (journal / "prior-build-receipt.json").read_bytes() == old_receipt
    assert not (journal / "prior-tls").exists()
    _assert_tls_contract(layout, new_ip)


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
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    original_map = target.read_bytes()
    original_inode = target.stat().st_ino
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
    if len(sys.argv) == 5 and not swapped and target == root + "/nginx":
        os.rename(root + "/nginx", detached)
        os.symlink(foreign, root + "/nginx")
        swapped = True
        _record_runtime_fault("parent-swap")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "parent-swap")
    assert result.returncode != 0
    assert foreign.read_bytes() == b"foreign parent map\n"
    assert (detached / target.name).read_bytes() == original_map
    assert (detached / target.name).stat().st_ino == original_inode
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


def test_prepare_runtime_refuses_tls_parent_swap_after_guard_without_staging_foreign_tls_files(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    foreign_tls = tmp_path / "foreign-tls-after-guard"
    foreign_tls.mkdir()
    marker = foreign_tls / "foreign-marker"
    marker.write_bytes(b"foreign TLS parent after guard\n")
    detached = layout.runtime / "tls-after-guard-detached"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign_tls)!r}
detached = {str(detached)!r}
original_close = os.close
swapped = False

def close(descriptor):
    global swapped
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    if len(sys.argv) == 3 and not swapped and path == runtime + "/bin":
        result = original_close(descriptor)
        os.rename(runtime + "/tls", detached)
        os.symlink(foreign, runtime + "/tls")
        swapped = True
        return result
    return original_close(descriptor)

os.close = close
""",
    )

    result = _run(layout, environment)

    assert result.returncode != 0
    assert (layout.runtime / "tls").is_symlink()
    assert marker.read_bytes() == b"foreign TLS parent after guard\n"
    assert not list(foreign_tls.glob(".rotate.*"))
    assert not (foreign_tls / "server.key").exists()
    assert not (foreign_tls / "server.crt").exists()
    assert (detached / "ca.crt").is_file()
    assert not (layout.runtime / "build-receipt.json").exists()


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
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    target = layout.runtime / "nginx" / "allowed-hosts.map"
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
    if len(sys.argv) == 5 and not replaced and current == runtime + "/nginx":
        os.replace(foreign_source, target_path)
        replaced = True
        _record_runtime_fault("tombstone-race")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "tombstone-race")
    assert result.returncode != 0
    assert target.read_bytes() == b"foreign leaf won race\n"
    assert target.stat().st_ino == foreign_identity
    assert list((layout.runtime / "nginx").glob(".termdeck-runtime-stage-*"))


def test_prepare_runtime_staged_write_failure_preserves_public_target(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    original_map = target.read_bytes()
    original_inode = target.stat().st_ino
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
    if len(sys.argv) == 5 and ".termdeck-runtime-stage-" in path and path.endswith("/payload"):
        _record_runtime_fault("staged-write")
        raise OSError(errno.EIO, "injected staged write failure")
    return original_write(descriptor, content)

os.write = write
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "staged-write")
    assert result.returncode != 0
    assert target.read_bytes() == original_map
    assert target.stat().st_ino == original_inode
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
        len(sys.argv) == 5
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
            _record_runtime_fault("stage-replacement")
    return result

os.stat = stat
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "stage-replacement")
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
    if len(sys.argv) == 5 and not swapped and path == "stage" and kwargs.get("dir_fd") is not None:
        try:
            parent = os.readlink(f"/proc/self/fd/{{kwargs['dir_fd']}}")
        except OSError:
            parent = ""
        if parent.startswith(runtime + "/nginx/.termdeck-runtime-dispose-"):
            os.rename(parent + "/stage", detached)
            os.rename(foreign, parent + "/stage")
            swapped = True
            _record_runtime_fault("isolated-child-replacement")
    return result

os.stat = stat
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "isolated-child-replacement")
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
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    target = layout.runtime / "nginx" / "allowed-hosts.map"
    original_map = target.read_bytes()
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
    if len(sys.argv) == 5 and path == runtime + "/nginx" and current_inode != initial_inode:
        _record_runtime_fault("final-parent-fsync")
        raise OSError(errno.EIO, "injected final parent fsync failure")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "final-parent-fsync")
    assert result.returncode != 0
    assert target.read_bytes() == _host_map()
    stages = list((layout.runtime / "nginx").glob(".termdeck-runtime-stage-*"))
    assert len(stages) == 1
    assert (stages[0] / "prior").read_bytes() == original_map
    assert (stages[0] / "prior").stat().st_ino == initial_inode


def test_prepare_runtime_refuses_source_drift_before_receipt_publication(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-ca-before-receipt"
    foreign.write_bytes(b"foreign CA replacement before receipt\n")
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign)!r}
original_fsync = os.fsync
swapped = False

def fsync(descriptor):
    global swapped
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    if len(sys.argv) == 5 and not swapped and path == runtime + "/nginx":
        os.replace(foreign, runtime + "/tls/ca.crt")
        swapped = True
        _record_runtime_fault("source-before-receipt")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "source-before-receipt")
    assert result.returncode != 0
    assert "source postimages changed" in result.stderr
    assert (layout.runtime / "tls" / "ca.crt").read_bytes() == b"foreign CA replacement before receipt\n"
    assert not (layout.runtime / "build-receipt.json").exists()


def test_prepare_runtime_tombstones_new_receipt_when_source_drifts_after_receipt_rename(
    tmp_path: Path,
) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-ca-after-receipt"
    foreign.write_bytes(b"foreign CA replacement after receipt\n")
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign)!r}
original_fsync = os.fsync
swapped = False

def fsync(descriptor):
    global swapped
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    if (
        len(sys.argv) == 5
        and not swapped
        and path == runtime
        and os.path.isfile(runtime + "/build-receipt.json")
    ):
        os.replace(foreign, runtime + "/tls/ca.crt")
        swapped = True
        _record_runtime_fault("source-after-receipt")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "source-after-receipt")
    assert result.returncode != 0
    assert "sources changed during final publication" in result.stderr
    assert (layout.runtime / "tls" / "ca.crt").read_bytes() == b"foreign CA replacement after receipt\n"
    assert not (layout.runtime / "build-receipt.json").exists()
    assert any(
        (stage / "prior").is_file()
        for stage in layout.runtime.glob(".termdeck-runtime-transaction-*")
    )


@pytest.mark.parametrize("after_receipt", (False, True))
def test_prepare_runtime_rejects_tls_key_swap_at_final_receipt_boundary(
    tmp_path: Path,
    after_receipt: bool,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_receipt_inode = (layout.runtime / "build-receipt.json").stat().st_ino
    old_map_inode = (layout.runtime / "nginx" / "allowed-hosts.map").stat().st_ino
    foreign_key = tmp_path / "foreign-server.key"
    _write_private_key(foreign_key)
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign_key = {str(foreign_key)!r}
after_receipt = {after_receipt!r}
old_receipt_inode = {old_receipt_inode!r}
old_map_inode = {old_map_inode!r}
original_fsync = os.fsync
swapped = False

def fsync(descriptor):
    global swapped
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    receipt = runtime + "/build-receipt.json"
    host_map = runtime + "/nginx/allowed-hosts.map"
    should_swap = (
        path == runtime
        and os.path.isfile(receipt)
        and os.lstat(receipt).st_ino != old_receipt_inode
        if after_receipt
        else (
            path == runtime + "/nginx"
            and os.path.isfile(host_map)
            and os.lstat(host_map).st_ino != old_map_inode
        )
    )
    if len(sys.argv) == 5 and not swapped and should_swap:
        os.replace(foreign_key, runtime + "/tls/server.key")
        swapped = True
        _record_runtime_fault("tls-key-at-receipt")
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "tls-key-at-receipt")
    assert result.returncode != 0
    assert "TLS postimage" in result.stderr
    assert not (layout.runtime / "build-receipt.json").exists()
    assert not foreign_key.exists()
    assert stat.S_IMODE((layout.runtime / "tls" / "server.key").stat().st_mode) == 0o600
    assert any(
        (stage / "prior").is_file()
        for stage in layout.runtime.glob(".termdeck-runtime-transaction-*")
    )


def test_prepare_runtime_refuses_private_stage_mode_drift_before_payload_publish(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    environment = _hook_environment(
        tmp_path,
        """
import os
import sys

original_open = os.open
changed = False

def open(path, flags, mode=0o777, *, dir_fd=None):
    global changed
    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
    if len(sys.argv) == 5 and not changed and path == "payload" and dir_fd is not None:
        try:
            stage = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:
            stage = ""
        if ".termdeck-runtime-stage-" in stage:
            os.chmod(stage, 0o755)
            changed = True
            _record_runtime_fault("private-stage-mode")
    return descriptor

os.open = open
""",
    )

    result = _run(layout, environment)

    _assert_runtime_fault(environment, "private-stage-mode")
    assert result.returncode != 0
    assert "private stage changed" in result.stderr
    stages = list(layout.runtime.glob("nginx/.termdeck-runtime-stage-*"))
    assert len(stages) == 1
    assert stat.S_IMODE(stages[0].stat().st_mode) == 0o755
    assert not (layout.runtime / "build-receipt.json").exists()


def test_prepare_runtime_rejects_postpublication_foreign_replacement_until_receipt_is_explicitly_invalidated(
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
    # v2 binds the exact leaf identity, so an out-of-band replacement is
    # rejected before this invocation moves either the prior receipt or a
    # public output.  The old receipt is stale evidence, not a valid receipt
    # for the hardlinked leaf.
    assert (layout.runtime / "build-receipt.json").read_bytes() == old_receipt
    assert outside.read_bytes() == b"foreign current-lan-ip\n"
    assert not [
        candidate
        for candidate in layout.runtime.glob(".termdeck-runtime-transaction-*")
        if (candidate / "prior").is_file()
    ]
    assert (foreign_journal / "foreign-marker").read_bytes() == b"foreign journal\n"

    # The operator retires the receipt and its remaining owned map together.
    # Once the receipt is absent, a new run cannot infer ownership of that
    # public map and must not silently replace it.  Keep both retired leaves
    # as explicit operator evidence while the new transaction starts empty.
    current.unlink()
    retired_receipt = tmp_path / "explicitly-retired-build-receipt.json"
    retired_map = tmp_path / "explicitly-retired-allowed-hosts.map"
    original_map_inode = (layout.runtime / "nginx" / "allowed-hosts.map").stat().st_ino
    (layout.runtime / "build-receipt.json").rename(retired_receipt)
    (layout.runtime / "nginx" / "allowed-hosts.map").rename(retired_map)
    retried = _run(layout)

    assert retried.returncode == 0, retried.stderr
    _assert_published(layout)
    assert retired_receipt.read_bytes() == old_receipt
    assert retired_map.read_bytes() == _host_map()
    assert retired_map.stat().st_ino == original_map_inode
    assert outside.read_bytes() == b"foreign current-lan-ip\n"
    assert (foreign_journal / "foreign-marker").read_bytes() == b"foreign journal\n"


def test_prepare_runtime_restores_authenticated_prior_tls_generation(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_tls = {
        name: (layout.runtime / "tls" / name).read_bytes()
        for name in ("ca.crt", "ca.key", "server.key", "server.crt", "server-chain.crt", "server.ext")
    }
    _set_fake_ip(layout, "192.168.50.10")
    rotated = _run(layout)
    assert rotated.returncode == 0, rotated.stderr
    receipt = json.loads((layout.runtime / "build-receipt.json").read_text(encoding="utf-8"))
    publication = receipt["tls_publication"]
    assert publication["prior"] is not None
    generation = publication["generation"]

    restored = _run(layout, arguments=["--restore-tls-generation", generation])

    assert restored.returncode == 0, restored.stderr
    result = json.loads(restored.stdout)
    assert result["ok"] is True
    assert result["action"] == "restored-runtime-generation"
    assert result["generation"] == generation
    receipt_result = result["receipt"]
    assert receipt_result["path"] == "build-receipt.json"
    assert receipt_result["sha256"] == hashlib.sha256(
        (layout.runtime / "build-receipt.json").read_bytes()
    ).hexdigest()
    assert receipt_result["identity"] == [
        (layout.runtime / "build-receipt.json").stat().st_dev,
        (layout.runtime / "build-receipt.json").stat().st_ino,
        stat.S_IMODE((layout.runtime / "build-receipt.json").stat().st_mode),
        (layout.runtime / "build-receipt.json").stat().st_nlink,
        (layout.runtime / "build-receipt.json").stat().st_size,
        (layout.runtime / "build-receipt.json").stat().st_mtime_ns,
        (layout.runtime / "build-receipt.json").stat().st_ctime_ns,
    ]
    for name, expected in old_tls.items():
        assert (layout.runtime / "tls" / name).read_bytes() == expected
    assert (layout.runtime / "current-lan-ip").read_bytes() == (LAN_IP + "\n").encode("ascii")
    assert (layout.runtime / "nginx" / "allowed-hosts.map").read_bytes() == _host_map(LAN_IP)
    assert not [name for name in os.listdir(layout.runtime / "tls") if name.startswith(".rotate.")]
    journal = layout.runtime / f".termdeck-tls-transaction-{generation}"
    assert (journal / "prior-tls" / "server.ext").read_bytes() != old_tls["server.ext"]
    assert (journal / f"rotation-evidence-{generation}" / "server.csr").is_file()
    _assert_published(layout, LAN_IP)

    retried = _run(layout, arguments=["--restore-tls-generation", generation])
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == result


def _tls_post_exchange_pre_receipt_kill_environment(tmp_path: Path, layout: RuntimeLayout) -> dict[str, str]:
    """Kill the final publisher after B is complete but before its receipt."""
    return _hook_environment(
        tmp_path,
        f"""
import os
import signal
import sys

runtime = {str(layout.runtime)!r}
original_fsync = os.fsync
injected = False

def fsync(descriptor):
    global injected
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    journals = [
        name for name in os.listdir(runtime)
        if (
            name.startswith(".termdeck-tls-transaction-")
            and os.path.isfile(os.path.join(runtime, name, "complete"))
            and os.path.isdir(os.path.join(runtime, name, "prior-tls"))
            and os.path.isfile(os.path.join(runtime, name, "prior-build-receipt.json"))
        )
    ] if os.path.isdir(runtime) else []
    if (
        not injected
        # This is the final runtime-publisher Python helper, after the TLS
        # helper has completed B and invalidated A's receipt.
        and len(sys.argv) == 5
        and sys.argv[1] == runtime
        and not os.path.exists(os.path.join(runtime, "build-receipt.json"))
        and len(journals) == 1
    ):
        injected = True
        original_fsync(descriptor)
        os.kill(os.getpid(), signal.SIGKILL)
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )


def test_prepare_runtime_discovers_unreceipted_tls_generation_for_outer_rollback(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_tls = {
        name: (layout.runtime / "tls" / name).read_bytes()
        for name in ("ca.crt", "ca.key", "server.key", "server.crt", "server-chain.crt", "server.ext")
    }
    _set_fake_ip(layout, "192.168.50.10")

    crashed = _run(layout, _tls_post_exchange_pre_receipt_kill_environment(tmp_path, layout))

    assert crashed.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
    assert not (layout.runtime / "build-receipt.json").exists()
    journals = [
        journal
        for journal in layout.runtime.glob(".termdeck-tls-transaction-*")
        if (journal / "complete").is_file()
        and (journal / "prior-tls").is_dir()
        and (journal / "prior-build-receipt.json").is_file()
    ]
    assert len(journals) == 1
    journal = journals[0]

    restored = _run(layout, arguments=["--restore-unreceipted-tls-generation"])

    assert restored.returncode == 0, restored.stderr
    result = json.loads(restored.stdout)
    assert result["ok"] is True
    assert result["discovered"] is True
    assert result["action"] == "restored-runtime-generation"
    assert result["receipt"]["path"] == "build-receipt.json"
    assert (layout.runtime / "build-receipt.json").is_file()
    for name, expected in old_tls.items():
        assert (layout.runtime / "tls" / name).read_bytes() == expected
    _assert_published(layout, LAN_IP)
    assert not [name for name in os.listdir(layout.runtime / "tls") if name.startswith(".rotate.")]

    # A crash after the discovery control returns but before activation writes
    # its checkpoint must be safe to replay: the freshly issued A receipt is
    # itself the proof that this one completed inverse already won.
    retried = _run(layout, arguments=["--restore-unreceipted-tls-generation"])
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == result


def _tls_pre_ready_kill_environment(tmp_path: Path, layout: RuntimeLayout, phase: str) -> dict[str, str]:
    assert phase in {"intent", "ready-auth", "inert-auth"}
    return _hook_environment(
        tmp_path,
        f"""
import os
import signal
import sys

runtime = {str(layout.runtime)!r}
phase = {phase!r}
original_fsync = os.fsync
injected = False

def fsync(descriptor):
    global injected
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    journals = [
        os.path.join(runtime, name)
        for name in os.listdir(runtime)
        if (
            name.startswith(".termdeck-tls-transaction-")
            # A successful bootstrap/older generation is intentionally
            # retained as authenticated evidence.  The crash target is the
            # one newly-created, still-incomplete journal.
            and not os.path.exists(os.path.join(runtime, name, "complete"))
        )
    ] if os.path.isdir(runtime) else []
    should_kill = False
    # `intent` and `ready.auth` occur while the publisher is staging a new
    # generation.  A missing ready marker is classified inert by the next
    # `recover` invocation, which writes `inert.auth` there instead.
    expected_mode = "recover" if phase == "inert-auth" else "publish"
    if len(sys.argv) == 6 and sys.argv[1] == expected_mode and not injected and len(journals) == 1:
        journal = journals[0]
        if phase == "intent":
            should_kill = path == journal and os.path.isfile(os.path.join(journal, "intent.json")) and not os.path.exists(os.path.join(journal, "ready.auth"))
        elif phase == "ready-auth":
            should_kill = path == journal and os.path.isfile(os.path.join(journal, "ready.auth")) and not os.path.exists(os.path.join(journal, "ready"))
        else:
            should_kill = path == journal and os.path.isfile(os.path.join(journal, "inert.auth")) and not os.path.exists(os.path.join(journal, "inert"))
    if should_kill:
        injected = True
        original_fsync(descriptor)
        os.kill(os.getpid(), signal.SIGKILL)
    return original_fsync(descriptor)

os.fsync = fsync
""",
    )


@pytest.mark.parametrize("phase", ("intent", "ready-auth"))
def test_prepare_runtime_recovers_authenticated_pre_ready_crash_windows(tmp_path: Path, phase: str) -> None:
    layout = _make_layout(tmp_path)
    assert _run(layout).returncode == 0
    _set_fake_ip(layout, "192.168.50.10")

    crashed = _run(layout, _tls_pre_ready_kill_environment(tmp_path, layout, phase))

    # The inner Python helper is killed, so bash reports either its signal
    # status directly or the conventional 128 + signal exit status.
    assert crashed.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
    recovered = _run(layout)
    assert recovered.returncode == 0, recovered.stderr
    assert _run(layout).returncode == 0
    _assert_published(layout, "192.168.50.10")


def test_prepare_runtime_recovers_inert_auth_only_crash_window(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    assert _run(layout).returncode == 0
    _set_fake_ip(layout, "192.168.50.10")
    first = _run(layout, _tls_pre_ready_kill_environment(tmp_path / "intent", layout, "intent"))
    assert first.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
    second = _run(layout, _tls_pre_ready_kill_environment(tmp_path / "inert", layout, "inert-auth"))
    assert second.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}

    recovered = _run(layout)
    assert recovered.returncode == 0, recovered.stderr
    assert _run(layout).returncode == 0
    assert any(journal.joinpath("inert").is_file() for journal in layout.runtime.glob(".termdeck-tls-transaction-*"))


def test_prepare_runtime_preserves_sessions_and_unrelated_work_across_rotation_and_recovery(
    tmp_path: Path,
) -> None:
    import sqlite3

    layout = _make_layout(tmp_path)
    # TERMDECK_STATE_ROOT owns sessions.sqlite3 and terminal-auth in the
    # deployed service. Keep a real WAL-mode database open throughout these
    # transitions, as an existing application session would.
    state = tmp_path / "state" / "termdeck"
    state.mkdir(parents=True, mode=0o750)
    database = state / "sessions.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, window_id TEXT, "
            "tab_index INTEGER, visible INTEGER, recovery_command TEXT, recovery_cwd TEXT)"
        )
        saved_session = (
            "remembered-tab",
            "research-window",
            7,
            1,
            "codex resume 00000000-0000-4000-8000-000000000001",
            str(tmp_path / "research"),
        )
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", saved_session)
        connection.commit()
        database.chmod(0o640)
        assert database.with_name("sessions.sqlite3-wal").stat().st_size > 0
        assert database.with_name("sessions.sqlite3-shm").is_file()

        work = layout.runtime / "unrelated-work"
        files = {
            state / "recovery-intents" / "remembered-tab.json": (
                b'{"id":"remembered-tab","intent":"active"}\n', 0o600
            ),
            state / "terminal-auth" / "research" / "config.toml": (
                b'model = "test-model"\n', 0o640
            ),
            work / "draft.md": (b"Uncommitted research notes\n", 0o640),
            work / "resume.sh": (b"#!/bin/sh\nexit 0\n", 0o750),
            layout.runtime / "nginx" / "logs" / "access.log": (
                b"Existing access log evidence\n", 0o640
            ),
        }
        for path, (content, mode) in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
        work.chmod(0o750)

        def preserved_snapshot() -> dict[Path, tuple[object, ...]]:
            paths = {state, work, *state.rglob("*"), *work.rglob("*"), *files}
            snapshot = {}
            for path in paths:
                observed = path.lstat()
                snapshot[path] = (
                    observed.st_dev,
                    observed.st_ino,
                    observed.st_mode,
                    observed.st_nlink,
                    observed.st_mtime_ns,
                    path.read_bytes() if stat.S_ISREG(observed.st_mode) else None,
                )
            return snapshot

        expected = preserved_snapshot()
        environment = {"TERMDECK_STATE_ROOT": str(state)}
        initial = _run(layout, environment)
        assert initial.returncode == 0, initial.stderr
        _assert_published(layout)
        assert preserved_snapshot() == expected, "initial provisioning changed existing work"
        initial_tls_inode = (layout.runtime / "tls").stat().st_ino
        initial_certificate = (layout.runtime / "tls" / "server.crt").read_bytes()

        rotated_ip = "192.168.50.10"
        _set_fake_ip(layout, rotated_ip)
        rotated = _run(layout, environment)
        assert rotated.returncode == 0, rotated.stderr
        _assert_published(layout, rotated_ip)
        _assert_tls_contract(layout, rotated_ip)
        assert (layout.runtime / "tls").stat().st_ino != initial_tls_inode
        assert (layout.runtime / "tls" / "server.crt").read_bytes() != initial_certificate
        assert preserved_snapshot() == expected, "TLS rotation changed existing work"

        recovery_ip = "192.168.50.11"
        _set_fake_ip(layout, recovery_ip)
        fault = _tls_boundary_fault_environment(
            tmp_path / "session-preservation-fault",
            layout,
            boundary="exchange",
            mode="interrupt",
            old_tls_inode=(layout.runtime / "tls").stat().st_ino,
        )
        interrupted = _run(layout, {**environment, **fault})
        assert interrupted.returncode != 0
        assert Path(fault["TERMDECK_TEST_TLS_FAULT_MARKER"]).read_text() == (
            f"exchange:interrupt:{layout.runtime}\n"
        )
        pending = [
            journal
            for journal in layout.runtime.glob(".termdeck-tls-transaction-*")
            if (journal / "ready").is_file() and not (journal / "complete").exists()
        ]
        assert len(pending) == 1
        assert not (layout.runtime / "build-receipt.json").exists()
        assert preserved_snapshot() == expected, "interrupted rotation changed existing work"

        recovered = _run(layout, environment)
        assert recovered.returncode == 0, recovered.stderr
        assert (pending[0] / "complete").read_bytes() == b"complete\n"
        _assert_published(layout, recovery_ip)
        _assert_tls_contract(layout, recovery_ip)
        assert preserved_snapshot() == expected, "transaction recovery changed existing work"
        assert connection.execute("SELECT * FROM sessions").fetchall() == [saved_session]
    finally:
        connection.close()


def test_prepare_runtime_reuses_completed_rotation_on_same_ip_retry(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    rotated_ip = "192.168.50.10"
    _set_fake_ip(layout, rotated_ip)
    rotated = _run(layout)
    assert rotated.returncode == 0, rotated.stderr
    receipt = json.loads((layout.runtime / "build-receipt.json").read_bytes())
    tls_identity = (layout.runtime / "tls").stat().st_ino
    prior = receipt["tls_publication"]["prior"]
    assert prior is not None
    journal = layout.runtime / prior["journal"]
    retained = {
        name: ((journal / name).stat().st_ino, (journal / name).read_bytes())
        for name in ("prior-build-receipt.json", "prior-current-lan-ip", "prior-allowed-hosts.map")
    }

    retried = _run(layout)

    assert retried.returncode == 0, retried.stderr
    _assert_published(layout, rotated_ip)
    assert (layout.runtime / "tls").stat().st_ino == tls_identity
    assert retained == {
        name: ((journal / name).stat().st_ino, (journal / name).read_bytes())
        for name in retained
    }


@pytest.mark.parametrize("relative", ("build-receipt.json", "current-lan-ip", "nginx/allowed-hosts.map"))
def test_prepare_runtime_preserves_foreign_leaf_before_publication_preflight(
    tmp_path: Path, relative: str,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    foreign = tmp_path / "foreign-preflight-leaf"
    foreign.write_bytes(b"foreign leaf before publication preflight\n")
    foreign.chmod(0o600)
    foreign_inode = foreign.stat().st_ino
    marker = tmp_path / "runtime-preflight-injected"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
relative = {relative!r}
foreign = {str(foreign)!r}
marker = {str(marker)!r}

def trace(frame, event, argument):
    if (
        len(sys.argv) == 5 and sys.argv[1] == runtime
        and event == "call" and frame.f_code.co_name == "_publish"
        and frame.f_locals.get("label") == relative
    ):
        sys.settrace(None)
        os.replace(foreign, runtime + "/" + relative)
        with open(marker, "w") as stream:
            stream.write(relative)
    return None

sys.settrace(trace)
""",
    )

    failed = _run(layout, environment)

    assert marker.read_text() == relative
    assert failed.returncode != 0
    assert "before no-replace publication" in failed.stderr or "since managed receipt validation" in failed.stderr
    target = layout.runtime / relative
    assert target.stat().st_ino == foreign_inode
    assert target.stat().st_nlink == 1
    assert target.read_bytes() == b"foreign leaf before publication preflight\n"


@pytest.mark.parametrize("relative", ("current-lan-ip", "nginx/allowed-hosts.map"))
def test_prepare_runtime_withdraws_receipt_when_output_changes_after_receipt_rename(
    tmp_path: Path, relative: str,
) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-postrename-output"
    foreign.write_bytes(b"foreign output after receipt rename\n")
    foreign.chmod(0o600)
    foreign_inode = foreign.stat().st_ino
    marker = tmp_path / "runtime-postrename-injected"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
relative = {relative!r}
foreign = {str(foreign)!r}
marker = {str(marker)!r}

def trace(frame, event, argument):
    if len(sys.argv) != 5 or sys.argv[1] != runtime:
        return None
    if frame.f_code.co_name != "_renameat2":
        return None
    if (
        event == "return" and frame.f_locals.get("source") == "payload"
        and frame.f_locals.get("destination") == "build-receipt.json"
    ):
        sys.settrace(None)
        os.replace(foreign, runtime + "/" + relative)
        with open(marker, "w") as stream:
            stream.write(str(os.lstat(runtime + "/build-receipt.json").st_ino))
    return trace

sys.settrace(trace)
""",
    )

    failed = _run(layout, environment)

    receipt_inode = int(marker.read_text())
    assert failed.returncode != 0
    assert "runtime publication postimage changed" in failed.stderr
    assert not (layout.runtime / "build-receipt.json").exists()
    assert (layout.runtime / relative).stat().st_ino == foreign_inode
    assert (layout.runtime / relative).read_bytes() == b"foreign output after receipt rename\n"
    assert any(
        (stage / "prior").is_file() and (stage / "prior").stat().st_ino == receipt_inode
        for stage in layout.runtime.glob(".termdeck-runtime-transaction-*")
    )


@pytest.mark.parametrize("boundary", ("rename", "fsync", "verification"))
def test_prepare_runtime_withdraws_owned_receipt_on_baseexception(
    tmp_path: Path, boundary: str,
) -> None:
    layout = _make_layout(tmp_path)
    marker = tmp_path / "runtime-interruption-injected"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
boundary = {boundary!r}
marker = {str(marker)!r}

def trace(frame, event, argument):
    if len(sys.argv) != 5 or sys.argv[1] != runtime:
        return None
    function = frame.f_code.co_name
    matched = (
        boundary == "rename" and function == "_renameat2" and event == "return"
        and frame.f_locals.get("source") == "payload"
        and frame.f_locals.get("destination") == "build-receipt.json"
    ) or (
        boundary == "fsync" and function == "_fsync" and event == "return"
        and frame.f_locals.get("label") == "publication parent after publishing build-receipt.json"
    ) or (
        boundary == "verification" and function == "_verify_runtime_publication_receipt"
        and event == "call" and os.path.isfile(runtime + "/build-receipt.json")
    )
    if matched:
        sys.settrace(None)
        with open(marker, "w") as stream:
            stream.write(str(os.lstat(runtime + "/build-receipt.json").st_ino))
        raise KeyboardInterrupt("injected runtime publication interruption")
    return trace if function in {{"_renameat2", "_fsync"}} else None

sys.settrace(trace)
""",
    )

    failed = _run(layout, environment)

    receipt_inode = int(marker.read_text())
    assert failed.returncode != 0
    assert "KeyboardInterrupt: injected runtime publication interruption" in failed.stderr
    assert not (layout.runtime / "build-receipt.json").exists()
    assert any(
        (stage / "prior").is_file() and (stage / "prior").stat().st_ino == receipt_inode
        for stage in layout.runtime.glob(".termdeck-runtime-transaction-*")
    )


@pytest.mark.parametrize("foreign_kind", ("regular", "symlink"))
def test_prepare_runtime_preserves_original_baseexception_when_receipt_cleanup_refuses_foreign_leaf(
    tmp_path: Path, foreign_kind: str,
) -> None:
    layout = _make_layout(tmp_path)
    foreign = tmp_path / "foreign-interruption-receipt"
    foreign.write_bytes(b"foreign receipt during interruption\n")
    foreign_inode = foreign.stat().st_ino
    retained = tmp_path / "detached-owned-receipt"
    marker = tmp_path / "runtime-cleanup-injected"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys

runtime = {str(layout.runtime)!r}
foreign = {str(foreign)!r}
foreign_kind = {foreign_kind!r}
retained = {str(retained)!r}
marker = {str(marker)!r}

def trace(frame, event, argument):
    if (
        len(sys.argv) == 5 and sys.argv[1] == runtime and event == "call"
        and frame.f_code.co_name == "_verify_runtime_publication_receipt"
        and os.path.isfile(runtime + "/build-receipt.json")
    ):
        sys.settrace(None)
        os.rename(runtime + "/build-receipt.json", retained)
        if foreign_kind == "regular":
            os.rename(foreign, runtime + "/build-receipt.json")
        else:
            os.symlink(foreign, runtime + "/build-receipt.json")
        with open(marker, "w") as stream:
            stream.write(foreign_kind)
        raise KeyboardInterrupt("original runtime publication interruption")
    return None

sys.settrace(trace)
""",
    )

    failed = _run(layout, environment)

    assert marker.read_text() == foreign_kind
    assert failed.returncode != 0
    assert "KeyboardInterrupt: original runtime publication interruption" in failed.stderr
    assert "could not invalidate owned build receipt" in failed.stderr
    public = layout.runtime / "build-receipt.json"
    assert public.stat().st_ino == foreign_inode
    assert public.read_bytes() == b"foreign receipt during interruption\n"
    assert public.is_symlink() == (foreign_kind == "symlink")
    assert retained.is_file()


def test_prepare_runtime_propagates_second_provenance_failure(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    calls = tmp_path / "provenance-calls"
    final_publisher = tmp_path / "final-publisher-called"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys
from pathlib import Path

if len(sys.argv) == 6 and sys.argv[1] == "provenance":
    calls = Path({str(calls)!r})
    count = int(calls.read_text()) + 1 if calls.exists() else 1
    calls.write_text(str(count))
    os._exit(2 if count == 1 else 7)
if len(sys.argv) == 5 and sys.argv[1] == {str(layout.runtime)!r}:
    Path({str(final_publisher)!r}).write_text("called")
""",
    )

    result = _run(layout, environment)

    assert calls.read_text() == "2"
    assert result.returncode == 7, result.stderr
    assert not final_publisher.exists()
    assert not (layout.runtime / "build-receipt.json").exists()


@pytest.mark.parametrize("relative", ("", "tls", "nginx"), ids=("runtime", "tls", "nginx"))
@pytest.mark.parametrize("boundary", ("fchmod", "fsync"))
def test_prepare_runtime_preserves_replacement_during_preflight(
    tmp_path: Path, relative: str, boundary: str,
) -> None:
    layout = _make_layout(tmp_path)
    source = layout.runtime / relative
    original_inode = source.stat().st_ino
    original_work = source / "preexisting-work"
    original_work.write_bytes(b"existing work remains intact\n")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o750)
    replacement.chmod(0o750)
    replacement_inode = replacement.stat().st_ino
    (replacement / "marker").write_bytes(b"replacement must remain untouched\n")
    detached = tmp_path / "detached"
    injected = tmp_path / "preflight-swap-fired"
    environment = _hook_environment(
        tmp_path,
        f"""
import os
import sys
from pathlib import Path

original = getattr(os, {boundary!r})
swapped = False

def mutate(descriptor, *arguments):
    global swapped
    result = original(descriptor, *arguments)
    if len(sys.argv) == 8 and not swapped:
        try:
            path = os.readlink(f"/proc/self/fd/{{descriptor}}")
        except OSError:
            path = ""
        if path == {str(source)!r}:
            swapped = True
            os.rename({str(source)!r}, {str(detached)!r})
            os.rename({str(replacement)!r}, {str(source)!r})
            Path({str(injected)!r}).write_text("swapped")
    return result

setattr(os, {boundary!r}, mutate)
""",
    )

    result = _run(layout, environment)

    assert injected.read_text() == "swapped"
    assert result.returncode != 0
    assert "directory identity changed during preflight" in result.stderr
    assert source.stat().st_ino == replacement_inode
    assert stat.S_IMODE(source.stat().st_mode) == 0o750
    assert sorted(path.name for path in source.iterdir()) == ["marker"]
    assert (source / "marker").read_bytes() == b"replacement must remain untouched\n"
    assert detached.stat().st_ino == original_inode
    assert (detached / "preexisting-work").read_bytes() == b"existing work remains intact\n"


@pytest.mark.parametrize("unreceipted", (False, True), ids=("generation", "unreceipted"))
def test_prepare_runtime_restores_authenticated_generation_without_network(
    tmp_path: Path, unreceipted: bool,
) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    old_tls = {
        name: (layout.runtime / "tls" / name).read_bytes()
        for name in ("ca.crt", "ca.key", "server.key", "server.crt", "server-chain.crt", "server.ext")
    }
    _set_fake_ip(layout, "192.168.50.10")
    if unreceipted:
        rotated = _run(layout, _tls_post_exchange_pre_receipt_kill_environment(tmp_path, layout))
        assert rotated.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
        assert not (layout.runtime / "build-receipt.json").exists()
        arguments = ["--restore-unreceipted-tls-generation"]
    else:
        rotated = _run(layout)
        assert rotated.returncode == 0, rotated.stderr
        receipt = json.loads((layout.runtime / "build-receipt.json").read_text())
        arguments = ["--restore-tls-generation", receipt["tls_publication"]["generation"]]
    ip_called = tmp_path / "offline-ip-called"
    (tmp_path / "ip").write_text(
        "#!/usr/bin/bash\n"
        + "printf 'called\\n' > " + shlex.quote(str(ip_called)) + "\nexit 1\n",
        encoding="utf-8",
    )

    restored = _run(layout, arguments=arguments)

    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["ok"] is True
    assert not ip_called.exists(), "rollback must use authenticated stored LAN addresses"
    for name, expected in old_tls.items():
        assert (layout.runtime / "tls" / name).read_bytes() == expected
    _assert_published(layout, LAN_IP)


def test_prepare_runtime_requires_binary_directory_before_changing_modes(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path)
    direct_script = tmp_path / "direct-prepare.sh"
    layout.script.rename(direct_script)
    (layout.runtime / "bin").rename(tmp_path / "binaries")
    directories = (layout.runtime, layout.runtime / "tls", layout.runtime / "nginx")
    for directory in directories:
        directory.chmod(0o750)
    before = {
        directory: (directory.stat().st_ino, stat.S_IMODE(directory.stat().st_mode))
        for directory in directories
    }

    result = _run(
        RuntimeLayout(layout.root, layout.runtime, direct_script),
        {"TERMDECK_RUNTIME_ROOT": str(layout.runtime)},
    )

    assert result.returncode != 0
    assert "required directory is missing" in result.stderr
    for directory, expected in before.items():
        assert (directory.stat().st_ino, stat.S_IMODE(directory.stat().st_mode)) == expected
    assert not (layout.runtime / "nginx" / "logs").exists()
    assert not (layout.runtime / "nginx" / "temp").exists()



def _tls_review_rotated_layout(tmp_path: Path) -> tuple[RuntimeLayout, str, Path]:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    _set_fake_ip(layout, "192.168.50.10")
    rotated = _run(layout)
    assert rotated.returncode == 0, rotated.stderr
    receipt = json.loads((layout.runtime / "build-receipt.json").read_text(encoding="utf-8"))
    generation = receipt["tls_publication"]["generation"]
    journal = layout.runtime / f".termdeck-tls-transaction-{generation}"
    return layout, generation, journal


def _tls_review_public_snapshot(layout: RuntimeLayout) -> dict[str, tuple[object, ...]]:
    paths = [
        "build-receipt.json",
        "current-lan-ip",
        "nginx/allowed-hosts.map",
        *("tls/" + name for name in (
            "ca.crt", "ca.key", "ca.srl", "server.key", "server.crt", "server-chain.crt", "server.ext",
        )),
    ]
    result = {}
    for name in paths:
        path = layout.runtime / name
        observed = path.stat()
        result[name] = (
            observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink,
            observed.st_mtime_ns, observed.st_ctime_ns, path.read_bytes(),
        )
    tls = (layout.runtime / "tls").stat()
    result["tls"] = (tls.st_dev, tls.st_ino, tls.st_mode, tls.st_uid)
    return result


@pytest.mark.parametrize("retained_name", (
    "prior-tls/server.key",
    "prior-tls/ca.crt",
    "prior-current-lan-ip",
    "prior-allowed-hosts.map",
))
def test_tls_review_rejects_changed_retained_generation_before_public_mutation(
    tmp_path: Path, retained_name: str,
) -> None:
    layout, generation, journal = _tls_review_rotated_layout(tmp_path)
    expected = _tls_review_public_snapshot(layout)
    changed = journal / retained_name
    changed.write_bytes(b"changed retained generation evidence\n")
    changed_inode = changed.stat().st_ino

    rejected = _run(layout, arguments=["--restore-tls-generation", generation])

    assert rejected.returncode != 0
    assert _tls_review_public_snapshot(layout) == expected
    assert changed.stat().st_ino == changed_inode
    assert changed.read_bytes() == b"changed retained generation evidence\n"
    assert not (journal / "rollback-ready").exists()
    assert not (journal / "rollback-prior-build-receipt.json").exists()
    _assert_published(layout, "192.168.50.10")
    _assert_tls_contract(layout, "192.168.50.10")


@pytest.mark.parametrize("mode", ("interrupt", "kill", "fsync"))
def test_tls_review_resumes_interrupted_inert_work_retention(tmp_path: Path, mode: str) -> None:
    layout = _make_layout(tmp_path)
    initial = _run(layout)
    assert initial.returncode == 0, initial.stderr
    expected = _tls_review_public_snapshot(layout)
    _set_fake_ip(layout, "192.168.50.10")
    staged = _run(layout, _tls_pre_ready_kill_environment(tmp_path / "intent-fault", layout, "intent"))
    assert staged.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
    journals = [
        path for path in layout.runtime.glob(".termdeck-tls-transaction-*")
        if (path / "intent.json").is_file() and not (path / "complete").exists()
    ]
    assert len(journals) == 1
    journal = journals[0]
    generation = journal.name.removeprefix(".termdeck-tls-transaction-")
    source = layout.runtime / "tls" / f".rotate.{generation}"
    retained = journal / f"rotation-evidence-{generation}"
    source_inode = source.stat().st_ino
    fault_marker = tmp_path / "inert-retention-fault"
    environment = _hook_environment(tmp_path / "inert-retention-hook", f"""
import errno
import os
import signal
import sys

original_fsync = os.fsync
mode = {mode!r}
source = {str(source)!r}
retained = {str(retained)!r}
fault_marker = {str(fault_marker)!r}
tls = {str(layout.runtime / 'tls')!r}
injected = False

def fsync(descriptor):
    global injected
    result = original_fsync(descriptor)
    try:
        path = os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        path = ""
    if (
        not injected and len(sys.argv) == 6 and sys.argv[1] == "recover"
        and path == tls and os.path.isdir(retained) and not os.path.exists(source)
    ):
        injected = True
        with open(fault_marker, "w", encoding="ascii") as handle:
            handle.write(mode)
        if mode == "interrupt":
            raise KeyboardInterrupt("injected inert retention interruption")
        if mode == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        raise OSError(errno.EIO, "injected inert retention fsync failure")
    return result
os.fsync = fsync
""")

    interrupted = _run(layout, environment)

    assert interrupted.returncode != 0
    assert fault_marker.read_text(encoding="ascii") == mode
    assert _tls_review_public_snapshot(layout) == expected
    assert retained.stat().st_ino == source_inode
    assert not source.exists()
    assert not (journal / "inert").exists()
    recovered = _run(layout)
    assert recovered.returncode == 0, recovered.stderr
    assert (journal / "inert").read_bytes() == b"inert\n"
    assert retained.stat().st_ino == source_inode
    assert not list((layout.runtime / "tls").glob(".rotate.*"))
    _assert_published(layout, "192.168.50.10")
    assert _run(layout).returncode == 0


@pytest.mark.parametrize("phase", ("provisional", "partial-payload", "authenticated"))
@pytest.mark.parametrize("mode", ("interrupt", "kill", "fsync"))
def test_tls_review_resumes_interrupted_restored_receipt_staging(
    tmp_path: Path, phase: str, mode: str,
) -> None:
    layout, generation, journal = _tls_review_rotated_layout(tmp_path)
    fault_marker = tmp_path / "restored-receipt-fault"
    environment = _hook_environment(tmp_path / "restored-receipt-hook", f"""
import errno
import os
import signal
import sys

original_fsync = os.fsync
original_write = os.write
journal = {str(journal)!r}
phase = {phase!r}
mode = {mode!r}
fault_marker = {str(fault_marker)!r}
prefix = "rollback-restored-build-receipt.json.staging-"
canonical = os.path.join(journal, "rollback-restored-build-receipt.json")
injected = False

def descriptor_path(descriptor):
    try:
        return os.readlink(f"/proc/self/fd/{{descriptor}}")
    except OSError:
        return ""

def fault():
    global injected
    injected = True
    with open(fault_marker, "w", encoding="ascii") as handle:
        handle.write(phase + ":" + mode)
    if mode == "interrupt":
        raise KeyboardInterrupt("injected restored receipt interruption")
    if mode == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    raise OSError(errno.EIO, "injected restored receipt fsync failure")

def fsync(descriptor):
    result = original_fsync(descriptor)
    if not injected and len(sys.argv) == 6 and sys.argv[1] == "restore" and descriptor_path(descriptor) == journal:
        staging = [os.path.join(journal, name) for name in os.listdir(journal) if name.startswith(prefix)]
        if phase == "provisional" and len(staging) == 1 and os.path.getsize(staging[0]) == 1:
            fault()
        if phase == "authenticated" and os.path.isfile(canonical):
            fault()
    return result

def write(descriptor, content):
    path = descriptor_path(descriptor)
    if (
        not injected and len(sys.argv) == 6 and sys.argv[1] == "restore"
        and phase == "partial-payload" and os.path.dirname(path) == journal
        and os.path.basename(path).startswith(prefix) and len(content) > 64
    ):
        original_write(descriptor, content[:37])
        original_fsync(descriptor)
        fault()
    return original_write(descriptor, content)

os.fsync = fsync
os.write = write
""")

    interrupted = _run(layout, environment, arguments=["--restore-tls-generation", generation])

    assert interrupted.returncode != 0
    assert fault_marker.read_text(encoding="ascii") == f"{phase}:{mode}"
    assert not (layout.runtime / "build-receipt.json").exists()
    incomplete = {
        path: (path.stat().st_ino, path.read_bytes())
        for path in journal.glob("rollback-restored-build-receipt.json.staging-*")
    }
    assert len(incomplete) == (0 if phase == "authenticated" else 1)
    if phase == "provisional":
        assert next(iter(incomplete.values()))[1] == b"\n"
    if phase == "partial-payload":
        assert len(next(iter(incomplete.values()))[1]) == 37

    recovered = _run(layout, arguments=["--restore-tls-generation", generation])

    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert result["action"] == "restored-runtime-generation"
    assert result["ok"] is True
    _assert_published(layout, LAN_IP)
    _assert_tls_contract(layout, LAN_IP)
    for path, expected in incomplete.items():
        assert (path.stat().st_ino, path.read_bytes()) == expected
    retried = _run(layout, arguments=["--restore-tls-generation", generation])
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == result



def _tls_review_restore_source_copies(layout: RuntimeLayout, journal: Path) -> dict[str, int]:
    prior_receipt = json.loads((journal / "prior-build-receipt.json").read_text(encoding="utf-8"))
    restored = {}
    for name in ("bin/termdeck-web-server", "nginx/nginx.conf"):
        source = layout.runtime / name
        content = source.read_bytes()
        assert hashlib.sha256(content).hexdigest() == prior_receipt["files"][name]
        old_inode = source.stat().st_ino
        mode = 0o755 if name == "bin/termdeck-web-server" else 0o644
        replacement = layout.root / ("restored-" + source.name)
        replacement.write_bytes(content)
        replacement.chmod(mode)
        os.replace(replacement, source)
        restored[name] = source.stat().st_ino
        assert restored[name] != old_inode
    return restored


def test_tls_review_inverse_accepts_authenticated_a_source_copies_on_new_inodes(tmp_path: Path) -> None:
    layout, generation, journal = _tls_review_rotated_layout(tmp_path)
    b_receipt = (layout.runtime / "build-receipt.json").read_bytes()
    source_inodes = _tls_review_restore_source_copies(layout, journal)

    restored = _run(layout, arguments=["--restore-tls-generation", generation])

    assert restored.returncode == 0, restored.stderr
    _assert_published(layout, LAN_IP)
    assert (journal / "rollback-prior-build-receipt.json").read_bytes() == b_receipt
    receipt = json.loads((layout.runtime / "build-receipt.json").read_text(encoding="utf-8"))
    for name, inode in source_inodes.items():
        assert (layout.runtime / name).stat().st_ino == inode
        assert receipt["files"][name] == hashlib.sha256(
            (layout.runtime / name).read_bytes()
        ).hexdigest()
    assert "producer_attestation" in receipt
    assert "producer_commitment" not in receipt
    retried = _run(layout, arguments=["--restore-tls-generation", generation])
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == json.loads(restored.stdout)


@pytest.mark.parametrize("source_name,change", (
    ("bin/termdeck-web-server", "bytes"),
    ("nginx/nginx.conf", "bytes"),
    ("bin/termdeck-web-server", "mode"),
    ("nginx/nginx.conf", "mode"),
))
def test_tls_review_inverse_rejects_sources_outside_authenticated_a_contract(
    tmp_path: Path, source_name: str, change: str,
) -> None:
    layout, generation, journal = _tls_review_rotated_layout(tmp_path)
    _tls_review_restore_source_copies(layout, journal)
    source = layout.runtime / source_name
    if change == "bytes":
        source.write_bytes(b"unrelated source bytes\n")
    else:
        source.chmod(0o640)
    expected = _tls_review_public_snapshot(layout)
    source_before = (source.stat().st_ino, source.stat().st_mode, source.read_bytes())

    rejected = _run(layout, arguments=["--restore-tls-generation", generation])

    assert rejected.returncode != 0
    assert "does not match authenticated A bytes and mode" in rejected.stderr
    assert _tls_review_public_snapshot(layout) == expected
    assert (source.stat().st_ino, source.stat().st_mode, source.read_bytes()) == source_before
    assert not (journal / "rollback-ready").exists()
    assert not (journal / "rollback-prior-build-receipt.json").exists()
