"""Qualify the real preparation, signing and publication boundary together."""

from __future__ import annotations

import _ctypes
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from test_termdeck_preparer_execution_policy import SERVICE_POLICY
from test_termdeck_web_prepare_runtime import _make_layout, _run


ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import hashlib, importlib.machinery, importlib.util, json, os, runpy, shutil, socket, stat, subprocess, sys, threading, time
from pathlib import Path

loader = importlib.machinery.SourceFileLoader('broker', '/usr/libexec/termdeck-preparer-broker')
spec = importlib.util.spec_from_loader('broker', loader)
broker = importlib.util.module_from_spec(spec)
sys.modules['broker'] = broker
loader.exec_module(broker)
bootstrap = runpy.run_path('/usr/libexec/termdeck-preparer-state')
modules = bootstrap['_load_private_package']()
attest = modules['termdeck.runtime_attestation']
config = broker.BrokerConfig()
policy_proc_mount()
capability_transitions = []
prepare_capabilities = broker._prepare_execution_capabilities
def observe_capabilities(config):
    before = policy_state()
    prepare_capabilities(config)
    after = policy_state()
    assert all(int(before[key],16) == 0xe3 for key in ('CapEff','CapPrm','CapBnd'))
    assert int(before['CapInh'],16) == int(before['CapAmb'],16) == 0x80
    assert all(int(after[key],16) == 0xe3 for key in ('CapEff','CapPrm','CapBnd'))
    assert int(after['CapInh'],16) == int(after['CapAmb'],16) == 0
    assert after['NoNewPrivs'] == '1' and after['Seccomp'] == '2'
    capability_transitions.append({'before':before,'after':after})
broker._prepare_execution_capabilities = observe_capabilities

def directory(path, uid=0, gid=0, mode=0o755):
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir()
            parent.chmod(0o755)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    os.chown(path, uid, gid)

Path('/etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\ntermdeck-preparer:x:101:101:preparer:/nonexistent:/bin/false\nmax:x:202:202:max:/nonexistent:/bin/false\n')
Path('/etc/group').write_text('root:x:0:\ntermdeck-preparer:x:101:\nmax:x:202:\n')
Path('/etc/passwd').chmod(0o644)
Path('/etc/group').chmod(0o644)
directory(Path('/tmp'), mode=0o1777)
directory(Path('/etc/ssl'))
directory(Path('/etc/termdeck'))
directory(Path('/etc/termdeck/control'), gid=101, mode=0o710)
directory(Path('/etc/termdeck/control/records'), mode=0o700)
directory(config.runtime_root.parent, 101, 101, 0o700)
directory(config.runtime_root, 101, 101, 0o700)
directory(config.consumer_root, 202, 202, 0o700)
directory(config.socket_path.parent, mode=0o711)
for relative in ('bin', 'nginx', 'tls'):
    directory(config.consumer_root / relative, 202, 202, 0o700)
sources = {'bin/termdeck-web-server': 0o755, 'nginx/nginx.conf': 0o600}
tls_modes = {'ca.crt':0o644, 'ca.key':0o600, 'ca.srl':0o644, 'server.key':0o600,
             'server.crt':0o644, 'server-chain.crt':0o644, 'server.ext':0o600}
sources.update({'tls/' + name:mode for name,mode in tls_modes.items()})
for relative, mode in sources.items():
    target = config.consumer_root / relative
    shutil.copyfile(Path('/initial') / relative, target)
    target.chmod(mode)
    os.chown(target, 202, 202)

# Only this isolated network namespace is configured; no host traffic leaves it.
for args in (['link','set','lo','up'], ['address','add','192.168.50.9/24','dev','lo'],
             ['route','add','default','dev','lo']):
    subprocess.run(['/usr/bin/ip', *args], check=True, capture_output=True)
subprocess.run(['/usr/libexec/termdeck-runtime-attestd','keygen'], check=True, capture_output=True)
daemon = subprocess.Popen(['/usr/libexec/termdeck-runtime-attestd','daemon'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

CLIENT = """
import json, socket, struct, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect('/run/termdeck-preparer-broker/request.sock')
    request = {'operation':'prepare','restore':{'generation':sys.argv[2] or None,'mode':sys.argv[1]},
               'schema':'termdeck.preparer-broker-request.v1'}
    payload = json.dumps(request,sort_keys=True,separators=(',',':')).encode()
    client.sendall(struct.pack('!I',len(payload)) + payload)
    def exact(size):
        data = b''
        while len(data) < size:
            block = client.recv(size-len(data))
            assert block
            data += block
        return data
    size = struct.unpack('!I',exact(4))[0]
    sys.stdout.buffer.write(exact(size))
"""

def exchange(mode, generation=None):
    outcomes, failures = [], []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(config.socket_path))
        os.chmod(config.socket_path, 0o600)
        os.chown(config.socket_path, 202, 202)
        listener.listen(1)
        def serve():
            try:
                install_service_policy()
                outcomes.append(broker.serve_once(listener, config=config))
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=serve)
        worker.start()
        response = subprocess.run(['/usr/bin/python3','-I','-c',CLIENT,mode,generation or ''],
            user=202, group=202, extra_groups=(), capture_output=True, timeout=90)
        worker.join(5)
        assert not worker.is_alive()
        assert not failures, failures
        assert response.returncode == 0, response.stderr.decode()
        value = json.loads(response.stdout)
        assert outcomes == [0] and value['ok'] is True, value
    config.socket_path.unlink()
    return value['result'], len(response.stdout)

