"""Reproduce the service capability transition without a host root action."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]

# The real boundary fixture also installs these restrictions in its broker
# thread. Its fixture supervisor keeps namespace setup privileges, while every
# broker subprocess inherits only the exact service policy installed here.
SERVICE_POLICY = r'''
import ctypes, glob, os

def policy_state():
    return {key:value.strip() for key,value in
            (line.split(':',1) for line in open('/proc/thread-self/status'))
            if key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb','NoNewPrivs','Seccomp','Seccomp_filters')}

def policy_proc_mount():
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.mount(b'proc',b'/proc',b'proc',14,b'hidepid=2,subset=pid') == 0, ctypes.get_errno()

def install_service_policy(*, startup_ambient=True):
    libc = ctypes.CDLL(None, use_errno=True)
    class Header(ctypes.Structure):
        _fields_ = [('version',ctypes.c_uint32),('pid',ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    allowed = {0,1,5,6,7}
    for capability in range(41):
        if capability not in allowed:
            assert libc.prctl(24,capability,0,0,0) == 0, (capability,ctypes.get_errno())
    assert libc.prctl(47,4,0,0,0) == 0
    # v262 exec-invoke.c drops SETUID during seccomp setup unless ambient.
    mask = 0xe3 if startup_ambient else 0x63
    inherited = 0x80 if startup_ambient else 0
    assert libc.capset(ctypes.byref(Header(0x20080522,0)),
                       (Data*2)(Data(mask,mask,inherited),Data())) == 0
    if startup_ambient:
        assert libc.prctl(47,2,7,0,0) == 0
    assert libc.prctl(38,1,0,0,0) == 0
    libraries = glob.glob('/usr/lib/systemd/libsystemd-shared-*.so')
    assert len(libraries) == 1
    shared = ctypes.CDLL(libraries[0], use_errno=True)
    shared.set_new.argtypes = [ctypes.c_void_p]
    shared.set_new.restype = ctypes.c_void_p
    shared.set_put.argtypes = [ctypes.c_void_p,ctypes.c_void_p]
    shared.syscall_filter_set_find.argtypes = [ctypes.c_char_p]
    shared.syscall_filter_set_find.restype = ctypes.c_void_p
    shared.seccomp_load_syscall_filter_set.argtypes = [ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_bool]
    shared.seccomp_restrict_address_families.argtypes = [ctypes.c_void_p,ctypes.c_bool]
    shared.seccomp_restrict_archs.argtypes = [ctypes.c_void_p]
    def integer_set(values):
        result = shared.set_new(None)
        assert result
        for value in values:
            assert shared.set_put(result,ctypes.c_void_p(value)) >= 0
        return result
    def deny_group(name):
        group = shared.syscall_filter_set_find(name)
        assert group and shared.seccomp_load_syscall_filter_set(0x7fff0000,group,0x50001,False) >= 0
    assert shared.seccomp_restrict_address_families(integer_set((1,16)),True) >= 0
    assert shared.seccomp_memory_deny_write_execute() >= 0
    assert shared.seccomp_restrict_realtime_full(1) >= 0
    assert shared.seccomp_restrict_suid_sgid() >= 0
    assert shared.seccomp_restrict_namespaces(0) >= 0
    assert shared.seccomp_protect_sysctl() >= 0
    deny_group(b'@module')
    assert shared.seccomp_protect_syslog() >= 0
    deny_group(b'@clock')
    deny_group(b'@raw-io')
    assert shared.seccomp_protect_hostname() >= 0
    seccomp = ctypes.CDLL('/usr/lib/libseccomp.so.2')
    seccomp.seccomp_arch_native.restype = ctypes.c_uint32
    assert shared.seccomp_restrict_archs(integer_set((seccomp.seccomp_arch_native(),))) >= 0
    assert shared.seccomp_lock_personality(0) >= 0
    assert shared.seccomp_load_syscall_filter_set(0x50001,shared.syscall_filter_set_find(b'@system-service'),0x7fff0000,False) >= 0
    return policy_state()
'''


def _command(probe: Path) -> list[str]:
    command = [
        'unshare','--user','--map-root-user','--map-auto','--setgroups','allow','--',
        'bwrap','--unshare-pid','--unshare-net','--die-with-parent','--cap-drop','ALL',
    ]
    for name in ('CHOWN','DAC_OVERRIDE','KILL','SETUID','SETGID','SETPCAP','SYS_ADMIN'):
        command.extend(('--cap-add','CAP_' + name))
    command.extend((
        '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin',
        '--symlink','usr/lib','/lib','--symlink','usr/lib','/lib64',
        '--tmpfs','/tmp','--proc','/proc','--dev','/dev',
        '--ro-bind',str(ROOT / 'deploy/termdeck-preparer-broker'),'/broker',
        '--ro-bind',str(probe),'/probe.py','--chdir','/tmp',
        '/usr/bin/python3','-I','/probe.py',
    ))
    return command


@pytest.mark.parametrize('startup_ambient', (False, True), ids=('missing-setuid','retained-setuid'))
def test_exact_service_capabilities_fail_before_seed_or_launch_without_child_capabilities(
    tmp_path: Path, startup_ambient: bool,
) -> None:
    for name in ('unshare','bwrap','newuidmap','newgidmap'):
        if shutil.which(name) is None:
            pytest.skip(f'execution policy qualification requires {name}')
    probe = tmp_path / 'probe.py'
    probe.write_text(SERVICE_POLICY + r'''
import errno, importlib.machinery, importlib.util, json, subprocess, sys
from pathlib import Path
loader = importlib.machinery.SourceFileLoader('broker','/broker')
spec = importlib.util.spec_from_loader('broker',loader)
broker = importlib.util.module_from_spec(spec)
sys.modules['broker'] = broker
loader.exec_module(broker)
policy_proc_mount()
fixed = Path('/tmp/fixed')
fixed.write_text('#!/usr/bin/python3 -I\nimport json,os\nprint(json.dumps({line.split(":",1)[0]:line.split(":",1)[1].strip() for line in open("/proc/self/status") if line.startswith(("Cap","NoNewPrivs","Uid:","Gid:"))}))\n')
fixed.chmod(0o755)
descriptor = os.open(fixed,os.O_RDONLY | os.O_NOFOLLOW)
''' + f'ambient = {startup_ambient!r}\n' + r'''
before = install_service_policy(startup_ambient=ambient)
config = broker.BrokerConfig()
if not ambient:
    broker.run_publication = lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('seed ran'))
    try:
        broker.serve_once(None,config=config,resolve_principals=lambda _:broker.Principals(929,929,202,202))
    except broker.BrokerError as error:
        assert error.category == 'missing-setuid-capability'
    else:
        raise AssertionError('missing SETUID reached request handling')
    try:
        subprocess.run([f'/proc/self/fd/{descriptor}'],user=929,group=929,extra_groups=(),
                       pass_fds=(descriptor,),cwd='/',start_new_session=True,umask=0o077,
                       stdin=subprocess.DEVNULL,capture_output=True,timeout=10)
    except PermissionError as error:
        assert error.errno == errno.EPERM and error.filename is None
    else:
        raise AssertionError('missing SETUID unexpectedly launched child')
    print(json.dumps({'before':before,'preseed_refused':True,'credential_setup_errno':'EPERM'}))
else:
    broker._prepare_execution_capabilities(config)
    after = policy_state()
    assert all(int(after[key],16) == 0xe3 for key in ('CapEff','CapPrm','CapBnd'))
    assert int(after['CapInh'],16) == int(after['CapAmb'],16) == 0
    child = subprocess.run([f'/proc/self/fd/{descriptor}'],user=929,group=929,extra_groups=(),
                           pass_fds=(descriptor,),cwd='/',start_new_session=True,umask=0o077,
                           stdin=subprocess.DEVNULL,capture_output=True,timeout=10,check=True)
    observed = json.loads(child.stdout)
    assert all(int(observed[key],16) == 0 for key in ('CapEff','CapPrm','CapInh','CapAmb'))
    assert int(observed['CapBnd'],16) == 0xe3 and observed['NoNewPrivs'] == '1'
    assert observed['Uid'].split() == observed['Gid'].split() == ['929'] * 4
    print(json.dumps({'before':before,'after':after,'child':observed}))
''')
    result = subprocess.run(_command(probe), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['before']['NoNewPrivs'] == '1'
    assert report['before']['Seccomp'] == '2'
    assert int(report['before']['CapBnd'],16) == 0xe3
    assert int(report['before']['CapEff'],16) == (0xe3 if startup_ambient else 0x63)
    print(json.dumps(report, sort_keys=True))
