"""Exercise the installed executable protocol with real, separate principals."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]

# The fixed preparer is a fixture here: cryptographic signing is qualified by
# the attestation suites. The broker, publisher, socket transport, owner/mode
# checks, descriptor execution, and UID/GID transitions below are real.
PROBE = r'''
import hashlib, importlib.machinery, importlib.util, json, os, shutil, socket, stat, subprocess, sys, threading
from pathlib import Path

def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module

broker = load('broker', '/fixture/broker')
publisher = load('publisher', '/fixture/publication')
libexec = Path('/tmp/libexec')
libexec.mkdir(mode=0o755)
for name, source in [('termdeck-preparer-broker', '/fixture/broker'),
                     ('termdeck-preparer-publication', '/fixture/publication')]:
    shutil.copyfile(source, libexec / name)
    (libexec / name).chmod(0o755)
preparer = libexec / 'termdeck-web-prepare-runtime'
preparer.write_text("""#!/usr/bin/python3 -I
import json, os, sys
assert os.getresuid() == (101, 101, 101)
assert os.getresgid() == (101, 101, 101)
assert os.getgroups() == []
capabilities = {key:value.strip() for key,value in (line.split(':',1) for line in open('/proc/self/status'))
                if key in ('CapEff','CapPrm','CapInh','CapAmb','NoNewPrivs')}
assert all(int(capabilities[key],16) == 0 for key in ('CapEff','CapPrm','CapInh','CapAmb'))
assert capabilities['NoNewPrivs'] == '1'
assert os.environ.get('TERMDECK_PREPARER_BROKER') == '1'
assert 'BROKER_PARENT_SECRET' not in os.environ
print('fixture-child-stderr-private-token', file=sys.stderr)
if '--verify-existing' in sys.argv:
    sys.stdout.buffer.write(open(os.environ['TERMDECK_RUNTIME_ROOT'] + '/build-receipt.json', 'rb').read())
elif '--restore-tls-generation' in sys.argv:
    sys.stdout.buffer.write(open(os.environ['TERMDECK_RUNTIME_ROOT'] + '/inverse.fixture', 'rb').read())
else:
    print('fixture-child-stdout-private-token')
""")
preparer.chmod(0o755)
os.environ['BROKER_PARENT_SECRET'] = 'fixture-parent-private-token'

def mkdir(path, uid, mode=0o700):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    os.chown(path, uid, uid)

producer = Path('/tmp/producer/runtime')
consumer = Path('/tmp/consumer')
mkdir(producer.parent, 101)
for root, uid in [(producer, 101), (consumer, 202)]:
    mkdir(root, uid)
    for name in ('bin', 'nginx', 'tls'):
        mkdir(root / name, uid)

def write(path, content, mode, uid):
    path.write_bytes(content)
    path.chmod(mode)
    os.chown(path, uid, uid)

def identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]

def claim(path, relative, include_path=True):
    value = {'identity': identity(path), 'mode': stat.S_IMODE(path.stat().st_mode),
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    if include_path:
        value['path'] = relative
    return value

for root, uid in [(producer, 101), (consumer, 202)]:
    write(root / 'bin/termdeck-web-server', b'fixture-server', 0o755, uid)
    write(root / 'nginx/nginx.conf', b'fixture-nginx', 0o600, uid)
    if root == consumer:
        write(root / publisher.CONTROL_KEY, b'k' * 32, 0o600, uid)
    for name, mode in publisher.TLS_REQUIRED_MODES.items():
        write(root / 'tls' / name, b'fixture-tls-' + name.encode(), mode, uid)
write(producer / 'current-lan-ip', b'192.168.50.9\n', 0o600, 101)
write(producer / 'nginx/allowed-hosts.map', b'192.168.50.9 1;\n', 0o600, 101)
generation = 'a' * 32
tls_stat = (producer / 'tls').stat()
receipt = {
    'schema': 'termdeck.lan-runtime-build.v2', 'ok': True,
    'files': {name: hashlib.sha256((producer / name).read_bytes()).hexdigest()
              for name in ('bin/termdeck-web-server', 'nginx/nginx.conf',
                           'tls/ca.crt', 'tls/server.crt', 'tls/server-chain.crt')},
    'runtime_publication': {'generation': generation, 'outputs': {
        name: claim(producer / name, name) for name in publisher.OUTPUT_MODES}},
    'tls_publication': {'generation': 'b' * 32, 'prior': None, 'current': {
        'path': 'tls', 'identity': [tls_stat.st_dev, tls_stat.st_ino, 0o700, 101],
        'manifest': {name: claim(producer / 'tls' / name, name, False)
                     for name in publisher.TLS_REQUIRED_MODES}}},
}
write(producer / 'build-receipt.json', broker._canonical_json(receipt) + b'\n', 0o644, 101)
config = broker.BrokerConfig(
    broker_path=libexec / 'termdeck-preparer-broker', preparer_path=preparer,
    publication_path=libexec / 'termdeck-preparer-publication',
    runtime_root=producer, consumer_root=consumer, socket_path=Path('/tmp/broker.sock'),
)
principals = broker.Principals(101, 101, 202, 202)

CLIENT = """
import json, socket, struct, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect('/tmp/broker.sock')
    value = {'operation':'prepare','restore':{'generation':sys.argv[2] or None,'mode':sys.argv[1]},
             'schema':'termdeck.preparer-broker-request.v1'}
    payload = json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    client.sendall(struct.pack('!I', len(payload)) + payload)
    def exact(size):
        data = b''
        while len(data) < size:
            block = client.recv(size - len(data))
            assert block
            data += block
        return data
    size = struct.unpack('!I', exact(4))[0]
    sys.stdout.buffer.write(exact(size))
