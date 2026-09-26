"""Authenticate the captured copied deployment, rather than a re-rendered A."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from termdeck import release_manager as manager
from termdeck import release_path_migration as migration
from termdeck import web_activation as activation
from test_release_manager import write_manifest, write_wheel


FIXTURE = Path(__file__).parent / "fixtures/legacy_c9d620_copied_activation"
PROJECT = Path(__file__).parents[1]
RUNTIME = PROJECT / ".termdeck"
RELEASE_ID = "c9d6204ee6dbe01dc4225970cb41014e759d38146bef824b5fbda71cb3ed707c"
CONTROL_FIXTURE_VALIDATOR = b'''
def _assert_control_binding(layout):
    root = layout.control_root
    _assert_canonical(root, label="isolated control fixture", must_exist=True)
    info = root.lstat()
    if (root != Path("/etc/termdeck/control") or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o710 or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid() or tuple(root.iterdir())):
        raise WrapperError("isolated control fixture changed")
'''


def captured_preimages():
    provenance = json.loads((FIXTURE / "provenance.json").read_bytes())
    result = {}
    for name, record in provenance["files"].items():
        content = (FIXTURE / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == record["sha256"]
        assert len(content) == record["size"]
        result[name] = content
    return provenance, result


@pytest.mark.parametrize("change", ["none", "release", "wheel", "source", "bootstrap", "launcher", "no-profile"])
def test_captured_variant_gate_requires_the_complete_known_rendering(change):
    """Fast gate checks; the separate namespace test verifies the real wheel."""
    provenance, original = captured_preimages()
    source = manager.release_layout(root=provenance["source_root"], bin_dir=provenance["source_bin_dir"])
    state = json.loads(original["state.json"])
    anchor = b'os.environ["T1000_MANAGED_ROOT"] = str(ROOT)\n'
    assert original["bootstrap.py"].count(anchor) == 1
    bootstrap = original["bootstrap.py"].replace(anchor, anchor + b'os.environ["TERMDECK_STATE_ROOT"] = \'/warp/state/termdeck\'\n', 1)
    prior_launcher = manager._launcher_source(source, Path(state["python_executable"]))
    assert manager.sha256_bytes(prior_launcher) == state["launcher_sha256"]
    launcher = prior_launcher.replace(b"set -eu\n", b"set -eu\nexport TERMDECK_STATE_ROOT=/warp/state/termdeck\n", 1)
    profile = {"release_id": RELEASE_ID, "wheel_sha256": "491fdd87ef8b97d488a456f1d5f893f284bf227693d2e7f7a61a30b2b09c080e"}
    if change in {"release", "wheel"}:
        profile["release_id" if change == "release" else "wheel_sha256"] = "0" * 64
    elif change == "source":
        source = manager.release_layout(root=source.root / "foreign", bin_dir=source.bin_dir)
    elif change == "bootstrap":
        bootstrap += b"\n# foreign bytes\n"
    elif change == "launcher":
        launcher += b"\n# foreign bytes\n"
    elif change == "no-profile":
        profile = None
    rendered = manager._activation_bundle(bootstrap, launcher, origin="selected-release")
    result = migration._captured_pre_wrapper_variant(rendered, source, profile)
    if change == "none":
        assert result.bootstrap == original["bootstrap.py"]
        assert result.launcher_sha256 == state["launcher_sha256"]
    else:
        assert result is None


def test_activation_cli_reports_release_preflight_refusal(monkeypatch, capsys):
    from contextlib import nullcontext
    monkeypatch.setattr(activation, "_paths", lambda _args: object())
    monkeypatch.setattr(activation, "_activation_lock", lambda _paths: nullcontext())
    def refused(*_args):
        raise manager.ReleaseError("legacy path migration state is not the canonical historical activation")
    monkeypatch.setattr(activation, "execute", refused)
    assert activation.main(["--execute", "--transaction", "/fixture/tx-refused"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"ok": False, "error": "legacy path migration state is not the canonical historical activation"}


@pytest.fixture
def captured_install(tmp_path):
    supplied = os.environ.get("TERMDECK_TEST_LEGACY_RELEASE")
    if not supplied:
        pytest.skip("set TERMDECK_TEST_LEGACY_RELEASE for the exact immutable historical wheel")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap is required to isolate the captured absolute deployment paths")
    provenance, original = captured_preimages()
    source = Path(supplied)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest == manager._PRE_WRAPPER_V1_MANIFEST
    root = tmp_path / "runtime"
    layout = manager.release_layout(root=root / "t1000-managed", bin_dir=root / "bin")
    verified = manager.verify_wheel(source / "artifacts" / manifest["wheel"], manifest_path=manifest_path)
    with manager.release_lock(layout):
        manager._stage_release(layout, verified, manifest_path)
    layout.bin_dir.mkdir()
    for name, destination in (("bootstrap.py", layout.bootstrap_path), ("state.json", layout.state_path), ("t1000", layout.launcher_path)):
        destination.write_bytes(original[name])
        destination.chmod(provenance["files"][name]["mode"])
    autostart = tmp_path / "autostart"
    autostart.mkdir()
    name = "io.github.t1000.T1000.desktop"
    (autostart / name).write_bytes(original[name])
    (autostart / name).chmod(provenance["files"][name]["mode"])
    control = tmp_path / "control"
    control.mkdir(mode=0o710)
    control.chmod(0o710)
    (root / "state").mkdir()
    (root / "units").mkdir()
    # Preserve the actual user-unit baseline in the private mount too. No
    # host service manager is reachable through the fixture command surface.
    for unit in (*activation.SERVICES, activation.WATCHDOG_UNIT):
        source_unit = Path("/home/arch/.config/systemd/user") / unit
        _path, capture = activation._capture_anchored(source_unit, label="read-only unit fixture capture")
        _path, confirmed = activation._capture_anchored(source_unit, label="read-only unit fixture readback")
        assert confirmed == capture
        if capture.snapshot.exists:
            copied = root / "units" / unit
            copied.write_bytes(capture.snapshot.content)
            copied.chmod(capture.snapshot.mode)
    (root / "state/preservation-sentinel").write_bytes(b"saved tabs and private data remain unchanged\n")
    with sqlite3.connect(root / "state/sessions.sqlite3") as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('preserved-session', '/work')")
    package = PROJECT / "termdeck"
    members = {"termdeck/" + str(path.relative_to(package)): path.read_bytes()
               for path in package.rglob("*.py") if path.name != "__init__.py"}
    members.update({"termdeck/" + str(path.relative_to(package)): path.read_bytes()
                    for path in (package / "data").iterdir() if path.is_file()})
    # Isolate external host storage/control admission in this user namespace,
    # which deliberately has no mapped root signer. Historical A and all
    # copied executable preimages remain exact; this is not production
    # admission proof. The captured desktop is also published and restored.
    members["termdeck/storage_fabric.py"] += b"\n\ndef require_hot_storage(**kwargs):\n    return None\n"
    members["termdeck/service_wrappers.py"] += CONTROL_FIXTURE_VALIDATOR
    wheel = tmp_path / "candidate/t1000-0.2.21-py3-none-any.whl"
    info = write_wheel(wheel, seed="b", version="0.2.21", extra_members=members)
    candidate_manifest = wheel.with_suffix(".release.json")
    write_manifest(candidate_manifest, wheel, info)
    temporary = tmp_path / "private-tmp"
    temporary.mkdir()
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text("#!/bin/sh\necho 'service action forbidden in captured-preimage fixture' >&2\nexit 99\n")
    fake_systemctl.chmod(0o500)
    return tmp_path, root, layout, wheel, candidate_manifest, bwrap, fake_systemctl, temporary, autostart, control


DRIVER = r'''
import json,os,sys
from pathlib import Path
from termdeck import release_manager as m, legacy_activation as legacy, web_activation as a, service_wrappers as w
from test_legacy_activation import stage_bootstrap, install
from test_web_activation import _storage_attestation,_inject_post_install_failure
from test_legacy_rendering_variant import CONTROL_FIXTURE_VALIDATOR
from types import SimpleNamespace

project=Path(sys.argv[1]); wheel=Path(sys.argv[2]); manifest=Path(sys.argv[3]); change=sys.argv[4]
runtime=project/'.termdeck'
paths=a.ActivationPaths(target=project,evidence=runtime/'state/recovery/activation-v2',runtime=runtime,
    database=runtime/'state/sessions.sqlite3',managed_root=runtime/'t1000-managed',managed_bin=runtime/'bin',unit_dir=Path('/home/arch/.config/systemd/user'))
layout=m.release_layout(root=paths.managed_root,bin_dir=paths.managed_bin)
state=m.load_managed_state(layout)
original={p:p.read_bytes() for p in (layout.bootstrap_path,layout.state_path,layout.launcher_path)}
desktop=m._production_autostart_path()
original[desktop]=desktop.read_bytes()
original_modes={p:p.stat().st_mode & 0o777 for p in original}
desktop_owner=(desktop.stat().st_uid,desktop.stat().st_gid,desktop.stat().st_nlink)
unit_paths=[paths.unit_dir/name for name in (*a.SERVICES,a.WATCHDOG_UNIT)]
def unit_preimages():
    return {p:(p.read_bytes(),p.stat().st_mode & 0o777) if p.exists() else None for p in unit_paths}
original_units=unit_preimages()
sentinel=runtime/'state/preservation-sentinel'; sentinel_before=sentinel.read_bytes()
m._require_managed_storage=lambda _layout:_storage_attestation()
exec(CONTROL_FIXTURE_VALIDATOR,w.__dict__)
candidate=json.loads(manifest.read_bytes())
tx,receipt=a.new_transaction(paths,source={'commit':'b'*40,'tree':'b'*40,'branch':'fixture','origin_main':'a'*40})
receipt.update(status='artifacts-staged',package={'wheel':str(wheel),'manifest':str(manifest),'release_id':candidate['release_id']},
    legacy_path_migration_request={'source_root':'/warp/runtime/termdeck/t1000-managed','source_bin_dir':'/warp/runtime/termdeck/bin'})
a._write(tx/'receipt.json',receipt)
if change=='bootstrap':
    layout.bootstrap_path.write_bytes(original[layout.bootstrap_path]+b'\n# foreign bytes\n')
elif change=='bootstrap-and-state':
    data=original[layout.bootstrap_path]+b'\n# self-consistent foreign bytes\n'
    layout.bootstrap_path.write_bytes(data)
    state['bootstrap_sha256']=m.sha256_bytes(data)
    layout.state_path.write_bytes(m.canonical_json_bytes(state))
elif change=='launcher':
    layout.launcher_path.write_bytes(original[layout.launcher_path]+b'\n# foreign launcher\n')
elif change=='state':
    state['launcher_sha256']='0'*64
    layout.state_path.write_bytes(m.canonical_json_bytes(state))
elif change=='source':
    receipt['legacy_path_migration_request']['source_root']='/warp/runtime/termdeck/foreign-managed'
elif change=='autostart':
    desktop.write_bytes(original[desktop]+b'\n# foreign desktop\n')
elif change=='autostart-mode':
    desktop.chmod(0o600)
elif change=='autostart-receipt':
    m._production_autostart_receipt_path(layout).write_bytes(b'{}\n')
before={p:p.read_bytes() for p in original}
case=SimpleNamespace(paths=paths,tx=tx,receipt=receipt,candidate=candidate,original_release=state['current_release_id'])
if change not in {'none','execute','autostart-inode','autostart-inverse-intent','autostart-inverse-published'}:
    import contextlib,io,os
    os.environ['TERMDECK_ACTIVATION_APPROVED']='1'
    a._write(tx/'receipt.json',receipt)
    error=io.StringIO()
    with contextlib.redirect_stderr(error):
        code=a.main(['--execute',*legacy._arguments(paths,tx)])
    assert code==2 and json.loads(error.getvalue())['ok'] is False,error.getvalue()
    assert 'Traceback' not in error.getvalue()
    assert {p:p.read_bytes() for p in before}==before
    assert sentinel.read_bytes()==sentinel_before
    assert unit_preimages()==original_units
    assert not (tx/'legacy-recovery.py').exists()
    print(json.dumps({'refused':change,'preimages_unchanged':True,'controlled_cli_error':True}));sys.exit(0)
if change=='execute':
    import pytest
    patch=pytest.MonkeyPatch()
    real_checks={name:getattr(a,name) for name in ('_preflight_watchdog_wrapper','snapshot_database','verify_database')}
    _inject_post_install_failure(patch)
    for name,implementation in real_checks.items():
        patch.setattr(a,name,implementation)
    patch.setattr(a,'GENERATED_RUNTIME',())
    try:
        a.execute(paths,tx)
    except RuntimeError as exc:
        assert str(exc)=='post-install activation failure',str(exc)
    else:
        raise AssertionError('injected post-install failure was not reached')
    final=a._read(tx/'receipt.json')
    assert final['status']=='rolled-back',final.get('rollback_error')
    assert final['legacy_bootstrap']['inverse_complete'] is True
    assert final['post_checks']['database']['preserved_session_ids']==1
    assert str(desktop) in final['legacy_bootstrap']['package_preimages']
    assert {p:p.read_bytes() for p in original}==original
    assert {p:p.stat().st_mode & 0o777 for p in original}==original_modes
    assert (desktop.stat().st_uid,desktop.stat().st_gid,desktop.stat().st_nlink)==desktop_owner
    assert not m._production_autostart_receipt_path(layout).exists()
    assert sentinel.read_bytes()==sentinel_before
    assert unit_preimages()==original_units
    print(json.dumps({'complete_execute_rollback':True,'exact_original_inverse':True}));sys.exit(0)
receipt=stage_bootstrap(case)
if change=='autostart-inode':
    publish=m._publish_install_wrapper_and_activation
    foreign_inode=[]
    def replace_before_publication(*args,**kwargs):
        replacement=desktop.with_suffix('.replacement')
        replacement.write_bytes(original[desktop])
        replacement.chmod(original_modes[desktop])
        os.replace(replacement,desktop)
        foreign_inode.append(desktop.stat().st_ino)
        return publish(*args,**kwargs)
    m._publish_install_wrapper_and_activation=replace_before_publication
    try:
        install(case,receipt)
    except m.ReleaseError as exc:
        assert str(exc)==desktop.name+' changed after activation preflight',str(exc)
    else:
        raise AssertionError('foreign same-byte desktop inode was accepted')
    assert desktop.stat().st_ino==foreign_inode[0]
    assert {p:p.read_bytes() for p in original}==original
    assert not m._production_autostart_receipt_path(layout).exists()
    assert all(not p.exists() for p in a._wrapper_publication_paths(paths))
    assert sentinel.read_bytes()==sentinel_before
    assert unit_preimages()==original_units
    print(json.dumps({'refused':change,'preimages_unchanged':True,'foreign_inode_preserved':True}));sys.exit(0)
result=install(case,receipt)
assert result['path_migration']['historical_paths_written'] is False
if change.startswith('autostart-inverse-'):
    phase=change.removeprefix('autostart-inverse-')
    child=os.fork()
    if child==0:
        try:
            receipt=a._read(tx/'receipt.json')
            def checkpoint(name):
                a._checkpoint_publication(paths,tx,receipt,name)
                record=receipt['legacy_bootstrap']['package_preimages'][str(desktop)]
                if (name=='legacy:package-inverse:'+str(desktop)
                        and record.get('rollback_publication',{}).get('state')==phase):
                    os._exit(87)
            legacy.restore_package(paths,tx,receipt,checkpoint)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(88)
        os._exit(89)
    _pid,status=os.waitpid(child,0)
    assert os.waitstatus_to_exitcode(status)==87
    receipt=a._read(tx/'receipt.json')
    inverse=receipt['legacy_bootstrap']['package_preimages'][str(desktop)]['rollback_publication']
    assert inverse['state']==phase
    assert Path(inverse['receipt']['journal_path']).is_file()
    legacy.restore_package(paths,tx,receipt,lambda name:a._checkpoint_publication(paths,tx,receipt,name))
    assert {p:p.read_bytes() for p in original}==original
    assert {p:p.stat().st_mode & 0o777 for p in original}==original_modes
    assert (desktop.stat().st_uid,desktop.stat().st_gid,desktop.stat().st_nlink)==desktop_owner
    assert not m._production_autostart_receipt_path(layout).exists()
    assert all(not p.exists() for p in a._wrapper_publication_paths(paths))
    assert sentinel.read_bytes()==sentinel_before
    assert unit_preimages()==original_units
    print(json.dumps({'inverse_crash':phase,'exact_original_inverse':True}));sys.exit(0)
import subprocess
for args in (['--help'],['release','info','--root',str(layout.root),'--bin-dir',str(layout.bin_dir),'--verify']):
    result=subprocess.run([str(layout.launcher_path),*args],capture_output=True,text=True,timeout=40)
    if result.returncode:
        print(result.stdout, file=sys.stderr)
    assert result.returncode==0,{'args':args,'code':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
    if args[0]=='release': assert json.loads(result.stdout)['ok'] is True
receipt=a._read(tx/'receipt.json')
legacy.restore_package(paths,tx,receipt,lambda name:a._checkpoint_publication(paths,tx,receipt,name))
assert {p:p.read_bytes() for p in original}==original
assert {p:p.stat().st_mode & 0o777 for p in original}==original_modes
assert (desktop.stat().st_uid,desktop.stat().st_gid,desktop.stat().st_nlink)==desktop_owner
assert not m._production_autostart_receipt_path(layout).exists()
assert sentinel.read_bytes()==sentinel_before
assert unit_preimages()==original_units
assert all(not p.exists() for p in a._wrapper_publication_paths(paths))
print(json.dumps({'migrated':True,'launcher_help':True,'release_info':True,'exact_original_inverse':True}))
'''


@pytest.mark.parametrize("change", ["none", "execute", "bootstrap", "bootstrap-and-state", "launcher", "state", "source", "autostart", "autostart-mode", "autostart-receipt", "autostart-inode", "autostart-inverse-intent", "autostart-inverse-published"])
def test_captured_actual_activation_migrates_and_restores_only_qualified_bytes(captured_install, change):
    tmp_path, root, layout, wheel, manifest, bwrap, fake_systemctl, temporary, autostart, control = captured_install
    command = [bwrap, "--unshare-user", "--unshare-net", "--die-with-parent",
               "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               "--bind", str(tmp_path), str(tmp_path), "--bind", str(root), str(RUNTIME),
               "--bind", str(autostart), "/home/arch/.config/autostart",
               "--bind", str(root / "units"), "/home/arch/.config/systemd/user",
               "--ro-bind", str(control), "/etc/termdeck/control",
               "--ro-bind", str(fake_systemctl), "/usr/bin/systemctl",
               "--setenv", "TMPDIR", str(temporary),
               "--setenv", "PYTHONPATH", str(PROJECT) + os.pathsep + str(PROJECT / "tests"),
               "--", sys.executable, "-B", "-c", DRIVER, str(PROJECT), str(wheel), str(manifest), change]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    outcome = json.loads(result.stdout)
    if change in {"none", "execute"} or change.startswith("autostart-inverse-"):
        assert outcome["exact_original_inverse"] is True
    else:
        assert outcome["preimages_unchanged"] is True
