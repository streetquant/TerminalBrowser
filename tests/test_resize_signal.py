from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping
from unittest import mock

from termdeck import resize_signal
from termdeck.resize_signal import ProcessIdentity, propagate_terminal_resize


def _write_process(
    root: Path,
    pid: int,
    *,
    ppid: int,
    pgrp: int,
    session: int,
    start_ticks: int,
    comm: str,
    children: tuple[int, ...] = (),
    environment: Mapping[str, str] | None = None,
) -> None:
    directory = root / str(pid)
    task = directory / "task" / str(pid)
    task.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(ppid), str(pgrp), str(session), *("0" for _ in range(15)), str(start_ticks)]
    assert len(fields) == 20
    (directory / "stat").write_text(
        f"{pid} ({comm}) " + " ".join(fields),
        encoding="latin-1",
    )
    # Linux procfs exposes this file without a trailing newline. Preserve that
    # contract so the regression test catches EOF-sensitive readers.
    (task / "children").write_text(" ".join(str(child) for child in children), encoding="ascii")
    entries = [
        f"{key}={value}".encode("utf-8")
        for key, value in (environment or {}).items()
    ]
    (directory / "environ").write_bytes(b"\0".join(entries))


def _private_environment(
    scope: str = "auth-0123456789abcdef0123456789abcdef",
) -> dict[str, str]:
    home = f"/home/max/.local/share/termdeck/terminal-auth/{scope}"
    return {
        "CODEX_HOME": home,
        "TERMDECK_AUTH_SCOPE_ID": scope,
        "CODEX_TERMDECK_GATE_ROOT": f"{home}/.termdeck-codex-lifetime",
    }


def _expected_private_tuple(
    scope: str = "auth-0123456789abcdef0123456789abcdef",
):
    environment = _private_environment(scope)
    return resize_signal.private_launch_tuple_for_scope(
        environment["CODEX_HOME"],
        environment["TERMDECK_AUTH_SCOPE_ID"],
    )


def _tree(root: Path, *, ambiguous: bool = False) -> None:
    inner_children = (102, 105) if ambiguous else (102,)
    _write_process(root, 100, ppid=1, pgrp=100, session=100, start_ticks=1000, comm="bash", children=(101,))
    _write_process(root, 101, ppid=100, pgrp=100, session=100, start_ticks=1001, comm="bwrap", children=inner_children)
    _write_process(root, 102, ppid=101, pgrp=102, session=102, start_ticks=1002, comm="bwrap", children=(103,))
    _write_process(
        root,
        103,
        ppid=102,
        pgrp=102,
        session=102,
        start_ticks=1003,
        comm="codex",
        children=(104,),
        environment=_private_environment(),
    )
    _write_process(root, 104, ppid=103, pgrp=104, session=102, start_ticks=1004, comm="python", children=())
    if ambiguous:
        _write_process(
            root,
            105,
            ppid=101,
            pgrp=105,
            session=105,
            start_ticks=1005,
            comm="bwrap",
            children=(106,),
        )
        _write_process(
            root,
            106,
            ppid=105,
            pgrp=105,
            session=105,
            start_ticks=1006,
            comm="codex",
            children=(),
            environment=_private_environment(),
        )


def test_exact_contained_session_group_is_signalled_once(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    calls: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )

    assert result.signalled is True
    assert result.target_pid == 102
    assert result.target_pgrp == 102
    assert calls == [(102, signal.SIGWINCH)]
    assert result.scanned_processes >= 3


