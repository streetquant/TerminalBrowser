import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from termdeck.client_files import StagingError
from termdeck import server_workspaces as workspaces
from termdeck import agent_launch as launch, client_files as cf
from test_agent_launch import Backend
from test_client_files import stage, call, upload, path, make_stage, ordinary_workspace_root  # noqa: F401


@pytest.fixture
def directories(ordinary_workspace_root):
    root = ordinary_workspace_root / "workspace-tree"
    root.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    workspace = root / "ordinary project with spaces"
    workspace.mkdir(mode=0o770)
    return state, workspace


def test_directory_selection_preserves_spaces_and_v2_identity(directories):
    state, workspace = directories
    result = workspaces.select(str(workspace), state_root=state)
    assert result["path"] == str(workspace)
    assert result["source"] == "server-directory"
    assert set(result["pin"]) == {"version", "chain"}
    assert result["pin"]["version"] == 2
    assert result["pin"]["chain"][-1] == [workspace.stat().st_dev, workspace.stat().st_ino]
    with workspaces.open_workspace(result) as directory:
        directory.check()


def test_initial_symlink_and_parent_syntax_resolve_to_canonical_directory(directories):
    state, workspace = directories
    alias = workspace.with_name("shortcut")
    alias.symlink_to(workspace, target_is_directory=True)
    for selected in (alias, alias / ".." / workspace.name):
        result = workspaces.select(str(selected), state_root=state)
        assert result["path"] == str(workspace)


@pytest.mark.parametrize("mode", [0o100, 0o500, 0o770, 0o777])
def test_searchable_directory_needs_no_read_or_owner_only_permissions(directories, mode):
    state, workspace = directories
    workspace.chmod(mode)
    previous = Path.cwd()
    try:
        result = workspaces.select(str(workspace), state_root=state)
        with workspaces.open_workspace(result) as directory, directory.entered():
            assert Path.cwd() == workspace
        assert Path.cwd() == previous
        # Permission changes remain governed by the OS, not an old read-only grant.
        workspace.chmod(0o700)
        with workspaces.open_workspace(result) as directory, directory.entered():
            Path("new-file").write_text("ordinary host access")
    finally:
        workspace.chmod(0o700)


@pytest.mark.parametrize("selected", ["relative", "../escape", ""])
def test_nonabsolute_paths_rejected(directories, selected):
    state, _ = directories
    with pytest.raises(StagingError):
        workspaces.select(selected, state_root=state)


@pytest.mark.parametrize("selected", ["/", "/proc", "/dev", "/sys", "/run"])
def test_os_searchable_system_directories_are_selectable(directories, selected):
    state, _ = directories
    receipt = workspaces.select(selected, state_root=state)
    with workspaces.open_workspace(receipt) as directory, directory.entered():
        assert Path.cwd() == Path(selected).resolve()
    if selected == "/":
        assert receipt["label"] == "/"


@pytest.mark.parametrize("kind", ["missing", "file", "fifo", "socket", "unsearchable"])
def test_missing_non_directory_and_os_inaccessible_objects_rejected(directories, kind):
    state, workspace = directories
    selected = workspace / "selection"
    control = None
    if kind == "file":
        selected.write_text("plain file")
    elif kind == "fifo":
        os.mkfifo(selected)
    elif kind == "socket":
        control = socket.socket(socket.AF_UNIX)
        control.bind(str(selected))
    elif kind == "unsearchable":
        selected.mkdir(mode=0o000)
    try:
        if kind == "unsearchable" and os.geteuid() == 0:
            # Root really can enter it; this feature must not invent restrictions.
            workspaces.select(str(selected), state_root=state)
        else:
            with pytest.raises(StagingError):
                workspaces.select(str(selected), state_root=state)
    finally:
        if control:
            control.close()
        if kind == "unsearchable":
            selected.chmod(0o700)


@pytest.mark.parametrize("relative", [".termdeck", ".termdeck/tls", ".beads", "terminal-auth", ".ssh"])
def test_host_user_can_select_previously_masked_names(directories, relative):
    state, workspace = directories
    selected = workspace / relative
    selected.mkdir(parents=True)
    receipt = workspaces.select(str(selected), state_root=state, protected=(selected,))
    assert receipt["path"] == str(selected)