"""

def exchange(mode, generation=''):
    outcome, failures = [], []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(config.socket_path))
        os.chmod(config.socket_path, 0o600)
        os.chown(config.socket_path, 202, 202)
        listener.listen(1)
        def serve():
            try:
                outcome.append(broker.serve_once(listener, config=config,
                    resolve_principals=lambda _config: principals))
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=serve)
        worker.start()
        result = subprocess.run(['/usr/bin/python3', '-I', '-c', CLIENT, mode, generation],
            user=202, group=202, extra_groups=(), capture_output=True, timeout=20)
        worker.join(20)
        assert not worker.is_alive()
        assert not failures, failures
        assert result.returncode == 0, result.stderr.decode()
        response = json.loads(result.stdout)
    config.socket_path.unlink()
    return response, outcome

prepared, outcome = exchange('none')
assert outcome == [0], prepared
assert prepared['ok'] is True
assert prepared['result']['operation'] == 'publish'
assert prepared['result']['schema'] == publisher.SCHEMA
verified, outcome = exchange('verify')
assert outcome == [0], verified
result = verified['result']
assert set(result) == {'identity', 'receipt', 'sha256', 'publication'}
assert result['receipt'] == receipt
assert result['identity'] == identity(consumer / 'build-receipt.json')
assert result['identity'] != identity(producer / 'build-receipt.json')
assert result['sha256'] == hashlib.sha256((consumer / 'build-receipt.json').read_bytes()).hexdigest()
for name in publisher.OUTPUT_MODES:
    assert result['publication']['outputs'][name]['identity'] == identity(consumer / name)
    assert result['publication']['outputs'][name]['identity'] != receipt['runtime_publication']['outputs'][name]['identity']
assert result['publication']['tls']['identity'][3] == 202
assert receipt['tls_publication']['current']['identity'][3] == 101

inverse = {
    'ok':True, 'action':'restored-runtime-generation', 'generation':'b' * 32,
    'receipt':{'path':'build-receipt.json', 'identity':identity(producer / 'build-receipt.json'),
               'sha256':hashlib.sha256((producer / 'build-receipt.json').read_bytes()).hexdigest()},
    'restored':{'outputs':{
        name:{key: value[key] for key in ('identity', 'sha256')}
        for name,value in receipt['runtime_publication']['outputs'].items()},
        'runtime_generation':generation, 'tls':receipt['tls_publication']['current']},
    'retained':{'fixture':'unchanged producer lineage'},
}
write(producer / 'inverse.fixture', broker._canonical_json(inverse), 0o600, 101)
restored, outcome = exchange('generation', 'b' * 32)
assert outcome == [0], restored
assert set(restored['result']) == {'producer', 'publication'}
assert restored['result']['producer'] == inverse
assert restored['result']['publication']['receipt']['identity'] == identity(consumer / 'build-receipt.json')
assert restored['result']['publication']['receipt']['identity'] != inverse['receipt']['identity']

inverse = {'ok':True, 'action':'restored-tls-generation', 'generation':'b' * 32,
           'receipt':'absent', 'restored':receipt['tls_publication']['current'],
           'retained':{'fixture':'unchanged producer lineage'}}
write(producer / 'inverse.fixture', broker._canonical_json(inverse), 0o600, 101)
(producer / 'build-receipt.json').unlink()
restored_tls, outcome = exchange('generation', 'b' * 32)
assert outcome == [0], restored_tls
assert restored_tls['result']['producer'] == inverse
assert restored_tls['result']['publication'] == inverse
assert not (consumer / 'build-receipt.json').exists()
for name in publisher.TLS_REQUIRED_MODES:
    assert (consumer / 'tls' / name).read_bytes() == (producer / 'tls' / name).read_bytes()

# A real helper refusal must yield bounded phase/category diagnostics, with
# neither child stderr nor the fixture token in a malformed consumer source.
write(consumer / 'nginx/nginx.conf', b'fixture-consumer-private-token', 0o644, 202)
refused, outcome = exchange('verify')
assert outcome == [1]
assert refused == {'error':'request-refused','ok':False,'returncode':None,'schema':broker.RESPONSE_SCHEMA}
print(json.dumps({'ok':True, 'distinct_principals':True, 'descriptor_execution':True,
                  'prepare':prepared['result']['operation'], 'verify':sorted(result),
                  'restore':['restored-runtime-generation', 'restored-tls-generation'],
                  'consumer_inodes':True, 'refusal':'publication-seed'}))
'''


def test_actual_subprocess_and_publication_wire_with_separate_principals(tmp_path: Path):
    """No root host action: multiple IDs exist only in this user namespace."""

    for command in ("unshare", "bwrap", "newuidmap", "newgidmap"):
        if shutil.which(command) is None:
            pytest.skip(f"separate-principal fixture requires {command}")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    command = [
        "unshare", "--user", "--map-root-user", "--map-auto", "--setgroups", "allow", "--",
        "bwrap", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", "/usr/bin",
        "--cap-add", "CAP_CHOWN", "--cap-add", "CAP_DAC_OVERRIDE",
        "--cap-add", "CAP_SETUID", "--cap-add", "CAP_SETGID",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
        "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", str(ROOT / "deploy/termdeck-preparer-broker"), "/fixture/broker",
        "--ro-bind", str(ROOT / "deploy/termdeck-preparer-publication"), "/fixture/publication",
        "--ro-bind", str(probe), "/fixture/probe.py",
        "--chdir", "/tmp", "/usr/bin/python3", "-I", "/fixture/probe.py",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["consumer_inodes"] is True
    assert result.stderr == (
        "TermDeck preparer broker refused: phase=publication-seed category=child-refused\n"
    )
    assert "private-token" not in result.stdout + result.stderr
