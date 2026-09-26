"""Frozen candidate recovery and the exact pre-wrapper release bootstrap.

The recovery program imports verified candidate bytes from its own frozen
capsule.  It never asks existing A to supply code it did not ship.  Managed
selection, wrappers and launcher remain untouched until that program is armed.
The existing descriptor journals own every forward and inverse publication.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Mapping

from . import release_manager as manager

SCHEMA = "termdeck.legacy-activation-bootstrap.v1"


def _api():
    from . import web_activation
    return web_activation


def _layout(paths):
    return manager.release_layout(root=paths.managed_root, bin_dir=paths.managed_bin)


def _capsule_layout(tx):
    return manager.release_layout(root=tx / "recovery-capsule", bin_dir=tx / "recovery-bin")


def _arguments(paths, tx):
    return [
        "--transaction", str(tx), "--target", str(paths.target),
        "--evidence", str(paths.evidence), "--runtime", str(paths.runtime),
        "--database", str(paths.database), "--managed-root", str(paths.managed_root),
        "--managed-bin", str(paths.managed_bin), "--unit-dir", str(paths.unit_dir),
    ]


def _runner_source(site, arguments):
    """Pin verified Python bytes before importing anything from the capsule.

    The stdlib loader uses captured bytes, closing the hash-then-import gap.
    It does not add a checkout or an unverified package directory to sys.path.
    The frozen site remains available for verified package data through
    __file__; it is reverified by recovery before any external mutation.
    """
    rows = {}
    directories = {}
    for path in sorted(site.rglob("*.py")):
        st = path.lstat()
        relative = str(path.relative_to(site))
        rows[relative] = [st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                          st.st_nlink, st.st_size, hashlib.sha256(path.read_bytes()).hexdigest()]
        for parent in (path.parent, *path.parent.parents):
            pst = parent.lstat()
            directories[str(parent)] = [pst.st_dev, pst.st_ino, pst.st_mode]
    return ("#!/usr/bin/python3\n" +
        "import hashlib, importlib.abc, importlib.util, os, stat, sys\n" +
        f"ROOT = {str(site)!r}\nFILES = {rows!r}\nDIRECTORIES = {directories!r}\n" +
        f"ARGUMENTS = {arguments!r}\n" + r'''
def refuse():
    raise SystemExit("legacy recovery capsule identity changed")
for path, expected in DIRECTORIES.items():
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino, info.st_mode] != expected:
        refuse()
modules = {}
for relative, expected in FILES.items():
    path = ROOT + "/" + relative
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        observed = [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_nlink, info.st_size]
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or observed != expected[:7]:
            refuse()
        content = stream.read()
        if hashlib.sha256(content).hexdigest() != expected[7]:
            refuse()
    package = relative.endswith("/__init__.py")
    name = relative[:-12] if package else relative[:-3]
    modules[name.replace("/", ".")] = (content, path, package)
class VerifiedLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        row = modules.get(fullname)
        return importlib.util.spec_from_loader(fullname, self, is_package=row[2]) if row else None
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        content, path, package = modules[module.__name__]
        module.__file__ = path
        if package:
            module.__path__ = [os.path.dirname(path)]
        exec(compile(content, path, "exec"), module.__dict__)
if len(sys.argv) != 2 or sys.argv[1] not in ("--watchdog", "--rollback"):
    raise SystemExit("choose --watchdog or --rollback for this bound transaction")
sys.meta_path.insert(0, VerifiedLoader())
from termdeck.web_activation import main
raise SystemExit(main([sys.argv[1], *ARGUMENTS]))
''').encode()


def prepare(paths, tx, receipt):
    """Authenticate the explicit path transition and freeze recovery B."""
    api = _api()
    layout = _layout(paths)
    current = api._managed_release_id(paths)
    request = receipt.get("legacy_path_migration_request")
    profile = manager._verified_pre_wrapper_profile(layout, current) if isinstance(current, str) else None
    if profile is None:
        if request is not None:
            raise api.WebActivationError("legacy activation request does not identify the supported historical release")
        return None
    state = manager.load_managed_state(layout)
    if manager._resolve_production_intent(layout, None):
        manager._require_managed_storage(layout)
    if not isinstance(request, dict) or set(request) != {"source_root", "source_bin_dir"}:
        raise api.WebActivationError("legacy activation requires explicit --migrate-from-root and --migrate-from-bin-dir during prepare")
    from .release_path_migration import prepare_legacy_path_migration
    with manager.release_lock(layout):
        plan = prepare_legacy_path_migration(
            layout, state, Path(sys.executable).resolve(strict=True),
            source_root=request["source_root"], source_bin_dir=request["source_bin_dir"],
        )
        original = api._watchdog_state_for_release(paths, current)
        plan.revalidate()
    result = _freeze_capsule(paths, tx, receipt, original)
    result.update(schema=SCHEMA, profile=profile, migration=request)
    return result


def prepare_recovery(paths, tx, receipt):
    """Arm candidate recovery for modern A without executing A's wrapper.

    A may predate lifecycle receipts. Its bytes and runtime config remain
    unmodified until the ordinary release publication owns the transition.
    The legacy exact inverse remains gated by its separate historical profile.
    """
    api = _api()
    current = api._managed_release_id(paths)
    if current is None:
        return None
    layout = _layout(paths)
    if manager._resolve_production_intent(layout, None):
        manager._require_managed_storage(layout)
    with manager.release_lock(layout):
        manager._verify_managed_activation(layout, manager.load_managed_state(layout), verify_release=True)
        original = api._watchdog_state_for_release(paths, current)
        binding = manager._release_wrapper_execution_kwargs(layout, current)
    result = _freeze_capsule(paths, tx, receipt, original)
    result.update(schema="termdeck.activation-recovery-capsule.v1",
                  original_binding=json.loads(json.dumps(binding, default=str)))
    return result


def _freeze_capsule(paths, tx, receipt, original):
    api = _api()
    package = receipt.get("package")
    if not isinstance(package, Mapping):
        raise api.WebActivationError("legacy activation lacks a qualified recovery package")
    verified = manager.verify_wheel(package["wheel"], manifest_path=package["manifest"])
    if verified.manifest is None or verified.manifest["release_id"] != package.get("release_id"):
        raise api.WebActivationError("legacy recovery package identity differs from the prepared transaction")
    capsule = _capsule_layout(tx)
    with manager.release_lock(capsule):
        release = manager._stage_release(capsule, verified, Path(package["manifest"]))
        manager._release_wrapper_execution_kwargs(capsule, str(package["release_id"]))
    runner = tx / "legacy-recovery.py"
    content = _runner_source(release / "site", _arguments(paths, tx))
    if os.path.lexists(runner):
        raise api.WebActivationError("legacy recovery program already exists; prepare a fresh transaction")
    runner_image = api._atomic(runner, content, 0o500)
    return {
        "original_state": original, "release_id": package["release_id"],
        "runner": runner_image,
        "arguments": _arguments(paths, tx),
        "capsule_binding": json.loads(json.dumps(
            manager._release_wrapper_execution_kwargs(capsule, str(package["release_id"])), default=str,
        )),
    }


def _data(paths, tx, receipt):
    api = _api()
    data = receipt.get("legacy_bootstrap")
    modern = data is None
    if modern:
        data = receipt.get("recovery_capsule")
    expected_schema = "termdeck.activation-recovery-capsule.v1" if modern else SCHEMA
    if not isinstance(data, dict) or data.get("schema") != expected_schema:
        raise api.WebActivationError("legacy activation bootstrap receipt is invalid")
    if data.get("arguments") != _arguments(paths, tx):
        raise api.WebActivationError("legacy activation paths differ from the recovery capsule")
    if modern:
        binding = manager._release_wrapper_execution_kwargs(_layout(paths), receipt.get("old_release_id"))
        if json.loads(json.dumps(binding, default=str)) != data.get("original_binding"):
            raise api.WebActivationError("recovery capsule original release identity changed")
    else:
        if data.get("migration") != receipt.get("legacy_path_migration_request"):
            raise api.WebActivationError("legacy activation migration request changed")
        profile = manager._verified_pre_wrapper_profile(_layout(paths), receipt.get("old_release_id"))
        if profile is None or profile != data.get("profile"):
            raise api.WebActivationError("legacy activation original release identity changed")
    package = receipt.get("package")
    if not isinstance(package, Mapping) or data.get("release_id") != package.get("release_id"):
        raise api.WebActivationError("legacy recovery capsule differs from the candidate")
    capsule = _capsule_layout(tx)
    actual = manager._release_wrapper_execution_kwargs(capsule, data["release_id"])
    # JSON persists paths as strings; compare the normalized immutable binding.
    if json.loads(json.dumps(actual, default=str)) != data.get("capsule_binding"):
        raise api.WebActivationError("legacy recovery capsule execution binding changed")
    runner = tx / "legacy-recovery.py"
    api._assert_current_postimage(runner, data.get("runner"), label="legacy recovery program")
    _path, capture = api._read_regular(runner, label="legacy recovery program")
    if capture.snapshot.content != _runner_source(Path(actual["execution_root"]), _arguments(paths, tx)):
        raise api.WebActivationError("legacy recovery program is not the verified canonical loader")
    return data


def arm(paths, tx, receipt, preimages, checkpoint):
    api = _api()
    data = _data(paths, tx, receipt)
    if api._watchdog_state_for_release(paths, receipt["old_release_id"]) != data["original_state"]:
        raise api.WebActivationError("legacy selection changed before recovery was armed")
    receipt["watchdog"] = {
        "unit": api.WATCHDOG_UNIT, "arming": True,
        "arm_phase": "recovery-capsule" if receipt.get("recovery_capsule") else "legacy-capsule",
        "expected_release_id": receipt["old_release_id"],
    }
    api._persist(paths, tx, receipt, "watchdog-installing")
    api._arm(paths, tx, preimage=preimages["unit:" + api.WATCHDOG_UNIT], checkpoint=checkpoint,
             recovery_command=[str(Path(sys.executable).resolve()), "-I", "-B", str(tx / "legacy-recovery.py"), "--watchdog"])
    _data(paths, tx, receipt)
    if api._watchdog_state_for_release(paths, receipt["old_release_id"]) != data["original_state"]:
        raise api.WebActivationError("legacy selection changed while recovery was armed")
    receipt["watchdog"].update(arming=False, armed_before_disruption=True)
    api._persist(paths, tx, receipt, "watchdog-armed")


def owns(paths, tx, receipt):
    api = _api()
    data = _data(paths, tx, receipt)
    watchdog = receipt.get("watchdog")
    if not isinstance(watchdog, Mapping) or watchdog.get("arm_phase") not in {"legacy-capsule", "recovery-capsule"}:
        return False
    if watchdog.get("disarmed_for_rollback") or watchdog.get("disarmed_after_activation"):
        return False
    record = receipt["preimages"].get("unit:" + api.WATCHDOG_UNIT)
    if not isinstance(record, Mapping):
        return False
    api._assert_current_postimage(paths.unit_dir / api.WATCHDOG_UNIT, record.get("postimage"), label="legacy recovery unit")
    state = api._managed_release_state_snapshot(paths, require_generation=True)
    if state == data["original_state"]:
        return True
    parsed = api._release_publication_record_state(receipt, key="package_publication")
    if parsed is None or parsed[1] != "install" or parsed[0].get("state") != "published":
        return False
    publication = api._release_publication_recovered_handoff(parsed[0])
    image = api._release_callback_postimage(publication, paths.managed_root / "state.json")
    return (isinstance(state, Mapping) and state.get("release_id") == data["release_id"]
            and state.get("sha256") == image.get("sha256") and state.get("identity") == image.get("identity"))


def capture_package_preimages(paths, tx, receipt, publication):
    """Capture all package destinations from the live pre-publication callback."""
    api = _api()
    data = _data(paths, tx, receipt)
    records = data.setdefault("package_preimages", {})
    if records:
        raise api.WebActivationError("legacy package publication repeats its preimage capture")
    wrappers = set(api._wrapper_publication_paths(paths))
    for path, capture in publication.preimages.items():
        key = "wrapper:" + str(path)
        if path in wrappers:
            record = receipt["preimages"][key]
        else:
            name = "legacy-package-" + hashlib.sha256(str(path).encode()).hexdigest()
            record = api._preimage(path, tx, name)
        if api._receipt_preimage(record, tx) != capture:
            raise api.WebActivationError("legacy package preimage differs from the release journal")
        if api._record_parent_guards(record, path).for_path(path) != publication.parent_guards.for_path(path):
            raise api.WebActivationError("legacy package parent differs from the release journal")
        records[str(path)] = record


def capture_package_postimages(paths, tx, receipt, publication, *, recovered=False):
    api = _api()
    data = _data(paths, tx, receipt)
    records = data.get("package_preimages")
    if not isinstance(records, dict) or set(records) != {str(path) for path in publication.preimages}:
        raise api.WebActivationError("legacy package publication destination set changed")
    for raw, record in records.items():
        path = Path(raw)
        capture = publication.postimages.get(path)
        if not isinstance(capture, manager._AnchoredFileSnapshot):
            raise api.WebActivationError("legacy package lacks its exact journal postimage")
        if recovered:
            before = api._receipt_preimage(record, tx)
            if capture.snapshot != before.snapshot:
                raise api.WebActivationError("legacy package recovery changed the original preimage")
        elif publication.preimages[path] != api._receipt_preimage(record, tx):
            raise api.WebActivationError("legacy package publication preimage changed")
        if capture.snapshot.exists:
            guards = api._record_parent_guards(record, path)
            if guards.for_path(path) != publication.parent_guards.for_path(path):
                raise api.WebActivationError("legacy package postimage parent changed")
            record["rollback_postimage" if recovered else "postimage"] = api._postimage_payload(path, capture, guards)
        elif not recovered:
            raise api.WebActivationError("legacy package forward publication unexpectedly removed a file")
        # Wrapper records are also used by the ordinary outer inverse.
        wrapper_key = "wrapper:" + raw
        if wrapper_key in receipt["preimages"]:
            receipt["preimages"][wrapper_key] = record


def restore_package(paths, tx, receipt, checkpoint):
    """Restore exact original A leaves; never render A with current templates."""
    api = _api()
    data = _data(paths, tx, receipt)
    layout = _layout(paths)
    storage = (manager._require_managed_storage(layout)
               if manager._resolve_production_intent(layout, None) else None)
    records = data.get("package_preimages")
    if records is None:
        if api._watchdog_state_for_release(paths, receipt["old_release_id"]) != data["original_state"]:
            raise api.WebActivationError("legacy state changed without a package publication")
        return
    if not isinstance(records, dict) or not records:
        raise api.WebActivationError("legacy package inverse preimages are invalid")
    parsed = api._release_publication_record_state(receipt, key="package_publication")
    if parsed is None or parsed[1] != "install":
        raise api.WebActivationError("legacy package inverse lacks its publication")
    source = parsed[0].get("receipt")
    expected = api._release_publication_image_map(source, "preimages")
    if set(records) != {str(path) for path in expected}:
        raise api.WebActivationError("legacy package inverse destination set changed")
    for raw, record in records.items():
        path = Path(raw)
        if api._record_target(record) != path or api._release_publication_snapshot_payload(path, api._receipt_preimage(record, tx)) != expected[path]:
            raise api.WebActivationError("legacy package inverse original preimage changed")
    if not data.get("inverse_started"):
        if parsed[0].get("state") == "published":
            if api._managed_release_id(paths) != data["release_id"]:
                raise api.WebActivationError("legacy package inverse selected release changed")
            api._assert_package_rollback_owned(paths, receipt, receipt["preimages"], current_release_id=api._managed_release_id(paths), old_release_id=receipt["old_release_id"])
            # JSON deserialization gives the package and outer wrapper records
            # separate objects. Carry exact lifecycle successors into both.
            for raw in records:
                key = "wrapper:" + raw
                if key in receipt["preimages"]:
                    records[raw] = receipt["preimages"][key]
            # Authenticate the complete package set before the first inverse,
            # including bootstrap/launcher, path receipt and desktop leaves.
            for raw, record in records.items():
                api._assert_current_postimage(Path(raw), record.get("postimage"), label="legacy package inverse")
        elif parsed[0].get("state") != "recovered":
            raise api.WebActivationError("legacy package inverse requires journal recovery first")
        data["inverse_started"] = True
        checkpoint("legacy:package-inverse-intent")
    state_path = str(paths.managed_root / "state.json")
    for raw in sorted(records, key=lambda path: (path == state_path, path)):
        record = records[raw]
        api._restore(record, tx, checkpoint=lambda raw=raw: checkpoint("legacy:package-inverse:" + raw))
        wrapper_key = "wrapper:" + raw
        if wrapper_key in receipt["preimages"]:
            receipt["preimages"][wrapper_key] = record
        checkpoint("legacy:package-inverse:" + raw)
    data["inverse_complete"] = True
    if storage is not None:
        manager._revalidate_managed_storage(layout, manager._storage_attestation_binding(storage),
                                            phase="after legacy activation inverse")
    checkpoint("legacy:package-inverse-complete")
