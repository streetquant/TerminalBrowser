"""Operator policy upgrades use disposable npm fixtures and private state only."""
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from termdeck import launch_policy as upgrade
from termdeck.agent_launch import POLICY, _pinned_executable, policy
from termdeck.client_files import StagingError
from termdeck.cli import build_parser


VERSION = "0.159.0"
NATIVE = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    if (getenv("OPENAI_API_KEY") || getenv("LD_PRELOAD") || getenv("NODE_OPTIONS")
        || !getenv("CODEX_HOME") || strncmp(argv[0], "/proc/self/fd/", 14)) return 9;
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts("codex-cli 0.159.0"); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--help")) { puts("--no-daemon --no-alt-screen --config"); return 0; }
    return 8;
}
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(data):
    try:
        return json.loads(data)
    except (TypeError, ValueError, UnicodeError) as exc:
        pytest.fail(f"test fixture JSON is invalid: {exc}")


def write(path, value):
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)


@pytest.fixture(scope="session")
def executable(tmp_path_factory):
    root = tmp_path_factory.mktemp("policy-native-fixture")
    target = root.joinpath("native")
    result = subprocess.run(["/usr/bin/cc", "-x", "c", "-o", str(target), "-"],
                            input=NATIVE, text=True, capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    return target.read_bytes()


@pytest.fixture
def fixture(tmp_path, executable, monkeypatch):
    state = tmp_path.joinpath("state")
    state.mkdir(mode=0o700)
    source = tmp_path.joinpath("account-source")
    source.mkdir(mode=0o700)
    prefix = tmp_path.joinpath("npm")
    package = prefix.joinpath("lib/node_modules/@openai/codex")
    platform = package.joinpath("node_modules/@openai/codex-linux-x64")
    native = platform.joinpath("vendor/x86_64-unknown-linux-musl/bin/codex")
    native.parent.mkdir(parents=True)
    native.write_bytes(executable)
    native.chmod(0o700)
    entrypoint = prefix.joinpath("bin/codex")
    entrypoint.parent.mkdir(parents=True)
    entrypoint.symlink_to("../lib/node_modules/@openai/codex/bin/codex.js")
    package.joinpath("bin").mkdir()
    package.joinpath("bin/codex.js").write_text("// fixture only\n")
    write(package.joinpath("package.json"), {"name": "@openai/codex", "version": VERSION,
          "bin": {"codex": "bin/codex.js"},
          "optionalDependencies": {"@openai/codex-linux-x64": "npm:@openai/codex@" + VERSION + "-linux-x64"}})
    write(platform.joinpath("package.json"), {"name": "@openai/codex", "version": VERSION + "-linux-x64",
                                       "os": ["linux"], "cpu": ["x64"]})
    original = {"version": 1, "workspaces": [{"id": "ws_" + "1" * 32, "label": "Preserved", "path": str(tmp_path)}],
                "codex": {"executable": str(native), "sha256": "0" * 64, "account_source": str(source)}}
    write(state.joinpath(POLICY), original)
    fixture_state = {"root": tmp_path, "state": state, "native": native, "package": package,
                     "platform": platform, "prefix": prefix, "entrypoint": entrypoint,
                     "original": (state.joinpath(POLICY)).read_bytes(), "bundle": tmp_path.joinpath("bundle")}
    fixture_state["resolution"] = {"prefix": str(prefix), "package": str(package), "version": VERSION,
                                    "layout": "nested", "platform_package": str(platform / "package.json"),
                                    "native": str(native)}

    def fixture_preflight(entrypoint_arg, expected_prefix):
        value = dict(fixture_state["resolution"])
        resolved = upgrade._resolution(value, entrypoint_arg, expected_prefix)
        if Path(resolved["entrypoint"]) != fixture_state["entrypoint"]:
            raise upgrade.PolicyUpgradeError("Fixture entrypoint changed")
        parent = read_json(Path(resolved["package"]).joinpath("package.json").read_bytes())
        if (parent.get("name") != "@openai/codex" or parent.get("version") != resolved["version"]
                or parent.get("bin") != {"codex": "bin/codex.js"}
                or parent.get("optionalDependencies", {}).get("@openai/codex-linux-x64")
                   != "npm:@openai/codex@" + resolved["version"] + "-linux-x64"):
            raise upgrade.PolicyUpgradeError("Fixture main package metadata changed")
        if resolved["platform_package"] is not None:
            child = read_json(Path(resolved["platform_package"]).read_bytes())
            if (child.get("name") != "@openai/codex" or child.get("version") != resolved["version"] + "-linux-x64"
                    or child.get("os") != ["linux"] or child.get("cpu") != ["x64"]):
                raise upgrade.PolicyUpgradeError("Fixture platform package metadata changed")
        return resolved

    fixture_state["preflight"] = fixture_preflight
    monkeypatch.setattr(upgrade, "_run_npm_preflight", fixture_preflight)
    return fixture_state


def prepare(f):
    return upgrade.prepare(f["state"], digest(f["state"].joinpath(POLICY)), f["native"], VERSION, f["bundle"])


def apply(f, receipt, *, rollback=False):
    return upgrade.publish(f["state"], f["bundle"], receipt["manifest_sha256"], rollback=rollback)


def test_prepare_apply_and_exact_rollback_preserve_fields_and_private_backup(fixture, monkeypatch):
    f = fixture
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-reach-probe")
    receipt = prepare(f)
    assert receipt["policy_changed"] is False
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    assert not (f["state"].joinpath(upgrade.LOCK)).exists()
    assert (f["bundle"].joinpath("before.json")).read_bytes() == f["original"]
    assert stat.S_IMODE(f["bundle"].stat().st_mode) == 0o700
    for name in ["before.json", "after.json", "manifest.json"]:
        assert stat.S_IMODE((f["bundle"].joinpath(name)).stat().st_mode) == 0o600
    result = apply(f, receipt)
    assert result["ok"] and not result["services_restarted"] and not result["terminals_started"]
    candidate, _ = policy(f["state"])
    original = read_json(f["original"])
    assert candidate["workspaces"] == original["workspaces"]
    assert candidate["codex"]["account_source"] == original["codex"]["account_source"]
    assert hmac.compare_digest(candidate["codex"]["sha256"], digest(f["native"]))
    assert stat.S_IMODE((f["state"].joinpath(POLICY)).stat().st_mode) == 0o600
    assert apply(f, receipt, rollback=True)["ok"]
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    pause = read_json(f["state"].joinpath(upgrade.AUTO_PAUSE).read_bytes())
    assert hmac.compare_digest(pause["policy_sha256"], digest(f["state"].joinpath(POLICY)))
    assert hmac.compare_digest(pause["native_sha256"], digest(f["native"]))
    assert not list(f["state"].glob(".web-launch-policy-record-*"))
    with pytest.raises(StagingError, match="identity changed"):
        policy(f["state"])


def test_native_probe_uses_the_verified_descriptor_and_isolated_argv_env(fixture, monkeypatch):
    f = fixture
    original_run = upgrade.subprocess.run
    observed = []

    def checked_run(argv, **kwargs):
        descriptor = kwargs["pass_fds"][0]
        descriptor_path = f"/proc/self/fd/{descriptor}"
        info = os.fstat(descriptor)
        path_info = os.stat(descriptor_path)
        assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o111
        assert info.st_uid in {0, os.geteuid()} and not info.st_mode & 0o022
        assert (info.st_dev, info.st_ino) == (path_info.st_dev, path_info.st_ino)
        assert hmac.compare_digest(hashlib.sha256(os.pread(descriptor, info.st_size, 0)).hexdigest(),
                                   digest(f["native"]))
        assert argv == [descriptor_path, argv[1]]
        assert kwargs["executable"] == descriptor_path
        assert kwargs["shell"] is False and kwargs["cwd"].is_dir()
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert not {"OPENAI_API_KEY", "NODE_OPTIONS", "LD_PRELOAD"} & kwargs["env"].keys()
        assert kwargs["env"]["CODEX_HOME"].startswith(str(kwargs["cwd"]))
        observed.append((info.st_dev, info.st_ino, argv[1]))
        return original_run(argv, **kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-reach-probe")
    monkeypatch.setenv("NODE_OPTIONS", "--require /synthetic/preload.js")
    monkeypatch.setenv("LD_PRELOAD", "/synthetic/preload.so")
    monkeypatch.setattr(upgrade.subprocess, "run", checked_run)
    assert prepare(f)["policy_changed"] is False
    assert [item[2] for item in observed] == ["--version", "--help"]


def test_native_exec_pins_elf_shell_and_shebang_descriptors(tmp_path):
    executable = Path("/usr/bin/true")
    assert os.execve in os.supports_fd
    expected = digest(executable)
    with _pinned_executable([str(executable)], expected) as (descriptor, argv):
        info = os.fstat(descriptor)
        assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o111
        assert argv == [str(executable)]
    code = ("from termdeck.agent_launch import _pinned_executable\n"
            "import os\n"
            "with _pinned_executable(['/usr/bin/true']) as (fd, argv):\n"
            "    os.execve(fd, argv, {})\n")
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr

    shell = Path("/bin/bash").resolve(strict=True)
    shell_code = ("from termdeck.agent_launch import _pinned_executable\n"
                  "import os\n"
                  f"with _pinned_executable([{str(shell)!r}, '-c', 'exit 0']) as (fd, argv):\n"
                  "    os.execve(fd, argv, {'PATH': os.defpath})\n")
    shell_result = subprocess.run([sys.executable, "-B", "-c", shell_code], capture_output=True,
                                  text=True, timeout=10, check=False)
    assert shell_result.returncode == 0, shell_result.stderr

    script = tmp_path.joinpath("native-wrapper")
    output = tmp_path.joinpath("script-proof")
    script.write_text("#!/usr/bin/python3\nimport pathlib,sys\npathlib.Path(sys.argv[1]).write_text('opened pinned script')\n")
    script.chmod(0o700)
    script_code = ("from termdeck.agent_launch import _pinned_executable\n"
                   "import os\n"
                   f"with _pinned_executable([{str(script)!r}, {str(output)!r}]) as (fd, argv):\n"
                   "    os.execve(fd, argv, {})\n")
    script_result = subprocess.run([sys.executable, "-B", "-c", script_code], capture_output=True,
                                   text=True, timeout=10, check=False)
    assert script_result.returncode == 0, script_result.stderr
    assert output.read_text() == "opened pinned script"
    with pytest.raises(StagingError, match="changed before launch"):
        with _pinned_executable([str(executable)], "0" * 64):
            pytest.fail("wrong expected binary digest reached execution")


def test_official_preflight_runs_only_the_pinned_helper_descriptor(tmp_path, monkeypatch):
    helper = tmp_path.joinpath("preflight.py")
    helper.write_text("print('fixture helper payload')\n")
    helper.chmod(0o600)
    expected_helper_sha256 = digest(helper)
    prefix = tmp_path.joinpath("npm")
    native = prefix.joinpath(upgrade._NPM_LAYOUTS["nested"])
    entrypoint = prefix.joinpath("bin/codex")
    value = {"prefix": str(prefix), "package": str(prefix.joinpath(upgrade._NPM_MAIN)),
             "version": VERSION, "layout": "nested",
             "platform_package": str(native.parents[3].joinpath("package.json")), "native": str(native)}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        descriptor = kwargs["pass_fds"][0]
        assert argv[0:3] == [sys.executable, "-I", "-S"]
        assert argv[3:5] == ["-B", f"/proc/self/fd/{descriptor}"]
        assert os.stat(f"/proc/self/fd/{descriptor}").st_ino == helper.stat().st_ino
        assert kwargs["shell"] is False and kwargs["stdin"] is subprocess.DEVNULL
        assert "NODE_OPTIONS" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(upgrade, "NPM_PREFLIGHT", helper)
    monkeypatch.setattr(upgrade, "NPM_PREFLIGHT_SHA256", expected_helper_sha256)
    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    monkeypatch.setenv("NODE_OPTIONS", "--require /foreign/preload.js")
    resolved = upgrade._run_npm_preflight(entrypoint, prefix)
    assert resolved["native"] == str(native)
    assert len(calls) == 1
    helper.write_text("print('replaced helper payload')\n")
    with pytest.raises(upgrade.PolicyUpgradeError, match="preflight identity changed"):
        upgrade._run_npm_preflight(entrypoint, prefix)
    assert len(calls) == 1


def test_global_npm_upgrade_automatically_qualifies_refreshes_and_honors_rollback(fixture):
    f = fixture
    policy_path = f["state"].joinpath(POLICY)
    original_value = read_json(policy_path.read_bytes())
    original_value["codex"]["sha256"] = digest(f["native"])
    write(policy_path, original_value)
    original_policy = policy_path.read_bytes()
    initial, initial_revision = policy(f["state"])
    assert hmac.compare_digest(initial["codex"]["sha256"], digest(f["native"]))

    next_version = "0.159.1"
    main_metadata = {"name": "@openai/codex", "version": next_version,
                     "bin": {"codex": "bin/codex.js"},
                     "optionalDependencies": {"@openai/codex-linux-x64":
                                               "npm:@openai/codex@" + next_version + "-linux-x64"}}
    write(f["package"].joinpath("package.json"), main_metadata)
    updated_native = f["root"].joinpath("updated-codex")
    result = subprocess.run(["/usr/bin/cc", "-x", "c", "-o", str(updated_native), "-"],
                            input=NATIVE.replace(VERSION, next_version), text=True,
                            capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    updated_native.chmod(0o700)
    os.replace(updated_native, f["native"])
    with pytest.raises(StagingError, match="identity changed"):
        policy(f["state"])
    assert policy_path.read_bytes() == original_policy
    incomplete = list(f["state"].glob(".web-launch-policy-auto-*"))
    assert not incomplete  # The official resolver rejects inconsistent metadata before staging.

    platform_metadata = {"name": "@openai/codex", "version": next_version + "-linux-x64",
                         "os": ["linux"], "cpu": ["x64"]}
    write(f["platform"].joinpath("package.json"), platform_metadata)
    f["resolution"]["version"] = next_version
    refreshed, refresh_revision = policy(f["state"])
    completed = [bundle for bundle in f["state"].glob(".web-launch-policy-auto-*")
                 if (bundle / "manifest.json").is_file()]
    assert len(completed) == 1
    bundle = completed[0]
    manifest = read_json(bundle.joinpath("manifest.json").read_bytes())
    assert manifest["automation"] == "launch-time"
    assert manifest["native"]["version"] == next_version
    assert hmac.compare_digest(manifest["native"]["sha256"], digest(f["native"]))
    assert hmac.compare_digest(refreshed["codex"]["sha256"], digest(f["native"]))
    assert not policy_path.read_bytes() == original_policy
    assert refresh_revision != initial_revision
    original = read_json(original_policy)
    assert refreshed["codex"]["executable"] == original["codex"]["executable"]
    assert refreshed["codex"]["account_source"] == original["codex"]["account_source"]
    assert refreshed["workspaces"] == original["workspaces"]

    rollback_receipt = {"manifest_sha256": digest(bundle.joinpath("manifest.json"))}
    rollback = upgrade.publish(f["state"], bundle, rollback_receipt["manifest_sha256"], rollback=True)
    assert rollback["ok"] and not rollback["services_restarted"] and not rollback["terminals_started"]
    assert policy_path.read_bytes() == original_policy
    with pytest.raises(StagingError, match="identity changed"):
        policy(f["state"])

    later_version = "0.159.2"
    write(f["package"].joinpath("package.json"), {
        "name": "@openai/codex", "version": later_version,
        "bin": {"codex": "bin/codex.js"},
        "optionalDependencies": {"@openai/codex-linux-x64":
                                  "npm:@openai/codex@" + later_version + "-linux-x64"}})
    write(f["platform"].joinpath("package.json"), {
        "name": "@openai/codex", "version": later_version + "-linux-x64",
        "os": ["linux"], "cpu": ["x64"]})
    f["resolution"]["version"] = later_version
    later_native = f["root"].joinpath("later-codex")
    later_result = subprocess.run(["/usr/bin/cc", "-x", "c", "-o", str(later_native), "-"],
                                  input=NATIVE.replace(VERSION, later_version), text=True,
                                  capture_output=True, timeout=20, check=False)
    assert later_result.returncode == 0, later_result.stderr
    later_native.chmod(0o700)
    os.replace(later_native, f["native"])
    later, _ = policy(f["state"])
    assert hmac.compare_digest(later["codex"]["sha256"], digest(f["native"]))
    assert later["codex"]["account_source"] == original["codex"]["account_source"]
    assert later["workspaces"] == original["workspaces"]


@pytest.mark.parametrize("layout", ["flat", "bundled"])
@pytest.mark.parametrize("remove_old", [False, True], ids=["coexisting-orphan", "removed-old-path"])
def test_policy_tracks_current_same_prefix_npm_layout_and_preserves_rollback(fixture, layout, remove_old):
    f = fixture
    policy_path = f["state"].joinpath(POLICY)
    original_value = read_json(policy_path.read_bytes())
    original_value["codex"]["sha256"] = digest(f["native"])
    write(policy_path, original_value)
    original_policy = policy_path.read_bytes()
    old_native = f["native"]
    current_native = f["prefix"].joinpath(upgrade._NPM_LAYOUTS[layout])
    current_native.parent.mkdir(parents=True)
    current_native.write_bytes(old_native.read_bytes())
    current_native.chmod(0o700)
    if layout == "flat":
        platform_root = current_native.parents[3]
        platform_root.mkdir(parents=True, exist_ok=True)
        write(platform_root.joinpath("package.json"), {"name": "@openai/codex", "version": VERSION + "-linux-x64",
              "os": ["linux"], "cpu": ["x64"]})
        platform_package = str(platform_root.joinpath("package.json"))
    else:
        platform_package = None
    f["resolution"] = {"prefix": str(f["prefix"]), "package": str(f["package"]), "version": VERSION,
                        "layout": layout, "platform_package": platform_package,
                        "native": str(current_native)}
    if remove_old:
        old_native.unlink()

    refreshed, _ = policy(f["state"])
    assert refreshed["codex"]["executable"] == str(current_native)
    assert hmac.compare_digest(refreshed["codex"]["sha256"], digest(current_native))
    assert refreshed["codex"]["account_source"] == original_value["codex"]["account_source"]
    assert refreshed["workspaces"] == original_value["workspaces"]
    if not remove_old:
        assert old_native.is_file()  # The old nested runtime may coexist as an orphan.
    bundles = [bundle for bundle in f["state"].glob(".web-launch-policy-auto-*")
               if (bundle / "manifest.json").is_file()]
    assert len(bundles) == 1
    manifest = read_json(bundles[0].joinpath("manifest.json").read_bytes())
    assert manifest["native"]["layout"] == layout
    assert manifest["native"]["entrypoint"] == str(f["entrypoint"])
    assert manifest["native"]["prefix"] == str(f["prefix"])
    assert manifest["native"]["preflight_sha256"] == upgrade.NPM_PREFLIGHT_SHA256

    rolled_back = upgrade.publish(f["state"], bundles[0], digest(bundles[0].joinpath("manifest.json")),
                                  rollback=True)
    assert rolled_back["ok"] and policy_path.read_bytes() == original_policy
    with pytest.raises(StagingError, match="identity changed"):
        policy(f["state"])
    assert policy_path.read_bytes() == original_policy


@pytest.mark.parametrize("foreign_field", ["prefix", "native"])
def test_current_npm_resolution_rejects_foreign_ancestor_without_policy_mutation(fixture, foreign_field):
    f = fixture
    policy_path = f["state"].joinpath(POLICY)
    current = read_json(policy_path.read_bytes())
    current["codex"]["sha256"] = digest(f["native"])
    write(policy_path, current)
    before = policy_path.read_bytes()
    foreign = f["root"].joinpath("foreign-prefix")
    if foreign_field == "prefix":
        f["resolution"]["prefix"] = str(foreign)
    else:
        f["resolution"]["native"] = str(foreign / upgrade._NPM_LAYOUTS["flat"])
    with pytest.raises(StagingError, match="identity changed"):
        policy(f["state"])
    assert policy_path.read_bytes() == before
    assert not list(f["state"].glob(".web-launch-policy-auto-*/manifest.json"))


def test_owned_publicly_readable_npm_directories_preserve_nofollow_revalidation(fixture):
    f = fixture
    for directory in [f["native"].parent, f["platform"], f["package"]]:
        directory.chmod(0o755)
    receipt = prepare(f)
    assert apply(f, receipt)["ok"]
    assert apply(f, receipt, rollback=True)["ok"]
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]


@pytest.mark.parametrize("mutation", ["wrong-version", "platform-version", "dependency", "platform-cpu", "native-symlink",
                                      "ancestor-symlink", "native-hardlink", "native-writable", "native-script", "package-symlink"])
def test_bad_native_identity_never_publishes(fixture, mutation):
    f = fixture
    if mutation in {"wrong-version", "dependency"}:
        p = f["package"].joinpath("package.json")
        value = read_json(p.read_bytes())
        if mutation == "wrong-version": value["version"] = "0.158.0"
        else: value["optionalDependencies"]["@openai/codex-linux-x64"] = "unqualified"
        write(p, value)
    elif mutation in {"platform-version", "platform-cpu"}:
        p = f["platform"].joinpath("package.json")
        value = read_json(p.read_bytes())
        value["version" if mutation == "platform-version" else "cpu"] = "wrong" if mutation == "platform-version" else ["arm64"]
        write(p, value)
    elif mutation in {"native-symlink", "package-symlink"}:
        p = f["native"] if mutation == "native-symlink" else f["package"].joinpath("package.json")
        saved = p.with_name(p.name + ".saved")
        p.rename(saved)
        p.symlink_to(saved)
    elif mutation == "ancestor-symlink":
        parent = f["native"].parent
        saved = parent.with_name("bin.saved")
        parent.rename(saved)
        parent.symlink_to(saved, target_is_directory=True)
    elif mutation == "native-hardlink": os.link(f["native"], f["root"].joinpath("other-link"))
    elif mutation == "native-writable":
        # The disposable negative control deliberately grants unsafe permissions.
        f["native"].chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)  # ubs:ignore[py.security.file-permissions] -- test fixture verifies this mode is rejected
    else: write(f["native"], b"#!/bin/sh\nexit 0\n")
    with pytest.raises((upgrade.PolicyUpgradeError, OSError, upgrade.StagingError)):
        prepare(f)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]


