from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck.settings import AppSettings
from termdeck.diagnostics import build_diagnostic_payload


def test_diagnostics_include_redaction_safe_release_identity(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite3"
    config = tmp_path / "config.json"
    settings = AppSettings(path=config)
    backend = SimpleNamespace(available=False)
    theme = SimpleNamespace(
        source_path=None,
        profile_name="default",
        font="Monospace 12",
        scrollback_lines=1000,
        window_width=1200,
        window_height=800,
    )
    release = {
        "schema": "t1000.release-status.v1",
        "ok": True,
        "posture": "managed-release",
        "release_id": "a" * 64,
        "build_info": {
            "build_id": "b" * 64,
            "source_commit": "c" * 40,
        },
        "errors": [],
    }

    with mock.patch("termdeck.diagnostics.TmuxBackend", return_value=backend), mock.patch(
        "termdeck.diagnostics.ResourceSampler", return_value=SimpleNamespace()
    ), mock.patch(
        "termdeck.diagnostics.AppSettings.load", return_value=settings
    ), mock.patch(
        "termdeck.diagnostics.load_budgets", return_value=[]
    ), mock.patch(
        "termdeck.diagnostics.TerminatorConfig.from_default_path",
        return_value=SimpleNamespace(to_theme=lambda: theme),
    ), mock.patch(
        "termdeck.diagnostics.release_status", return_value=release
    ) as release_probe:
        payload = build_diagnostic_payload(db_path=db, config_path=config)

    assert payload["release"] == release
    assert payload["privacy"]["captures_included"] is False
    assert payload["privacy"]["environment_included"] is False
    release_probe.assert_called_once_with(verify_artifact=False)