try:
    for _ in range(100):
        if Path('/etc/termdeck/control/runtime-attestd.sock').exists():
            break
        if daemon.poll() is not None:
            raise AssertionError(daemon.communicate())
        time.sleep(0.02)
    else:
        raise AssertionError('disposable attestation listener did not appear')
    prepared, _ = exchange('none')
    assert prepared['operation'] == 'publish'
    verified, frame_bytes = exchange('verify')
    receipt = verified['receipt']
    attestation_config = attest.production_config()
    attest.verify_runtime_build_attestation(receipt, config=attestation_config)
    assert receipt['producer_attestation']['attestation']['algorithm'] == 'ed25519-openssl-pkeyutl.v1'
    assert receipt['producer_attestation']['receipt_path'] == str(config.runtime_root / 'build-receipt.json')
    assert receipt['tls_publication']['current']['identity'][3] == 101
    assert verified['publication']['tls']['identity'][3] == 202
    for relative in ('build-receipt.json','current-lan-ip','nginx/allowed-hosts.map', *['tls/' + name for name in tls_modes]):
        producer = config.runtime_root / relative
        consumer = config.consumer_root / relative
        assert producer.read_bytes() == consumer.read_bytes(), relative
        assert producer.stat().st_uid == 101 and consumer.stat().st_uid == 202
        assert producer.stat().st_ino != consumer.stat().st_ino
    assert verified['sha256'] == hashlib.sha256((config.consumer_root / 'build-receipt.json').read_bytes()).hexdigest()
    first_generation = receipt['tls_publication']['generation']
    initial_tls = {name:(Path('/initial/tls') / name).read_bytes() for name in tls_modes}
    a_tls = {name:(config.consumer_root / 'tls' / name).read_bytes() for name in tls_modes}
    a_outputs = {name:(config.consumer_root / name).read_bytes()
                 for name in ('current-lan-ip','nginx/allowed-hosts.map')}
    for args in (['address','del','192.168.50.9/24','dev','lo'],
                 ['address','add','192.168.50.10/24','dev','lo']):
        subprocess.run(['/usr/bin/ip', *args], check=True, capture_output=True)
    exchange('none')
    b_verified, b_frame_bytes = exchange('verify')
    b_receipt = b_verified['receipt']
    assert b_receipt['lan_ip'] == '192.168.50.10'
    assert b_receipt['tls_publication']['generation'] != b_receipt['runtime_publication']['generation']
    # Activation restores retained source copies on new inodes before its
    # inverse request. Both publication principals keep their required modes.
    restored_source_inodes = {}
    for root, uid in ((config.runtime_root,101),(config.consumer_root,202)):
        for relative, mode in (('bin/termdeck-web-server',0o755),('nginx/nginx.conf',0o600)):
            source = root / relative
            previous = source.stat().st_ino
            replacement = source.with_name(source.name + '.restored-copy')
            replacement.write_bytes(source.read_bytes())
            replacement.chmod(mode)
            os.chown(replacement, uid, uid)
            replacement.replace(source)
            assert source.stat().st_ino != previous
            if uid == 101:
                restored_source_inodes[relative] = source.stat().st_ino
    inverse, _ = exchange('generation',b_receipt['tls_publication']['generation'])
    assert inverse['producer']['action'] == 'restored-runtime-generation'
    restored, restored_frame_bytes = exchange('verify')
    attest.verify_runtime_build_attestation(restored['receipt'], config=attestation_config)
    assert restored['receipt']['lan_ip'] == '192.168.50.9'
    for relative, inode in restored_source_inodes.items():
        assert (config.runtime_root / relative).stat().st_ino == inode
    for root in (config.runtime_root,config.consumer_root):
        for name, content in a_tls.items():
            assert (root / 'tls' / name).read_bytes() == content
        for name, content in a_outputs.items():
            assert (root / name).read_bytes() == content
    replay, _ = exchange('generation',b_receipt['tls_publication']['generation'])
    assert replay['producer'] == inverse['producer']
    replay_verified, _ = exchange('verify')
    assert replay_verified['sha256'] == restored['sha256']
    legacy_inverse, _ = exchange('generation',first_generation)
    assert legacy_inverse['producer']['action'] == 'restored-tls-generation'
    assert legacy_inverse['producer']['receipt'] == 'absent'
    legacy_replay, _ = exchange('generation',first_generation)
    assert legacy_replay['producer'] == legacy_inverse['producer']
    for root in (config.runtime_root,config.consumer_root):
        assert not (root / 'build-receipt.json').exists()
        for name, content in initial_tls.items():
            assert (root / 'tls' / name).read_bytes() == content
    records = list(Path('/etc/termdeck/control/records').glob('*.json'))
    assert len(records) >= 3, 'real attestation service did not record all signed generations'
    denied = subprocess.run(['/usr/bin/python3','-I','-c',
        "from pathlib import Path; Path('/etc/termdeck/control/runtime-attestd.key').read_bytes()"],
        user=202, group=202, extra_groups=(), capture_output=True)
    assert denied.returncode != 0 and b'PermissionError' in denied.stderr
    print(json.dumps({'ok':True,'algorithm':'ed25519-openssl-pkeyutl.v1',
                      'producer_uid':101,'consumer_uid':202,'signer_uid':0,
                      'frame_bytes':max(frame_bytes,b_frame_bytes,restored_frame_bytes),
                      'records':len(records),'full_inverse_replay':True,'tls_inverse_replay':True,
                      'capability_policy':capability_transitions[0],
                      'policy_qualified_requests':len(capability_transitions)}))
