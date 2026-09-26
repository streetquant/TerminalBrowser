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
    # Keep sibling/rename scenarios inside the one automatically cleaned root.
    root = ordinary_workspace_root / "workspace-tree"
    root.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    workspace = root / "ordinary project with spaces"
    workspace.mkdir(mode=0o770)
    return state, workspace


def sandbox(directory, code, *args):
    argv = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-all", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            *directory.mount_arguments(), "--chdir", str(directory.path),
            "--", sys.executable, "-c", code, *map(str, args)]
    return argv


def test_directory_selection_needs_no_saved_policy_and_preserves_spaces(directories):
    state, workspace = directories
    result = workspaces.select(str(workspace), state_root=state)
    assert result["path"] == str(workspace)
    assert result["source"] == "server-directory"
    assert not result["pin"]["read_only"]
    assert result["pin"]["chain"][-1] == [workspace.stat().st_dev, workspace.stat().st_ino]
    with workspaces.open_workspace(result, state_root=state) as directory:
        directory.check()


def test_initial_symlink_resolves_to_canonical_directory(directories):
    state, workspace = directories
    alias = workspace.with_name("shortcut")
    alias.symlink_to(workspace, target_is_directory=True)
    result = workspaces.select(str(alias), state_root=state)
    assert result["path"] == str(workspace)


def test_readonly_directory_is_usable_and_cannot_gain_write_authority(directories):
    state, workspace = directories
    workspace.chmod(0o500)
    try:
        result = workspaces.select(str(workspace), state_root=state)
        assert result["pin"]["read_only"]
        workspace.chmod(0o700)
        with workspaces.open_workspace(result, state_root=state) as directory:
            argv = sandbox(directory, "import pathlib; pathlib.Path('proof').write_text('unexpected')")
            completed = subprocess.run(argv, pass_fds=(directory.fd,), capture_output=True, text=True, timeout=10)
        assert completed.returncode != 0
        assert "Read-only file system" in completed.stderr
        assert not (workspace / "proof").exists()
    finally:
        workspace.chmod(0o700)


@pytest.mark.parametrize("selected", ["relative", "../escape", "/tmp/../etc", "", "/", "/proc", "/dev", "/sys", "/run"])
def test_invalid_and_control_paths_rejected(directories, selected):
    state, _ = directories
    with pytest.raises(StagingError):
        workspaces.select(selected, state_root=state)


@pytest.mark.parametrize("kind", ["missing", "file", "fifo", "socket", "unreadable"])
def test_missing_inaccessible_and_special_directory_objects_rejected(directories, kind):
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
    elif kind == "unreadable":
        selected.mkdir(mode=0o000)
    try:
        with pytest.raises(StagingError):
            workspaces.select(str(selected), state_root=state)
    finally:
        if control:
            control.close()
        if kind == "unreadable":
            selected.chmod(0o700)


@pytest.mark.parametrize("relative", [".termdeck", ".termdeck/tls", ".beads", "terminal-auth", ".ssh"])
def test_reserved_directory_cannot_be_selected(directories, relative):
    state, workspace = directories
    protected = workspace / relative
    protected.mkdir(parents=True)
    with pytest.raises(StagingError, match="Protected storage"):
        workspaces.select(str(protected), state_root=state)


def test_external_auth_and_catalogue_cannot_be_selected(directories):
    state, workspace = directories
    catalogue = workspace / "external account catalogue"
    catalogue.mkdir()
    child = catalogue / "accounts"
    child.mkdir()
    for selected in (state, catalogue, child):
        with pytest.raises(StagingError, match="Protected storage"):
            workspaces.select(str(selected), state_root=state, protected=(catalogue,))