def test_selection_does_not_walk_or_mask_descendants(directories, monkeypatch):
    state, workspace = directories
    (workspace / "unsearchable").mkdir(mode=0o000)
    (workspace / "alias").symlink_to(state, target_is_directory=True)
    def unexpected(*_args, **_kwargs):
        pytest.fail("Cwd selection must not recursively inventory the host")
    monkeypatch.setattr(os, "scandir", unexpected)
    try:
        result = workspaces.select(str(workspace), state_root=state, protected=(state,))
        with workspaces.open_workspace(result) as directory:
            directory.check()
    finally:
        (workspace / "unsearchable").chmod(0o700)


@pytest.mark.parametrize("replacement", ["directory", "symlink", "ancestor"])
def test_receipt_rejects_replaced_directory_and_ancestor(directories, replacement):
    state, workspace = directories
    if replacement == "ancestor":
        workspace = workspace / "project"
        workspace.mkdir()
    result = workspaces.select(str(workspace), state_root=state)
    changed = workspace.parent if replacement == "ancestor" else workspace
    moved = changed.with_name(changed.name + "-original")
    changed.rename(moved)
    if replacement == "symlink":
        changed.symlink_to(moved, target_is_directory=True)
    else:
        changed.mkdir()
        if replacement == "ancestor":
            (changed / workspace.name).mkdir()
    with pytest.raises(StagingError, match="changed"):
        with workspaces.open_workspace(result):
            pytest.fail("Replacement was accepted")


def test_replacement_at_fchdir_fails_before_exec_and_restores_cwd(directories, monkeypatch):
    state, workspace = directories
    result = workspaces.select(str(workspace), state_root=state)
    previous = Path.cwd()
    real_fchdir = os.fchdir
    with workspaces.open_workspace(result) as directory:
        def raced(fd):
            if fd == directory.fd:
                workspace.rename(workspace.with_name("original"))
                workspace.mkdir()
            real_fchdir(fd)
        monkeypatch.setattr(os, "fchdir", raced)
        with pytest.raises(StagingError, match="identity changed"):
            with directory.entered():
                pytest.fail("Replacement race reached execution")
    assert Path.cwd() == previous


def test_legacy_directory_pin_never_becomes_host_authority(directories):
    state, workspace = directories
    result = workspaces.select(str(workspace), state_root=state)
    result["pin"].update(version=1, read_only=False)
    with pytest.raises(StagingError, match="new host-user launch"):
        with workspaces.open_workspace(result):
            pytest.fail("Legacy mount grant became host authority")


def creation(store, actor, workspace, **extra):
    options = call((store, actor), "launch-options")
    assert options["server_directory"]
    return {"operation": "create-window", "request_id": uuid.uuid4().hex,
            "policy_revision": options["policy_revision"], "name": "Any server directory",
            "workspace_path": str(workspace), "runtime": "shell", "tabs": ["One"],
            "attachments": [], **extra}


def test_unregistered_selection_persists_and_replays_across_store_restart(stage, ordinary_workspace_root):
    store, actor = stage
    workspace = ordinary_workspace_root / "project with spaces"
    workspace.mkdir(mode=0o770)
    request = creation(store, actor, workspace)
    receipt = store.handle(actor, request)
    reopened = cf.StagingStore(store.root.parent, store.auth_directory, session_database=store.session_database)
    repeated = reopened.handle(actor, request)
    assert repeated["launch_id"] == receipt["launch_id"]
    backend = Backend()
    assert launch.start(reopened, actor, repeated, backend=backend)["status"] == "started"
    assert launch.start(reopened, actor, receipt, backend=backend)["status"] == "started"
    assert len(backend.records) == 1
    assert backend.records[0].cwd == str(workspace)
    assert not (store.root.parent / launch.POLICY).exists()
    with pytest.raises(StagingError, match="different content"):
        reopened.handle(actor, dict(request, workspace_path=str(workspace.parent)))


