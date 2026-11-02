import os
from unittest import mock

import pytest

from termdeck import codex_config_scope as scope
import test_codex_launcher_chain as existing_chain


@pytest.fixture
def successor_chain():
    fixture = existing_chain.CodexLauncherChainTests(methodName="runTest")
    fixture.setUp()
    with mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SHA256", "0" * 64), \
         mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SUCCESSOR_SHA256", fixture.router_digest):
        yield fixture
    fixture.doCleanups()


def test_exact_successor_chain_is_recognized_without_caller_attestor(successor_chain):
    assert successor_chain.resolve() == successor_chain.router
    assert successor_chain.prepare().launcher_path == successor_chain.router


def test_previous_accepted_router_remains_recognized_without_successor_pin(successor_chain):
    with mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SHA256", successor_chain.router_digest), \
         mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SUCCESSOR_SHA256", "0" * 64):
        assert successor_chain.resolve() == successor_chain.router


def test_successor_pin_does_not_accept_a_changed_router(successor_chain):
    successor_chain.router.write_bytes(successor_chain.router.read_bytes() + b"\n")
    assert successor_chain.resolve() is None


def test_successor_pin_does_not_accept_an_unreviewed_contained_payload(successor_chain):
    successor_chain.contained.write_bytes(successor_chain.contained.read_bytes() + b"\n")
    assert successor_chain.resolve() is None


def test_successor_pin_does_not_accept_an_unreviewed_helper(successor_chain):
    successor_chain.helper.write_bytes(successor_chain.helper.read_bytes() + b"\n")
    assert successor_chain.resolve() is None


@pytest.mark.parametrize("node_name", ["router", "contained", "helper", "bin", "libexec"])
def test_successor_pin_does_not_accept_a_writable_chain_node(successor_chain, node_name):
    path = getattr(successor_chain, node_name)
    path.chmod(path.stat().st_mode | 0o020)
    assert successor_chain.resolve() is None


@pytest.mark.parametrize("node_name", ["router", "contained", "helper"])
def test_successor_pin_does_not_accept_a_hardlinked_payload(successor_chain, node_name):
    path = getattr(successor_chain, node_name)
    os.link(path, successor_chain.root / (node_name + "-alias"))
    assert successor_chain.resolve() is None


@pytest.mark.parametrize("node_name", ["router", "contained"])
def test_successor_pin_does_not_follow_a_payload_symlink(successor_chain, node_name):
    path = getattr(successor_chain, node_name)
    target = path.with_name(path.name + "-target")
    path.rename(target)
    path.symlink_to(target.name)
    assert successor_chain.resolve() is None


def test_successor_pin_does_not_attest_a_caller_selected_router_path(successor_chain):
    with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(successor_chain.router)}):
        with pytest.raises(scope.CodexConfigScopeError, match="not an attested"):
            successor_chain.resolve()


def test_successor_pin_does_not_attest_a_foreign_entry_copy(successor_chain):
    foreign = successor_chain.root / "foreign"
    foreign.mkdir()
    entry = foreign / "codex"
    entry.symlink_to(successor_chain.router)
    with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(entry)}):
        with pytest.raises(scope.CodexConfigScopeError, match="not an attested"):
            successor_chain.resolve()


def test_successor_pin_does_not_ignore_router_replacement_during_read(successor_chain):
    native_reader = scope._read_small_regular

    def replace_after_read(path, *, maximum):
        payload = native_reader(path, maximum=maximum)
        if path == successor_chain.router:
            replacement = path.with_name(path.name + "-replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o700)
            replacement.replace(path)
        return payload

    with mock.patch.object(scope, "_read_small_regular", side_effect=replace_after_read):
        assert successor_chain.resolve() is None