@pytest.mark.parametrize("external", [True, False])
def test_home_with_private_catalogue_alias_hides_canonical_target(directories, external):
    state, workspace = directories
    catalogue = workspace.parent / "outside catalogue" if external else workspace / "native catalogue"
    catalogue.mkdir()
    (catalogue / "auth.json").write_text("private credentials")
    (workspace / ".codex").symlink_to(catalogue, target_is_directory=True)
    (workspace / "ordinary-alias").symlink_to(catalogue, target_is_directory=True)
    result = workspaces.select(str(workspace), state_root=state)
    code = """import pathlib,sys
root=pathlib.Path.cwd()
assert not (root/'.codex/auth.json').exists()
assert not (root/'ordinary-alias/auth.json').exists()
assert not (pathlib.Path(sys.argv[1])/'auth.json').exists()
(root/'project.txt').write_text('works')
"""
    with workspaces.open_workspace(result, state_root=state) as directory:
        completed = subprocess.run(sandbox(directory, code, catalogue), pass_fds=(directory.fd,),
                                   capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert (workspace / "project.txt").read_text() == "works"
    assert (catalogue / "auth.json").read_text() == "private credentials"


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
    with pytest.raises(StagingError):
        workspaces.open_workspace(result, state_root=state).__enter__()


def test_open_descriptor_never_mounts_replacement_after_final_path_check(directories):
    state, workspace = directories
    (workspace / "identity").write_text("approved original")
    result = workspaces.select(str(workspace), state_root=state)
    with workspaces.open_workspace(result, state_root=state) as directory:
        argv = sandbox(directory, "import pathlib; assert pathlib.Path('identity').read_text() == 'approved original'")
        moved = workspace.with_name("original")
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "identity").write_text("unauthorized replacement")
        completed = subprocess.run(argv, pass_fds=(directory.fd,), capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_broad_parent_masks_nested_state_catalogue_ledger_private_homes_and_special_files(directories):
    state, workspace = directories
    root = workspace.parent
    catalogue = root / "external catalogue"
    catalogue.mkdir()
    hidden = [state, catalogue, workspace / ".termdeck", workspace / ".beads",
              workspace / "nested/terminal-auth", workspace / "nested/project/.beads"]
    for path in hidden:
        path.mkdir(parents=True, exist_ok=True)
        (path / "sentinel").write_text("private")
    outside = root.with_name(root.name + "-outside")
    outside.mkdir()
    (outside / "sentinel").write_text("outside")
    (workspace / "outside-alias").symlink_to(outside, target_is_directory=True)
    fifo = workspace / "host-control"
    os.mkfifo(fifo)
    result = workspaces.select(str(root), state_root=state, protected=(catalogue,))
    code = """import pathlib,stat,sys
root=pathlib.Path(sys.argv[1])
for value in sys.argv[2:]:
    assert not (pathlib.Path(value)/'sentinel').exists(), value
control=root/'ordinary project with spaces/host-control'
assert not stat.S_ISFIFO(control.stat().st_mode)
assert not (root/'ordinary project with spaces/outside-alias/sentinel').exists()
(root/'ordinary project with spaces/proof').write_text('authorized')
"""
    with workspaces.open_workspace(result, state_root=state, protected=(catalogue,)) as directory:
        completed = subprocess.run(sandbox(directory, code, root, *hidden), pass_fds=(directory.fd,),
                                   capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert (workspace / "proof").read_text() == "authorized"
    assert all((path / "sentinel").read_text() == "private" for path in hidden)


def test_inventory_limit_fails_clearly(directories, monkeypatch):
    state, workspace = directories
    for index in range(3):
        (workspace / str(index)).mkdir()
    monkeypatch.setattr(workspaces, "_MAX_ENTRIES", 2)
    with pytest.raises(StagingError, match="too large"):
        workspaces.select(str(workspace), state_root=state)


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


def test_broad_authenticated_launch_keeps_exact_attachment_and_masks_surrounding_state(ordinary_workspace_root, monkeypatch):
    (ordinary_workspace_root / "state").mkdir(mode=0o700)
    store, actor = make_stage(ordinary_workspace_root / "state", ordinary_workspace_root / "auth")
    project = ordinary_workspace_root / "project with spaces"
    project.mkdir()
    (project / ".beads").mkdir()
    (project / ".beads/private").write_text("private")
    obj = upload((store, actor), b"original immutable bytes", display_path="/client/metadata-only")
    request = creation(store, actor, ordinary_workspace_root, attachments=[obj["reference"]])
    receipt = store.handle(actor, request)
    launch.start(store, actor, receipt, backend=Backend())
    upload((store, actor), b"newer", previous=obj["reference"])
    code = """import pathlib,sys
attachment=pathlib.Path(sys.argv[1]); root=pathlib.Path.cwd()
assert attachment.read_bytes()==b'original immutable bytes'
assert not (root/'auth/termdeck-web-auth-v1.sqlite3').exists()
assert not (root/'state/client-files/metadata.sqlite3').exists()
assert not (root/'project with spaces/.beads/private').exists()
try: attachment.write_text('changed')
except OSError: pass
else: raise AssertionError('immutable attachment became writable')
(root/'project with spaces/proof').write_text('confined')
"""
    completed = []
    class Executed(Exception):
        pass
    def execute(_executable, argv, _environment):
        assert "--bind-fd" in argv
        split = argv.index("--")
        descriptors = tuple(int(argv[i + 1]) for i, value in enumerate(argv) if value in {"--bind-fd", "--ro-bind-fd"})
        command = [*argv[:split + 1], sys.executable, "-c", code, str(path((store, actor), obj))]
        completed.append(subprocess.run(command, pass_fds=descriptors, capture_output=True, text=True, timeout=10))
        raise Executed()
    monkeypatch.setattr(os, "execve", execute)
    args = SimpleNamespace(state=str(store.root.parent), auth_dir=str(store.auth_directory),
                           db=str(store.session_database), launch=receipt["launch_id"], tab=receipt["tabs"][0])
    with pytest.raises(Executed):
        launch.execute(args)
    assert completed[0].returncode == 0, completed[0].stderr
    assert (project / "proof").read_text() == "confined"
    assert path((store, actor), obj).read_bytes() == b"original immutable bytes"
    with pytest.raises(StagingError, match="consumed"):
        launch.execute(args)
