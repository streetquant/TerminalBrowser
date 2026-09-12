from __future__ import annotations

import ast
import os
from pathlib import Path
import stat

import pytest

from scripts.smoke_support import require_private_validation_runtime


REPO_ROOT = Path(__file__).resolve().parents[1]


def _temporary_directory_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tempfile"
        and node.func.attr == "TemporaryDirectory"
    ]


@pytest.mark.parametrize(
    "script_name",
    (
        "check_clipboard_paste_smoke.py",
        "check_contained_resize_smoke.py",
        "check_generic_session_recovery_smoke.py",
        "check_hidden_recipe_geometry_smoke.py",
        "check_immediate_new_tab_smoke.py",
        "check_shortcut_lifecycle_smoke.py",
        "check_startup_geometry_smoke.py",
        "check_tab_manager_move_smoke.py",
        "check_tab_strip_smoke.py",
        "check_tabs_pane_smoke.py",
        "check_terminal_scroll_smoke.py",
        "check_terminal_select_copy_smoke.py",
        "check_window_restore_smoke.py",
    ),
)
def test_tmux_smoke_sockets_use_private_validation_runtime(script_name: str) -> None:
    source = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    calls = _temporary_directory_calls(source)

    assert "require_private_validation_runtime" in source
    assert not any(
        isinstance(node, ast.Constant) and node.value == "/tmp"
        for node in ast.walk(ast.parse(source))
    )
    assert any(
        any(
            keyword.arg == "dir"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "runtime_base"
            for keyword in call.keywords
        )
        for call in calls
    )


def test_private_validation_runtime_requires_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("T1000_VALIDATION_RUNTIME_BASE", raising=False)
    monkeypatch.delenv("T1000_VALIDATION_RUNTIME_POLICY", raising=False)

    with pytest.raises(RuntimeError, match="refusing a /tmp fallback"):
        require_private_validation_runtime()


@pytest.mark.parametrize(
    "value, pattern",
    (
        ("relative-runtime", "must be an absolute path"),
        ("/path/that/does/not/exist", "is unavailable"),
    ),
)
def test_private_validation_runtime_rejects_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    pattern: str,
) -> None:
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", value)

    with pytest.raises(RuntimeError, match=pattern):
        require_private_validation_runtime()


def test_private_validation_runtime_rejects_nonprivate_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime.chmod(0o755)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))

    with pytest.raises(RuntimeError, match="private 0700 directory"):
        require_private_validation_runtime()


def test_private_validation_runtime_rejects_symlink_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    link = tmp_path / "runtime-link"
    link.symlink_to(runtime, target_is_directory=True)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(link))

    with pytest.raises(RuntimeError, match="private 0700 directory"):
        require_private_validation_runtime()


def test_private_validation_runtime_rejects_external_temp_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))
    import scripts.smoke_support as smoke_support

    monkeypatch.setattr(smoke_support, "_VALIDATION_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(smoke_support.tempfile, "gettempdir", lambda: "/tmp")

    with pytest.raises(RuntimeError, match="outside the private validation runtime"):
        require_private_validation_runtime()


def test_private_validation_runtime_requires_explicit_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))
    monkeypatch.delenv("T1000_VALIDATION_RUNTIME_POLICY", raising=False)
    import scripts.smoke_support as smoke_support

    monkeypatch.setattr(smoke_support, "_VALIDATION_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(smoke_support.tempfile, "gettempdir", lambda: str(runtime))

    with pytest.raises(RuntimeError, match="refusing to infer CI portability"):
        require_private_validation_runtime()


def test_ci_private_validation_runtime_does_not_require_warp_drives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    temporary = runtime / "t"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    temporary.mkdir(mode=0o700)
    temporary.chmod(0o700)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_POLICY", "ci")
    import scripts.smoke_support as smoke_support

    monkeypatch.setattr(smoke_support, "_VALIDATION_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(smoke_support.tempfile, "gettempdir", lambda: str(temporary))

    import termdeck.storage_fabric as storage_fabric

    def reject_storage(**_kwargs: object) -> None:
        raise AssertionError("CI must not require Warp Drives")

    monkeypatch.setattr(storage_fabric, "attest_storage_fabric", reject_storage)

    assert require_private_validation_runtime() == runtime


def test_private_validation_runtime_requires_verified_ssd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_POLICY", "production")
    import scripts.smoke_support as smoke_support

    monkeypatch.setattr(smoke_support, "_VALIDATION_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(smoke_support.tempfile, "gettempdir", lambda: str(runtime))

    import termdeck.storage_fabric as storage_fabric

    class RejectedAttestation:
        def require(self) -> object:
            raise RuntimeError("rotational backing device")

    def reject_storage(**_kwargs: object) -> RejectedAttestation:
        return RejectedAttestation()

    monkeypatch.setattr(storage_fabric, "attest_storage_fabric", reject_storage)

    with pytest.raises(RuntimeError, match="not verified Warp Drives SSD storage"):
        require_private_validation_runtime()


def test_private_validation_runtime_returns_verified_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    temporary = runtime / "t"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    temporary.mkdir(mode=0o700)
    temporary.chmod(0o700)
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_BASE", str(runtime))
    monkeypatch.setenv("T1000_VALIDATION_RUNTIME_POLICY", "production")
    import scripts.smoke_support as smoke_support

    monkeypatch.setattr(smoke_support, "_VALIDATION_RUNTIME_MAX_PATH_BYTES", 4096)
    monkeypatch.setattr(smoke_support.tempfile, "gettempdir", lambda: str(temporary))

    import termdeck.storage_fabric as storage_fabric

    observed: dict[str, object] = {}

    class AcceptedAttestation:
        def require(self) -> object:
            return self

    def accept_storage(**kwargs: object) -> AcceptedAttestation:
        observed.update(kwargs)
        return AcceptedAttestation()

    monkeypatch.setattr(storage_fabric, "attest_storage_fabric", accept_storage)

    assert require_private_validation_runtime() == runtime
    assert observed["managed_root"] == runtime
    assert observed["production"] is True
    assert observed["hot_paths"] == {
        "auth_root": runtime,
        "checkpoint_root": temporary,
        "database_root": runtime,
        "runtime_root": temporary,
        "tmux_runtime": runtime,
    }
    metadata = runtime.lstat()
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700


def test_contained_resize_tmux_root_uses_private_validation_runtime() -> None:
    source = (REPO_ROOT / "scripts" / "check_contained_resize_smoke.py").read_text(
        encoding="utf-8"
    )

    assert 'environment["TMUX_TMPDIR"] = str(runtime_base)' in source
    assert "tempfile.mkdtemp" not in source


def test_codex_auth_selector_preserves_runner_attested_tmux_runtime() -> None:
    source = (
        REPO_ROOT / "scripts" / "check_codex_auth_selector_smoke.py"
    ).read_text(encoding="utf-8")

    assert "require_private_validation_runtime" in source
    assert "/dev/shm" not in source
    assert 'os.environ.get("TMUX_TMPDIR") != str(runtime_base)' in source
    assert 'os.environ["TMUX_TMPDIR"] =' not in source


def test_window_restore_uses_explicit_fixture_launcher_attestation() -> None:
    source = (REPO_ROOT / "scripts" / "check_window_restore_smoke.py").read_text(
        encoding="utf-8"
    )

    assert "_install_fixture_launcher_attestation(fake_codex)" in source
    assert "launcher_attestor=attest_fixture" in source