@pytest.mark.parametrize("mutation", ["digest", "policy-symlink", "policy-hardlink", "policy-mode", "duplicate-json", "state-symlink"])
def test_bad_preimage_or_storage_refused(fixture, mutation):
    f = fixture
    p = f["state"].joinpath(POLICY)
    expected = digest(p)
    if mutation == "digest": expected = "f" * 64
    elif mutation == "policy-symlink":
        saved = f["root"].joinpath("original")
        p.rename(saved)
        p.symlink_to(saved)
    elif mutation == "policy-hardlink": os.link(p, f["root"].joinpath("other-link"))
    elif mutation == "policy-mode": p.chmod(0o644)
    elif mutation == "duplicate-json":
        write(p, b'{"version":1,"version":1,"codex":null,"workspaces":[]}')
        expected = digest(p)
    else:
        saved = f["root"].joinpath("state.saved")
        f["state"].rename(saved)
        f["state"].symlink_to(saved, target_is_directory=True)
    with pytest.raises((upgrade.PolicyUpgradeError, OSError, upgrade.StagingError)):
        upgrade.prepare(f["state"], expected, f["native"], VERSION, f["bundle"])
    assert not f["bundle"].exists()


@pytest.mark.parametrize("mutation", ["old-policy", "same-bytes-new-inode", "native", "package", "manifest", "backup", "candidate", "validation"])
def test_apply_rejects_drift_and_preserves_current_policy(fixture, mutation):
    f = fixture
    receipt = prepare(f)
    if mutation == "old-policy": write(f["state"].joinpath(POLICY), b"changed by another owner action")
    elif mutation == "same-bytes-new-inode":
        temporary = f["state"].joinpath("new")
        write(temporary, f["original"])
        temporary.replace(f["state"].joinpath(POLICY))
    elif mutation == "native":
        with f["native"].open("ab") as handle: handle.write(b"changed")
    elif mutation == "package":
        p = f["package"].joinpath("package.json")
        write(p, p.read_bytes() + b"\n")
    else:
        name = {"manifest": "manifest.json", "backup": "before.json", "candidate": "after.json",
                "validation": "validation/" + POLICY}[mutation]
        p = f["bundle"].joinpath(name)
        write(p, p.read_bytes() + b"\n")
    current = (f["state"].joinpath(POLICY)).read_bytes()
    with pytest.raises(upgrade.PolicyUpgradeError): apply(f, receipt)
    assert (f["state"].joinpath(POLICY)).read_bytes() == current