def test_no_candidate_and_wrong_owner_are_safe_noops(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_process(proc, 100, ppid=1, pgrp=100, session=100, start_ticks=1, comm="bash", children=(101,))
    _write_process(proc, 101, ppid=100, pgrp=100, session=100, start_ticks=2, comm="python", children=())

    missing = propagate_terminal_resize(100, proc_root=proc, expected_uid=os.geteuid(), killpg=lambda *_: None)
    wrong_owner = propagate_terminal_resize(100, proc_root=proc, expected_uid=os.geteuid() + 1, killpg=lambda *_: None)

    assert missing.signalled is False
    assert missing.reason == "no exact private TermDeck containment chain"
    assert wrong_owner.signalled is False
    assert wrong_owner.reason == "pane root has unexpected ownership"


def test_unrelated_bwrap_session_without_codex_witness_is_not_signalled(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_process(
        proc,
        100,
        ppid=1,
        pgrp=100,
        session=100,
        start_ticks=1,
        comm="bash",
        children=(101,),
    )
    _write_process(
        proc,
        101,
        ppid=100,
        pgrp=101,
        session=101,
        start_ticks=2,
        comm="bwrap",
        children=(102,),
    )
    _write_process(
        proc,
        102,
        ppid=101,
        pgrp=101,
        session=101,
        start_ticks=3,
        comm="python",
        children=(),
    )
    calls: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )

    assert result.signalled is False
    assert result.reason == "no exact private TermDeck containment chain"
    assert calls == []


def test_ambiguous_nearest_containment_boundaries_fail_closed(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc, ambiguous=True)
    calls: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )

    assert result.signalled is False
    assert result.reason == "ambiguous private TermDeck containment chains"
    assert calls == []


def test_node_bound_and_malformed_stat_fail_closed(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_process(proc, 100, ppid=1, pgrp=100, session=100, start_ticks=1, comm="bash", children=(101,))
    _write_process(proc, 101, ppid=100, pgrp=100, session=100, start_ticks=2, comm="python", children=(102,))
    _write_process(proc, 102, ppid=101, pgrp=100, session=100, start_ticks=3, comm="python", children=())

    bounded = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        maximum_processes=2,
        killpg=lambda *_: None,
    )
    assert bounded.signalled is False
    assert bounded.reason == "process-tree node bound reached"

    (proc / "100" / "stat").write_text("malformed", encoding="ascii")
    malformed = propagate_terminal_resize(100, proc_root=proc, expected_uid=os.geteuid(), killpg=lambda *_: None)
    assert malformed.signalled is False
    assert malformed.reason == "pane root is unavailable"


def test_pid_replacement_between_discovery_and_commit_is_rejected() -> None:
    root = ProcessIdentity(100, 1, 100, 100, 10, os.geteuid(), "bash")
    parent = ProcessIdentity(101, 100, 100, 100, 11, os.geteuid(), "bwrap")
    target = ProcessIdentity(102, 101, 102, 102, 12, os.geteuid(), "bwrap")
    witness = ProcessIdentity(103, 102, 102, 102, 13, os.geteuid(), "codex")
    private_tuple = _expected_private_tuple()
    candidate = resize_signal._Candidate(
        root,
        (parent,),
        parent,
        target,
        witness,
        private_tuple,
        4,
    )
    replaced = ProcessIdentity(102, 101, 102, 102, 99, os.geteuid(), "bwrap")
    current = resize_signal._Candidate(
        root,
        (parent,),
        parent,
        replaced,
        witness,
        private_tuple,
        4,
    )
    calls: list[tuple[int, int]] = []

    with mock.patch.object(
        resize_signal,
        "_discover_contained_group",
        side_effect=[
            (candidate, "exact private TermDeck containment chain found", 4),
            (current, "exact private TermDeck containment chain found", 4),
        ],
    ):
        result = propagate_terminal_resize(
            100,
            proc_root=Path("/synthetic-proc"),
            expected_private_tuple=private_tuple,
            killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
        )

    assert result.signalled is False
    assert result.reason == "contained process graph or private identity changed before signal"
    assert calls == []



def test_pidfd_success_signals_exact_witness_and_closes_descriptor(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    opens: list[tuple[int, int]] = []
    sends: list[tuple[int, int, object | None, int]] = []
    closes: list[int] = []
    groups: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=lambda pid, flags: (opens.append((pid, flags)) or 77),
        pidfd_send_signal=lambda fd, sig, info, flags: sends.append(
            (fd, sig, info, flags)
        ),
        pidfd_has_exited=lambda _fd: False,
        close_fd=lambda fd: closes.append(fd),
    )

    assert result.signalled is True
    assert result.signal_method == "pidfd-witness"
    assert result.target_pid == 102
    assert result.target_pgrp == 102
    assert result.witness_pid == 103
    assert opens == [(103, 0)]
    assert sends == [(77, signal.SIGWINCH, None, 0)]
    assert closes == [77]
    assert groups == []


