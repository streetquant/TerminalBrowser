"""The managed CLI exposes only the fixed, authorized native launch bridge."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from termdeck import agent_launch, cli
from termdeck.client_files import StagingError
from termdeck.storage_fabric import StorageFabricError


REQUIRED = ("state", "auth-dir", "db", "launch", "tab")


@pytest.fixture
def launch_argv(tmp_path, monkeypatch):
    monkeypatch.delenv("T1000_MANAGED_ROOT", raising=False)
    monkeypatch.setattr(cli, "_adopt_process_name", lambda: None)
    values = {
        "state": str(tmp_path / "private state"),
        "auth-dir": str(tmp_path / "private auth"),
        "db": str(tmp_path / "sessions.sqlite3"),
        "launch": "launch-0123456789abcdef",
        "tab": "tab-0123456789abcdef",
    }
    return ["agent-launch", *(part for name in REQUIRED for part in ("--" + name, values[name]))]


def test_parser_registers_exact_agent_launch_handler(launch_argv):
    args = cli.build_parser().parse_args(launch_argv)

    assert args.command == "agent-launch"
    assert args.func is agent_launch.command
    assert args._uses_session_db is True
    assert args.resume_permit is None
    assert {name.replace("-", "_"): launch_argv[launch_argv.index("--" + name) + 1]
            for name in REQUIRED} == {
        name.replace("-", "_"): getattr(args, name.replace("-", "_"))
        for name in REQUIRED
    }


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_argument_refuses_before_dispatch(launch_argv, monkeypatch, capsys, missing):
    execute = Mock()
    monkeypatch.setattr(agent_launch, "execute", execute)
    index = launch_argv.index("--" + missing)
    del launch_argv[index:index + 2]

    with pytest.raises(SystemExit) as stopped:
        cli.main(launch_argv)

    assert stopped.value.code == 2
    assert "--" + missing in capsys.readouterr().err
    execute.assert_not_called()


@pytest.mark.parametrize("option", (*REQUIRED, "resume-permit"))
def test_argument_without_value_refuses_before_dispatch(launch_argv, monkeypatch, capsys, option):
    execute = Mock()
    monkeypatch.setattr(agent_launch, "execute", execute)

    with pytest.raises(SystemExit) as stopped:
        cli.main([*launch_argv, "--" + option])

    assert stopped.value.code == 2
    assert "expected one argument" in capsys.readouterr().err
    execute.assert_not_called()


@pytest.mark.parametrize("extra", [
    ["--command", "printf unexpected"],
    ["--request", '{"operation":"exec"}'],
    ["--", "/bin/sh"],
    ["unexpected-positional-command"],
])
def test_unknown_or_command_arguments_refuse_before_dispatch(launch_argv, monkeypatch, capsys, extra):
    execute = Mock()
    monkeypatch.setattr(agent_launch, "execute", execute)

    with pytest.raises(SystemExit) as stopped:
        cli.main([*launch_argv, *extra])

    assert stopped.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    execute.assert_not_called()


@pytest.mark.parametrize("permit", [None, "resume-0123456789abcdef"])
def test_main_dispatches_to_authorized_launch_with_literal_values(launch_argv, monkeypatch, permit):
    execute = Mock()
    monkeypatch.setattr(agent_launch, "execute", execute)
    # Parsing forwards one value; only the launch helper validates identities.
    launch_argv[-1] = "literal tab; $(not-a-command)"
    argv = launch_argv if permit is None else [*launch_argv, "--resume-permit", permit]

    assert cli.main(argv) == 0

    execute.assert_called_once()
    args, = execute.call_args.args
    assert args.tab == "literal tab; $(not-a-command)"
    assert args.resume_permit == permit
    assert args.db == launch_argv[launch_argv.index("--db") + 1]
    assert args.state == launch_argv[launch_argv.index("--state") + 1]
    assert args.auth_dir == launch_argv[launch_argv.index("--auth-dir") + 1]
    assert args.launch == launch_argv[launch_argv.index("--launch") + 1]


@pytest.mark.parametrize("error", [
    StagingError("Launch authorization refused", "forbidden"),
    OSError("private filesystem detail"),
    ValueError("private malformed value"),
])
def test_launch_refusal_is_controlled_without_gui_fallback(launch_argv, monkeypatch, capsys, error):
    execute = Mock(side_effect=error)
    gui = Mock()
    monkeypatch.setattr(agent_launch, "execute", execute)
    monkeypatch.setattr(cli, "cmd_gui", gui)

    assert cli.main(launch_argv) == 78

    execute.assert_called_once()
    gui.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "TermDeck native task could not start" in output.err
    assert "Traceback" not in output.err
    if isinstance(error, StagingError):
        assert "Launch authorization refused" in output.err
    else:
        assert str(error) not in output.err


def test_managed_database_gate_precedes_launch_dispatch(launch_argv, tmp_path, monkeypatch, capsys):
    execute = Mock()
    gate = Mock(side_effect=StorageFabricError("disposable storage admission refusal"))
    monkeypatch.setattr(agent_launch, "execute", execute)
    monkeypatch.setattr(cli, "require_hot_storage", gate)
    monkeypatch.setenv("T1000_MANAGED_ROOT", str(tmp_path / "managed"))

    assert cli.main(launch_argv) == 78

    gate.assert_called_once()
    assert gate.call_args.kwargs["db_path"] == tmp_path / "sessions.sqlite3"
    execute.assert_not_called()
    assert "managed CLI refused unsafe session state" in capsys.readouterr().err
