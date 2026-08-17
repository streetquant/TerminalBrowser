from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck import cli
from termdeck.release_manager import ReleaseError


def capture(handler, args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = handler(args)
    return code, json.loads(output.getvalue())


def test_release_parser_wires_all_read_and_mutation_subcommands() -> None:
    parser = cli.build_parser()
    cases = {
        ("release", "info"): cli.cmd_release_info,
        ("release", "build"): cli.cmd_release_build,
        ("release", "verify"): cli.cmd_release_verify,
        ("release", "install"): cli.cmd_release_install,
        ("release", "rollback"): cli.cmd_release_rollback,
        ("release", "snapshot"): cli.cmd_release_snapshot,
    }
    argv = {
        ("release", "info"): ["release", "info"],
        ("release", "build"): ["release", "build", "--output", "/tmp/out"],
        ("release", "verify"): ["release", "verify", "a.whl", "a.json"],
        ("release", "install"): ["release", "install", "a.whl", "a.json"],
        ("release", "rollback"): ["release", "rollback", "--legacy"],
        ("release", "snapshot"): ["release", "snapshot"],
    }
    for key, expected in cases.items():
        assert parser.parse_args(argv[key]).func is expected


def test_release_info_combines_runtime_and_managed_integrity() -> None:
    args = argparse.Namespace(root=Path("/managed"), bin_dir=Path("/bin"), verify=True)
    with mock.patch(
        "termdeck.cli.release_status",
        return_value={"ok": True, "posture": "managed-release", "release_id": "a" * 64},
    ) as runtime, mock.patch(
        "termdeck.cli.managed_release_status",
        return_value={"ok": True, "verified": True},
    ) as managed:
        code, payload = capture(cli.cmd_release_info, args)
    assert code == 0
    assert payload["ok"] is True
    assert payload["runtime"]["release_id"] == "a" * 64
    runtime.assert_called_once_with(verify_artifact=True, managed_root=Path("/managed"))
    managed.assert_called_once_with(root=Path("/managed"), bin_dir=Path("/bin"), verify=True)


def test_release_handlers_forward_exact_user_arguments() -> None:
    with mock.patch("termdeck.cli.build_release", return_value={"ok": True, "action": "release-build"}) as build:
        code, payload = capture(
            cli.cmd_release_build,
            argparse.Namespace(
                source=Path("source"),
                output=Path("output"),
                allow_dirty=True,
                overwrite=True,
                python=Path("/usr/bin/python"),
            ),
        )
    assert code == 0 and payload["ok"] is True
    build.assert_called_once_with(
        Path("source"),
        Path("output"),
        allow_dirty=True,
        python=Path("/usr/bin/python"),
        overwrite=True,
    )

    verified = SimpleNamespace(as_dict=lambda: {"release_id": "b" * 64})
    with mock.patch("termdeck.cli.verify_wheel", return_value=verified) as verify:
        code, payload = capture(
            cli.cmd_release_verify,
            argparse.Namespace(wheel=Path("x.whl"), manifest=Path("x.json")),
        )
    assert code == 0 and payload["action"] == "release-verify"
    verify.assert_called_once_with(Path("x.whl"), manifest_path=Path("x.json"))

    with mock.patch("termdeck.cli.install_release", return_value={"ok": True}) as install:
        code, _payload = capture(
            cli.cmd_release_install,
            argparse.Namespace(
                wheel=Path("x.whl"),
                manifest=Path("x.json"),
                root=Path("root"),
                bin_dir=Path("bin"),
                python=None,
                lock_timeout=2.5,
            ),
        )
    assert code == 0
    install.assert_called_once_with(
        Path("x.whl"),
        Path("x.json"),
        root=Path("root"),
        bin_dir=Path("bin"),
        python=None,
        lock_timeout=2.5,
    )

    with mock.patch("termdeck.cli.rollback_release", return_value={"ok": True}) as rollback:
        code, _payload = capture(
            cli.cmd_release_rollback,
            argparse.Namespace(
                root=Path("root"),
                bin_dir=Path("bin"),
                release_id="c" * 64,
                legacy=False,
                lock_timeout=3.0,
            ),
        )
    assert code == 0
    rollback.assert_called_once_with(
        root=Path("root"),
        bin_dir=Path("bin"),
        release_id="c" * 64,
        to_legacy=False,
        lock_timeout=3.0,
    )

    with mock.patch(
        "termdeck.cli.semantic_runtime_snapshot",
        return_value={"schema": "t1000.semantic-runtime-snapshot.v1", "sha256": "d" * 64},
    ) as snapshot:
        code, payload = capture(
            cli.cmd_release_snapshot,
            argparse.Namespace(db=Path("sessions.sqlite3")),
        )
    assert code == 0 and payload["action"] == "release-snapshot"
    snapshot.assert_called_once_with(db_path=Path("sessions.sqlite3"))


def test_release_handler_errors_are_machine_readable_and_nonzero() -> None:
    with mock.patch("termdeck.cli.install_release", side_effect=ReleaseError("bad manifest")):
        code, payload = capture(
            cli.cmd_release_install,
            argparse.Namespace(
                wheel=Path("x.whl"),
                manifest=Path("x.json"),
                root=None,
                bin_dir=None,
                python=None,
                lock_timeout=15.0,
            ),
        )
    assert code == 2
    assert payload == {
        "ok": False,
        "action": "release-install",
        "error": "bad manifest",
    }


def test_doctor_exposes_redaction_safe_release_posture() -> None:
    fake_theme = SimpleNamespace(source_path=None, profile_name="default")
    with mock.patch("termdeck.cli.shutil.which", return_value="/usr/bin/tool"), mock.patch(
        "termdeck.cli._has_gi_namespace", return_value=True
    ), mock.patch(
        "termdeck.cli.TerminatorConfig.from_default_path",
        return_value=SimpleNamespace(to_theme=lambda: fake_theme),
    ), mock.patch(
        "termdeck.cli.release_status",
        return_value={"ok": True, "posture": "development", "release_id": None},
    ):
        code, payload = capture(cli.cmd_doctor, argparse.Namespace())
    assert code == 0
    assert payload["release"]["posture"] == "development"
