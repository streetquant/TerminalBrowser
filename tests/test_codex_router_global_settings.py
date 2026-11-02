import hashlib
import os
from unittest import mock

import pytest

from termdeck import codex_config_scope as scope
import test_codex_launcher_chain as existing_chain


@pytest.fixture
def global_chain():
    fixture = existing_chain.CodexLauncherChainTests(methodName="runTest")
    fixture.setUp()
    fixture.approved_helper = fixture.libexec / "codex-approved-runtime.py"
    fixture.approved_helper.write_text("raise RuntimeError('recognition must never execute helpers')\n")
    fixture.approved_helper.chmod(0o700)
    fixture.router.write_text(
        fixture.router.read_text()
        + 'readonly approved_runtime_helper="' + str(fixture.approved_helper) + '"\n'
        + 'readonly approved_runtime_role="direct-runtime-codex"\n'
    )
    fixture.router_digest = hashlib.sha256(fixture.router.read_bytes()).hexdigest()
    with mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_GLOBAL_SETTINGS_SHA256", fixture.router_digest), \
         mock.patch.object(scope, "_MANAGED_CODEX_GLOBAL_SETTINGS_HELPER_SHA256", hashlib.sha256(fixture.helper.read_bytes()).hexdigest()), \
         mock.patch.object(scope, "_MANAGED_CODEX_APPROVED_RUNTIME_HELPER_SHA256", hashlib.sha256(fixture.approved_helper.read_bytes()).hexdigest()):
        yield fixture
    fixture.doCleanups()


def test_global_router_requires_exact_native_and_approved_helpers(global_chain):
    assert global_chain.resolve() == global_chain.router
    assert global_chain.prepare().launcher_path == global_chain.router


def test_global_router_cannot_borrow_older_native_helper(global_chain):
    with mock.patch.object(scope, "_MANAGED_CODEX_GLOBAL_SETTINGS_HELPER_SHA256", "0" * 64):
        assert global_chain.resolve() is None


def test_current_native_helper_cannot_borrow_older_router_pin(global_chain):
    with mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_GLOBAL_SETTINGS_SHA256", "0" * 64), \
         mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SHA256", global_chain.router_digest):
        assert global_chain.resolve() is None


@pytest.mark.parametrize("node", ["router", "contained", "helper", "approved_helper"])
def test_global_chain_rejects_changed_payload(global_chain, node):
    path = getattr(global_chain, node)
    path.write_bytes(path.read_bytes() + b"\n")
    assert global_chain.resolve() is None


@pytest.mark.parametrize("node", ["router", "contained", "helper", "approved_helper", "bin", "libexec"])
def test_global_chain_rejects_writable_nodes(global_chain, node):
    path = getattr(global_chain, node)
    path.chmod(path.stat().st_mode | 0o020)
    assert global_chain.resolve() is None


@pytest.mark.parametrize("node", ["router", "contained", "helper", "approved_helper"])
def test_global_chain_rejects_hardlinked_payload(global_chain, node):
    path = getattr(global_chain, node)
    os.link(path, global_chain.root / (node + "-alias"))
    assert global_chain.resolve() is None


def test_global_chain_rejects_symlinked_approved_helper(global_chain):
    path = global_chain.approved_helper
    target = path.with_name(path.name + "-target")
    path.rename(target)
    path.symlink_to(target.name)
    assert global_chain.resolve() is None


def test_global_chain_rejects_missing_approved_helper(global_chain):
    global_chain.approved_helper.unlink()
    assert global_chain.resolve() is None


def test_global_chain_rejects_approved_helper_replacement_during_read(global_chain):
    native_reader = scope._read_small_regular

    def replace_after_read(path, *, maximum):
        payload = native_reader(path, maximum=maximum)
        if path == global_chain.approved_helper:
            replacement = path.with_name(path.name + "-replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o700)
            replacement.replace(path)
        return payload

    with mock.patch.object(scope, "_read_small_regular", side_effect=replace_after_read):
        assert global_chain.resolve() is None


@pytest.mark.parametrize("binding", ["approved_runtime_helper", "approved_runtime_role"])
def test_global_router_requires_approved_path_and_role(global_chain, binding):
    global_chain.router.write_text(global_chain.router.read_text().replace(binding, "unrelated"))
    digest = hashlib.sha256(global_chain.router.read_bytes()).hexdigest()
    with mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_GLOBAL_SETTINGS_SHA256", digest):
        assert global_chain.resolve() is None