def test_native_lock_rejects_competing_process_then_allows_retry(fixture):
    f = fixture
    receipt = prepare(f)
    with os.fdopen(os.open(f["state"].joinpath(upgrade.LOCK), os.O_CREAT | os.O_RDWR, 0o600), "r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(upgrade.PolicyUpgradeError, match="holds the lock"): apply(f, receipt)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    assert apply(f, receipt)["ok"]


def test_last_prepublication_drift_is_not_overwritten(fixture, monkeypatch):
    f = fixture
    receipt = prepare(f)
    write_new = upgrade._write_new
    changed = b"concurrent new policy"
    def race(tree, name, data):
        write_new(tree, name, data)
        if name.startswith(".web-launch-policy-"): write(f["state"].joinpath(POLICY), changed)
    monkeypatch.setattr(upgrade, "_write_new", race)
    with pytest.raises(upgrade.PolicyUpgradeError, match="compare-and-swap"): apply(f, receipt)
    assert (f["state"].joinpath(POLICY)).read_bytes() == changed
    assert not list(f["state"].glob(".web-launch-policy-*"))


def test_failed_atomic_publication_preserves_old_policy_and_backup(fixture, monkeypatch):
    f = fixture
    receipt = prepare(f)
    def failed(*args, **kwargs): raise OSError("injected atomic publish failure")
    monkeypatch.setattr(upgrade.os, "replace", failed)
    with pytest.raises(OSError, match="injected"): apply(f, receipt)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    assert (f["bundle"].joinpath("before.json")).read_bytes() == f["original"]


def test_rollback_refuses_neighbor_changes(fixture):
    f = fixture
    receipt = prepare(f)
    apply(f, receipt)
    value = read_json((f["state"].joinpath(POLICY)).read_bytes())
    value["workspaces"][0]["label"] = "New operator choice"
    write(f["state"].joinpath(POLICY), value)
    changed = (f["state"].joinpath(POLICY)).read_bytes()
    with pytest.raises(upgrade.PolicyUpgradeError, match="preimage"): apply(f, receipt, rollback=True)
    assert (f["state"].joinpath(POLICY)).read_bytes() == changed


def test_cli_requires_explicit_review_inputs_and_has_no_session_database_side_effect(fixture, capsys):
    f = fixture
    parser = build_parser()
    args = parser.parse_args(["launch-policy", "prepare", "--state-dir", str(f["state"]),
                             "--expected-policy-sha256", digest(f["state"].joinpath(POLICY)),
                             "--native", str(f["native"]), "--version", VERSION, "--output", str(f["bundle"])])
    assert args._uses_session_db is False
    assert args.func(args) == 0
    receipt = read_json(capsys.readouterr().out)
    assert receipt["ok"] and not receipt["policy_changed"]
    assert not (f["state"].joinpath("sessions.sqlite3")).exists()
    with pytest.raises(SystemExit): parser.parse_args(["launch-policy", "apply", "--state-dir", str(f["state"])])


@pytest.mark.parametrize("source, message", [
    (NATIVE.replace("codex-cli 0.159.0", "codex-cli 0.158.0"), "Live native version"),
    (NATIVE.replace("--no-daemon --no-alt-screen --config", "--no-daemonish --no-alt-screen --config"), "required private-launch"),
    (NATIVE.replace("puts(\"codex-cli 0.159.0\"); return 0", "return 7"), "probe failed"),
    (NATIVE.replace("puts(\"codex-cli 0.159.0\"); return 0", "for (int i=0;i<140000;i++) putchar('x'); return 0"), "output bound"),
])
def test_actual_probe_rejects_wrong_live_version_flags_failure_and_output(fixture, source, message):
    f = fixture
    result = subprocess.run(["/usr/bin/cc", "-x", "c", "-o", str(f["native"]), "-"],
                            input=source, text=True, capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    with pytest.raises(upgrade.PolicyUpgradeError, match=message):
        prepare(f)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    assert not (f["bundle"].joinpath("manifest.json")).exists()


@pytest.mark.parametrize("changed", ["native", "policy"])
def test_drift_during_real_probe_never_produces_review_manifest(fixture, monkeypatch, changed):
    f = fixture
    original_probe = upgrade._probe
    def probe(*args):
        result = original_probe(*args)
        if changed == "native":
            with f["native"].open("ab") as stream:
                stream.write(b"changed after help")
        else:
            write(f["state"].joinpath(POLICY), b"new operator policy")
        return result
    monkeypatch.setattr(upgrade, "_probe", probe)
    with pytest.raises(upgrade.PolicyUpgradeError, match="changed"):
        prepare(f)
    expected = f["original"] if changed == "native" else b"new operator policy"
    assert (f["state"].joinpath(POLICY)).read_bytes() == expected
    assert not (f["bundle"].joinpath("manifest.json")).exists()


def test_foreign_native_owner_is_rejected_before_probe(fixture, monkeypatch):
    f = fixture
    native_identity = f["native"].stat()
    actual_fstat = upgrade.os.fstat
    def foreign_owner(fd):
        info = actual_fstat(fd)
        if (info.st_dev, info.st_ino) == (native_identity.st_dev, native_identity.st_ino):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid()+1, st_nlink=info.st_nlink)
        return info
    monkeypatch.setattr(upgrade.os, "fstat", foreign_owner)
    with pytest.raises(upgrade.PolicyUpgradeError, match="must be owned"):
        prepare(f)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]