finally:
    daemon.terminate()
    daemon.wait(timeout=10)
'''


def test_real_preparer_signer_and_publisher_use_separate_principals(tmp_path: Path):
    for executable in ("unshare", "bwrap", "newuidmap", "newgidmap", "openssl", "ip"):
        if shutil.which(executable) is None:
            pytest.skip(f"signed boundary qualification requires {executable}")
    initial = _make_layout(tmp_path / "initial-layout")
    initial_result = _run(initial)
    assert initial_result.returncode == 0, initial_result.stderr
    package = tmp_path / "package"
    manifest = json.loads((ROOT / "deploy/preparer-install-manifest.json").read_text())
    for entry in manifest["entries"]:
        destination = package / entry["destination"].lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry["source"], destination)
        destination.chmod(int(entry["mode"], 8))
    for directory_path in package.rglob("*"):
        if directory_path.is_dir():
            directory_path.chmod(0o755)
    probe = tmp_path / "probe.py"
    probe.write_text(SERVICE_POLICY + PROBE)
    python_library = f"python{sys.version_info.major}.{sys.version_info.minor}"
    command = [
        "unshare", "--user", "--map-root-user", "--map-auto", "--setgroups", "allow", "--",
        "bwrap", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin",
        "--cap-drop", "ALL",
        "--cap-add", "CAP_CHOWN", "--cap-add", "CAP_DAC_OVERRIDE", "--cap-add", "CAP_NET_ADMIN",
        "--cap-add", "CAP_SETUID", "--cap-add", "CAP_SETGID",
        "--cap-add", "CAP_KILL", "--cap-add", "CAP_SETPCAP", "--cap-add", "CAP_SYS_ADMIN",
        "--ro-bind", "/usr", "/host-usr", "--dir", "/usr",
        "--symlink", "/host-usr/bin", "/usr/bin", "--dir", "/usr/lib",
        "--symlink", f"/host-usr/lib/{python_library}", f"/usr/lib/{python_library}",
        "--symlink", "/host-usr/lib/systemd", "/usr/lib/systemd",
        "--symlink", "/host-usr/lib", "/lib", "--symlink", "/host-usr/lib", "/lib64",
        "--symlink", "/usr/bin", "/bin", "--tmpfs", "/tmp", "--tmpfs", "/etc",
        "--ro-bind", "/etc/ssl/openssl.cnf", "/etc/ssl/openssl.cnf",
        "--tmpfs", "/var", "--tmpfs", "/run", "--tmpfs", "/warp", "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", str(package / "usr/libexec"), "/usr/libexec",
        "--ro-bind", str(package / "usr/lib/termdeck-preparer"), "/usr/lib/termdeck-preparer",
        "--ro-bind", str(initial.runtime), "/initial", "--ro-bind", str(probe), "/probe.py",
        "--setenv", "LD_LIBRARY_PATH", "/host-usr/lib",
    ]
    libraries: set[str] = {"/usr/lib/libseccomp.so.2"}
    systemd_libraries = list(Path('/usr/lib/systemd').glob('libsystemd-shared-*.so'))
    assert len(systemd_libraries) == 1
    for executable in ("/usr/bin/python3", "/usr/bin/openssl", "/usr/bin/ip", "/usr/bin/bash", "/usr/bin/awk", _ctypes.__file__, str(systemd_libraries[0])):
        linked = subprocess.run(["ldd", executable], capture_output=True, text=True, check=True)
        libraries.update(re.findall(r"=> (/usr/lib/\S+)", linked.stdout))
    for library in sorted(libraries):
        command.extend(("--symlink", "/host-usr/lib/" + Path(library).name, library))
    command.extend(("--chdir", "/tmp", "/usr/bin/python3", "-I", "/probe.py"))
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["producer_uid"] != report["consumer_uid"] != report["signer_uid"]
    assert report["frame_bytes"] < 128 * 1024
    assert report["full_inverse_replay"] is True and report["tls_inverse_replay"] is True
    assert report["policy_qualified_requests"] >= 10
    print(json.dumps(report, sort_keys=True))
