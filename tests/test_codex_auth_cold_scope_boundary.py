from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def smoke(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "cold_scope_boundary_smoke", scripts / "check_codex_auth_cold_scope_smoke.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_configured_binary_skips_without_discovery_or_execution(smoke, monkeypatch, capsys):
    monkeypatch.delenv("TERMDECK_REAL_CODEX_BINARY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("an omitted provider prerequisite must not discover or execute local tools")

    monkeypatch.setattr(smoke.shutil, "which", forbidden)
    monkeypatch.setattr(smoke.subprocess, "run", forbidden)
    monkeypatch.setattr(smoke.Path, "home", forbidden)
    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", forbidden)
    assert smoke.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["skipped"]
    assert "TERMDECK_REAL_CODEX_BINARY" in result["reason"]


def test_configured_binary_requires_an_absolute_path(smoke, monkeypatch):
    monkeypatch.setenv("TERMDECK_REAL_CODEX_BINARY", "codex")
    monkeypatch.setattr(smoke.shutil, "which", lambda *_: pytest.fail("no PATH fallback"))
    with pytest.raises(ValueError, match="absolute path"):
        smoke.main()


@pytest.mark.parametrize("rejected", ["missing", "contained", "wrapper", "nonexecutable"])
def test_configured_rejection_cannot_fall_back_to_another_installation(smoke, tmp_path, monkeypatch, rejected):
    candidate = tmp_path / ("codex-contained" if rejected == "contained" else "selected-codex")
    if rejected != "missing":
        candidate.write_text("termdeck-private-codex-wrapper" if rejected == "wrapper" else "fixture")
        candidate.chmod(0o600 if rejected == "nonexecutable" else 0o700)
    monkeypatch.setenv("TERMDECK_REAL_CODEX_BINARY", str(candidate))
    discoveries = []

    def which(name):
        discoveries.append(name)
        assert name == "tmux"
        return None

    monkeypatch.setattr(smoke.shutil, "which", which)
    monkeypatch.setattr(smoke.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("rejected executable was run"))
    with pytest.raises(RuntimeError, match="Explicit TERMDECK_REAL_CODEX_BINARY was rejected"):
        smoke.main()
    assert discoveries == ["tmux"]


def test_failed_configured_probe_is_isolated_and_never_retried(smoke, tmp_path, monkeypatch):
    candidate = tmp_path / "selected-codex"
    candidate.write_text("fixture")
    candidate.chmod(0o700)
    unrelated = tmp_path / "unrelated-home"
    monkeypatch.setenv("HOME", str(unrelated))
    monkeypatch.setenv("CODEX_HOME", str(unrelated / ".codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-must-not-be-inherited")
    monkeypatch.setenv("TERMDECK_REAL_CODEX_BINARY", str(candidate))
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None if name == "tmux" else pytest.fail("no provider discovery"))
    calls = []

    def fail_probe(argv, **kwargs):
        calls.append((argv, kwargs))
        home = Path(kwargs["env"]["HOME"])
        assert home.is_dir() and home != unrelated
        assert kwargs["cwd"] == home
        assert kwargs["env"]["CODEX_HOME"] == str(home / ".codex")
        assert "OPENAI_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 7, "", "fixture version failure")

    monkeypatch.setattr(smoke.subprocess, "run", fail_probe)
    with pytest.raises(RuntimeError, match="fixture version failure"):
        smoke.main()
    assert len(calls) == 1
    assert calls[0][0] == [str(candidate), "--version"]
    assert not Path(calls[0][1]["env"]["HOME"]).exists()


def test_version_probe_child_receives_only_the_private_fixture_environment(smoke, tmp_path, monkeypatch):
    observation = tmp_path / "probe-observation.json"
    candidate = tmp_path / "selected-version-fixture"
    candidate.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"with open({str(observation)!r}, 'w') as stream:\n"
        "    json.dump({'env': dict(os.environ), 'cwd': os.getcwd()}, stream)\n"
        "print('codex-cli fixture-only')\n"
    )
    candidate.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated-auth"))
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "TERMDECK_CODEX_ACCOUNT_SOURCE"):
        monkeypatch.setenv(key, "fixture-must-not-be-inherited")
    probe_home = tmp_path / "probe-home"
    probe_home.mkdir(mode=0o700)
    direct, version = smoke._is_direct_codex(candidate, probe_home=probe_home)
    assert direct and version == "codex-cli fixture-only"
    result = json.loads(observation.read_text())
    assert result["cwd"] == str(probe_home)
    assert result["env"]["HOME"] == str(probe_home)
    assert result["env"]["CODEX_HOME"] == str(probe_home / ".codex")
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        assert Path(result["env"][key]).is_relative_to(probe_home)
    assert result["env"]["PATH"] == os.defpath
    assert not any("must-not-be-inherited" in value for value in result["env"].values())