def test_pidfd_enosys_uses_fully_revalidated_compatibility_path(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    groups: list[tuple[int, int]] = []
    sends: list[tuple[int, int, object | None, int]] = []

    def unsupported(_pid: int, _flags: int) -> int:
        raise OSError(errno.ENOSYS, "pidfd_open unsupported")

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=unsupported,
        pidfd_send_signal=lambda fd, sig, info, flags: sends.append(
            (fd, sig, info, flags)
        ),
    )

    assert result.signalled is True
    assert result.signal_method == "process-group-fallback"
    assert groups == [(102, signal.SIGWINCH)]
    assert sends == []



def test_missing_python_pidfd_capability_uses_compatibility_path(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    groups: list[tuple[int, int]] = []

    with mock.patch.object(resize_signal, "_is_real_procfs", return_value=True), mock.patch.object(
        resize_signal.os,
        "pidfd_open",
        None,
        create=True,
    ), mock.patch.object(
        resize_signal.signal,
        "pidfd_send_signal",
        None,
        create=True,
    ):
        result = propagate_terminal_resize(
            100,
            proc_root=proc,
            expected_uid=os.geteuid(),
            expected_private_tuple=_expected_private_tuple(),
            killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        )

    assert result.signalled is True
    assert result.signal_method == "process-group-fallback"
    assert groups == [(102, signal.SIGWINCH)]


def test_invalid_pidfd_descriptor_is_fail_closed_without_group_fallback(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    groups: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=lambda _pid, _flags: -1,
        pidfd_send_signal=lambda *_args: None,
    )

    assert result.signalled is False
    assert result.reason == "pidfd_open returned an invalid descriptor"
    assert result.signal_method == "none"
    assert groups == []

def test_pidfd_permission_failure_is_fail_closed_without_group_fallback(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    groups: list[tuple[int, int]] = []

    def denied(_pid: int, _flags: int) -> int:
        raise PermissionError(errno.EPERM, "denied")

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=denied,
        pidfd_send_signal=lambda *_args: None,
    )

    assert result.signalled is False
    assert result.reason == "permission denied opening Codex witness pidfd"
    assert result.signal_method == "none"
    assert groups == []


def test_readable_pidfd_is_rejected_and_closed_without_any_signal(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    sends: list[tuple[object, ...]] = []
    groups: list[tuple[int, int]] = []
    closes: list[int] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=lambda _pid, _flags: 88,
        pidfd_send_signal=lambda *args: sends.append(args),
        pidfd_has_exited=lambda _fd: True,
        close_fd=lambda fd: closes.append(fd),
    )

    assert result.signalled is False
    assert result.reason == "Codex witness exited before pidfd signal"
    assert sends == []
    assert groups == []
    assert closes == [88]


def test_pidfd_sender_failure_never_widens_into_group_fallback(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    groups: list[tuple[int, int]] = []
    closes: list[int] = []

    def vanished(_fd: int, _sig: int, _info: object | None, _flags: int) -> None:
        raise ProcessLookupError(errno.ESRCH, "gone")

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=_expected_private_tuple(),
        killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
        pidfd_open=lambda _pid, _flags: 99,
        pidfd_send_signal=vanished,
        pidfd_has_exited=lambda _fd: False,
        close_fd=lambda fd: closes.append(fd),
    )

    assert result.signalled is False
    assert result.reason == "Codex witness disappeared before pidfd signal"
    assert groups == []
    assert closes == [99]


def test_pidfd_is_held_across_identity_revalidation_and_closed() -> None:
    root = ProcessIdentity(100, 1, 100, 100, 10, os.geteuid(), "bash")
    parent = ProcessIdentity(101, 100, 100, 100, 11, os.geteuid(), "bwrap")
    target = ProcessIdentity(102, 101, 102, 102, 12, os.geteuid(), "bwrap")
    witness = ProcessIdentity(103, 102, 102, 102, 13, os.geteuid(), "codex")
    replaced = ProcessIdentity(103, 102, 102, 102, 99, os.geteuid(), "codex")
    private_tuple = _expected_private_tuple()
    candidate = resize_signal._Candidate(
        root,
        (parent,),
        parent,
        target,
        witness,
        private_tuple,
        4,
    )
    changed = resize_signal._Candidate(
        root,
        (parent,),
        parent,
        target,
        replaced,
        private_tuple,
        4,
    )
    groups: list[tuple[int, int]] = []
    sends: list[tuple[object, ...]] = []
    closes: list[int] = []

    with mock.patch.object(
        resize_signal,
        "_discover_contained_group",
        side_effect=[
            (candidate, "exact private TermDeck containment chain found", 4),
            (changed, "exact private TermDeck containment chain found", 4),
        ],
    ):
        result = propagate_terminal_resize(
            100,
            proc_root=Path("/synthetic-proc"),
            expected_private_tuple=private_tuple,
            killpg=lambda pgrp, sig: groups.append((pgrp, sig)),
            pidfd_open=lambda _pid, _flags: 123,
            pidfd_send_signal=lambda *args: sends.append(args),
            pidfd_has_exited=lambda _fd: False,
            close_fd=lambda fd: closes.append(fd),
        )

    assert result.signalled is False
    assert result.reason == "contained process graph or private identity changed before signal"
    assert sends == []
    assert groups == []
    assert closes == [123]

def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def test_real_proc_tree_signals_payload_group_but_not_mcp_group(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    inner = tmp_path / "inner.py"
    outer = tmp_path / "outer.py"
    payload_receipt = tmp_path / "payload.received"
    mcp_receipt = tmp_path / "mcp.received"
    payload_ready = tmp_path / "payload.ready"
    mcp_ready = tmp_path / "mcp.ready"
    ready = tmp_path / "ready.json"

    target.write_text(
        """from pathlib import Path
import ctypes, os, signal, sys
receipt = Path(sys.argv[1])
ready = Path(sys.argv[2])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'codex', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
def handler(*_args):
    receipt.write_text(str(os.getpid()), encoding='ascii')
signal.signal(signal.SIGWINCH, handler)
ready.write_text(str(os.getpid()), encoding='ascii')
while True:
    signal.pause()
""",
        encoding="utf-8",
    )
    inner.write_text(
        """import ctypes, json, os, subprocess, sys
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'bwrap', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
scope = 'auth-0123456789abcdef0123456789abcdef'
home = str(Path(sys.argv[6]).parent / scope)
payload_env = dict(os.environ)
payload_env.update({
    'CODEX_HOME': home,
    'TERMDECK_AUTH_SCOPE_ID': scope,
    'CODEX_TERMDECK_GATE_ROOT': home + '/.termdeck-codex-lifetime',
})
payload = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],
    env=payload_env,
)
mcp = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[4], sys.argv[5]], preexec_fn=os.setpgrp)
for _attempt in range(500):
    if Path(sys.argv[3]).exists() and Path(sys.argv[5]).exists():
        break
    __import__('time').sleep(0.01)
else:
    raise SystemExit(97)
Path(sys.argv[6]).write_text(json.dumps({'inner': os.getpid(), 'payload': payload.pid, 'mcp': mcp.pid}), encoding='utf-8')
raise SystemExit(payload.wait())
""",
        encoding="utf-8",
    )
    outer.write_text(
        """import ctypes, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(15, b'bwrap', 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'prctl failed')
inner = subprocess.Popen([sys.executable, sys.argv[1], *sys.argv[2:]], start_new_session=True)
raise SystemExit(inner.wait())
""",
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [
            sys.executable,
            str(outer),
            str(inner),
            str(target),
            str(payload_receipt),
            str(payload_ready),
            str(mcp_receipt),
            str(mcp_ready),
            str(ready),
        ]
    )
    identities: dict[str, int] = {}
    try:
        assert _wait_for(ready)
        identities = {key: int(value) for key, value in json.loads(ready.read_text()).items()}
        expected_private_tuple = resize_signal.private_launch_tuple_for_scope(
            str(tmp_path / "auth-0123456789abcdef0123456789abcdef"),
            "auth-0123456789abcdef0123456789abcdef",
        )
        result = propagate_terminal_resize(
            os.getpid(),
            expected_private_tuple=expected_private_tuple,
        )
        assert result.signalled is True
        assert result.target_pid == identities["inner"]
        assert result.witness_pid == identities["payload"]
        assert result.signal_method in {"pidfd-witness", "process-group-fallback"}
        assert _wait_for(payload_receipt)
        time.sleep(0.2)
        assert not mcp_receipt.exists()
    finally:
        inner_pid = identities.get("inner")
        mcp_pid = identities.get("mcp")
        if inner_pid:
            try:
                os.killpg(inner_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if mcp_pid:
            try:
                os.killpg(mcp_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_expected_private_tuple_is_required_and_must_match_witness(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    expected = _expected_private_tuple()
    witness_environment = _private_environment()
    (proc / "103" / "environ").write_bytes(
        b"\0".join(
            f"{key}={value}".encode()
            for key, value in witness_environment.items()
        )
    )
    calls: list[tuple[int, int]] = []

    accepted = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert accepted.signalled is True
    assert calls == [(102, signal.SIGWINCH)]

    calls.clear()
    wrong = _expected_private_tuple("auth-fedcba9876543210fedcba9876543210")
    rejected = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=wrong,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert rejected.signalled is False
    assert calls == []


def test_private_launch_tuple_rejects_partial_duplicate_and_inconsistent_environment(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _tree(proc)
    expected = _expected_private_tuple()
    environ = proc / "103" / "environ"
    calls: list[tuple[int, int]] = []

    environ.write_bytes(
        b"CODEX_HOME=/home/max/.local/share/termdeck/terminal-auth/"
        b"auth-0123456789abcdef0123456789abcdef"
    )
    partial = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert partial.signalled is False

    valid = _private_environment()
    environ.write_bytes(
        b"\0".join(
            [f"{key}={value}".encode() for key, value in valid.items()]
            + [f"CODEX_HOME={valid['CODEX_HOME']}".encode()]
        )
    )
    duplicate = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert duplicate.signalled is False

    valid["TERMDECK_AUTH_SCOPE_ID"] = "auth-other"
    environ.write_bytes(
        b"\0".join(f"{key}={value}".encode() for key, value in valid.items())
    )
    inconsistent = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert inconsistent.signalled is False
    assert calls == []


def test_production_launcher_intermediary_is_accepted_but_foreign_domain_is_not(
    tmp_path: Path,
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    expected = _expected_private_tuple()
    _write_process(
        proc,
        100,
        ppid=1,
        pgrp=100,
        session=100,
        start_ticks=1,
        comm="bash",
        children=(101,),
    )
    _write_process(
        proc,
        101,
        ppid=100,
        pgrp=100,
        session=100,
        start_ticks=2,
        comm="bash",
        children=(102,),
    )
    _write_process(
        proc,
        102,
        ppid=101,
        pgrp=100,
        session=100,
        start_ticks=3,
        comm="bwrap",
        children=(103,),
    )
    _write_process(
        proc,
        103,
        ppid=102,
        pgrp=103,
        session=103,
        start_ticks=4,
        comm="bwrap",
        children=(104,),
    )
    _write_process(
        proc,
        104,
        ppid=103,
        pgrp=103,
        session=103,
        start_ticks=5,
        comm="node",
        children=(105,),
    )
    _write_process(
        proc,
        105,
        ppid=104,
        pgrp=103,
        session=103,
        start_ticks=6,
        comm="codex",
        environment=_private_environment(),
    )
    calls: list[tuple[int, int]] = []

    result = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert result.signalled is True
    assert calls == [(103, signal.SIGWINCH)]

    # Moving one launcher intermediary out of the pane's process domain must
    # invalidate the chain instead of discovering its nested sandbox.
    _write_process(
        proc,
        101,
        ppid=100,
        pgrp=999,
        session=999,
        start_ticks=2,
        comm="bash",
        children=(102,),
    )
    calls.clear()
    foreign = propagate_terminal_resize(
        100,
        proc_root=proc,
        expected_uid=os.geteuid(),
        expected_private_tuple=expected,
        killpg=lambda pgrp, sig: calls.append((pgrp, sig)),
    )
    assert foreign.signalled is False
    assert calls == []