@pytest.mark.parametrize("phase", ["start", "execute"])
def test_launch_revalidates_explicit_directory_before_registry_or_process(stage, phase, monkeypatch, ordinary_workspace_root):
    store, actor = stage
    workspace = ordinary_workspace_root / "project"
    workspace.mkdir()
    receipt = store.handle(actor, creation(store, actor, workspace))
    backend = Backend()
    if phase == "execute":
        launch.start(store, actor, receipt, backend=backend)
    workspace.rename(workspace.with_name(workspace.name + "-original"))
    workspace.mkdir()
    def unexpected(*_args):
        raise AssertionError("Replaced directory must not reach a process")
    monkeypatch.setattr(os, "execve", unexpected)
    with pytest.raises(StagingError, match="identity changed"):
        if phase == "start":
            launch.start(store, actor, receipt, backend=backend)
        else:
            launch.execute(SimpleNamespace(state=str(store.root.parent), auth_dir=str(store.auth_directory),
                           db=str(store.session_database), launch=receipt["launch_id"], tab=receipt["tabs"][0]))
    if phase == "start":
        assert not backend.records and not store.session_database.exists()


def test_directory_request_rejects_ambiguous_authority_and_client_display_metadata(stage, ordinary_workspace_root):
    store, actor = stage
    workspace = ordinary_workspace_root / "project"
    workspace.mkdir()
    with pytest.raises(StagingError, match="Choose one"):
        store.handle(actor, creation(store, actor, workspace, workspace_id="ws_" + "a" * 32))
    obj = upload(stage, display_path=str(workspace))
    request = creation(store, actor, workspace, attachments=[obj["reference"]])
    request.pop("workspace_path")
    with pytest.raises(StagingError, match="Choose one"):
        store.handle(actor, request)


def test_broad_launch_preserves_api_attachment_bytes_with_normal_host_visibility(ordinary_workspace_root, monkeypatch):
    (ordinary_workspace_root / "state").mkdir(mode=0o700)
    store, actor = make_stage(ordinary_workspace_root / "state", ordinary_workspace_root / "auth")
    project = ordinary_workspace_root / "project with spaces"
    project.mkdir()
    (project / ".beads").mkdir()
    (project / ".beads/private").write_text("disposable host fixture")
    obj = upload((store, actor), b"original immutable bytes", display_path="/client/metadata-only")
    receipt = store.handle(actor, creation(store, actor, ordinary_workspace_root, attachments=[obj["reference"]]))
    launch.start(store, actor, receipt, backend=Backend())
    upload((store, actor), b"newer", previous=obj["reference"])
    code = """import pathlib,sys
attachment=pathlib.Path(sys.argv[1]); root=pathlib.Path.cwd()
assert attachment.read_bytes()==b'original immutable bytes'
assert (root/'auth/termdeck-web-auth-v1.sqlite3').exists()
assert (root/'state/client-files/metadata.sqlite3').exists()
assert (root/'project with spaces/.beads/private').read_text()=='disposable host fixture'
(root/'project with spaces/proof').write_text('host user')
"""
    completed = []
    class Executed(Exception):
        pass
    def execute(executable, argv, environment):
        assert executable != "/usr/bin/bwrap"
        completed.append(subprocess.run([sys.executable, "-c", code, str(path((store, actor), obj))],
                                       env=environment, capture_output=True, text=True, timeout=10))
        raise Executed()
    monkeypatch.setattr(os, "execve", execute)
    args = SimpleNamespace(state=str(store.root.parent), auth_dir=str(store.auth_directory),
                           db=str(store.session_database), launch=receipt["launch_id"], tab=receipt["tabs"][0])
    with pytest.raises(Executed):
        launch.execute(args)
    assert completed[0].returncode == 0, completed[0].stderr
    assert (project / "proof").read_text() == "host user"
    assert path((store, actor), obj).read_bytes() == b"original immutable bytes"
    with pytest.raises(StagingError, match="consumed"):
        launch.execute(args)