def test_process_lock_blocks_independent_cli_and_retry_rolls_back(fixture):
    f = fixture
    receipt = prepare(f)
    argv = [sys.executable, "-B", "-m", "termdeck.cli", "launch-policy", "apply",
            "--state-dir", str(f["state"]), "--bundle", str(f["bundle"]),
            "--manifest-sha256", receipt["manifest_sha256"]]
    with os.fdopen(os.open(f["state"].joinpath(upgrade.LOCK), os.O_CREAT | os.O_RDWR, 0o600), "r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    assert blocked.returncode == 2
    assert "holds the lock" in read_json(blocked.stdout)["error"]
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
    published = apply(f, receipt)
    assert published["ok"]
    restored = apply(f, receipt, rollback=True)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]


@pytest.mark.parametrize("after_replace", [False, True])
def test_fsync_failure_reports_failure_and_keeps_exact_rollback_evidence(fixture, monkeypatch, after_replace):
    f = fixture
    receipt = prepare(f)
    original_fsync, original_replace = upgrade.os.fsync, upgrade.os.replace
    published = False
    def replace(*args, **kwargs):
        nonlocal published
        original_replace(*args, **kwargs)
        published = True
    def fsync(fd):
        if (after_replace and published) or (not after_replace and stat.S_ISREG(os.fstat(fd).st_mode)):
            raise OSError("injected durability failure")
        original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(upgrade.os, "replace", replace)
        patch.setattr(upgrade.os, "fsync", fsync)
        with pytest.raises(OSError, match="durability"):
            apply(f, receipt)
    assert (f["bundle"].joinpath("before.json")).read_bytes() == f["original"]
    if after_replace:
        assert (f["state"].joinpath(POLICY)).read_bytes() == (f["bundle"].joinpath("after.json")).read_bytes()
        assert apply(f, receipt, rollback=True)["ok"]
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]


@pytest.mark.parametrize("changed", ["account_source", "workspaces"])
def test_even_a_rehashed_manifest_cannot_change_unrelated_fields(fixture, changed):
    f = fixture
    receipt = prepare(f)
    candidate_path = f["bundle"].joinpath("after.json")
    candidate = read_json(candidate_path.read_bytes())
    if changed == "account_source":
        candidate["codex"][changed] = str(f["root"])
    else:
        candidate[changed] = []
    write(candidate_path, candidate)
    manifest_path = f["bundle"].joinpath("manifest.json")
    manifest = read_json(manifest_path.read_bytes())
    manifest["after_sha256"] = digest(candidate_path)
    write(manifest_path, manifest)
    receipt["manifest_sha256"] = digest(manifest_path)
    with pytest.raises(upgrade.PolicyUpgradeError, match="unrelated settings"):
        apply(f, receipt)
    assert (f["state"].joinpath(POLICY)).read_bytes() == f["original"]
