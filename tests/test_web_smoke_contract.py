"""Qualification binaries must not be confused with shipped auth executables."""
import importlib.util
from pathlib import Path

import pytest


def validator():
    path = Path(__file__).resolve().parents[1] / "scripts/check_web_readonly_smoke.py"
    spec = importlib.util.spec_from_file_location("web_smoke_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._validate_auth_target_kinds


@pytest.mark.parametrize("kinds", [[["lib"]], [["lib"], ["test"]]])
def test_library_and_qualification_targets_are_allowed(kinds):
    validator()(kinds)


@pytest.mark.parametrize("kinds", [[], [["test"]], [["lib"], ["lib"]], [["lib"], ["bin"]], [["lib"], ["proc-macro"]], [["lib"], ["custom-build"]]])
def test_shipped_auth_entrypoints_and_invalid_targets_are_rejected(kinds):
    with pytest.raises(RuntimeError, match="library-only"):
        validator()(kinds)


def workspace_validator():
    path = Path(__file__).resolve().parents[1] / "scripts/check_web_readonly_smoke.py"
    spec = importlib.util.spec_from_file_location("web_workspace_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = (path.parents[1] / "web/crates/termdeck-web-server/assets/workspace-ui.js").read_text()
    return module._validate_workspace_contract, source


def test_confirmed_manager_send_is_distinct_from_normal_terminal_input():
    validate, source = workspace_validator()
    validate(source)


@pytest.mark.parametrize("suffix", ['fetch("/input/text")', 'fetch("/input/key")', 'fetch("/control/release")', 'innerHTML', 'Take control', 'sessionStorage', 'https://elsewhere.example'])
def test_obsolete_controls_and_unscoped_input_stay_forbidden(suffix):
    validate, source = workspace_validator()
    with pytest.raises(RuntimeError):
        validate(source + suffix)


@pytest.mark.parametrize("contract", ['if (!accepted) return', 'rows.length > 128', 'control.indeterminate', 'cancel.focus()'])
def test_manager_send_cannot_drop_confirmation_or_bounds(contract):
    validate, source = workspace_validator()
    with pytest.raises(RuntimeError):
        validate(source.replace(contract, 'removed'))


@pytest.mark.parametrize("operation,error,allowed", [
    ("list", "net::ERR_ABORTED", True),
    ("list", "net::ERR_FAILED", False),
    ("list", "net::ERR_CONNECTION_REFUSED", False),
    ("bulk", "net::ERR_ABORTED", False),
    ("move", "net::ERR_ABORTED", False),
    ("appearance", "net::ERR_ABORTED", False),
    (None, "net::ERR_ABORTED", False),
])
def test_browser_only_classifies_aborted_telemetry_reads(operation, error, allowed):
    path = Path(__file__).resolve().parents[1] / "scripts/web_management_browser_checks.py"
    spec = importlib.util.spec_from_file_location("manager_browser_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failure = {"url": "http://127.0.0.1:1234/_termdeck/api/v1/management", "management_operation": operation, "failure": error}
    assert module.is_cancelled_manager_read(failure) is allowed
    failure["url"] += "/different-endpoint"
    assert not module.is_cancelled_manager_read(failure)
