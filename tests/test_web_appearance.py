import json
from pathlib import Path

import pytest

from termdeck.settings import AppSettings
from termdeck.web_appearance import exchange


def save_request(path, **overrides):
    data = exchange({"mode": "read"}, path)
    return {"mode": "save", "confirmation": "save-desktop-defaults", "revision": data["revision"], "values": {**data["values"], **overrides}}


def test_appearance_round_trip_preserves_unrelated_settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"keyboard": {"profile": "browser"}, "unrelated": {"keep": 1}}))
    request = save_request(path, chrome="noir", palette="nord", cursorColor="#abcdef", cursorStyle="bar", scrollback=30000, tabsPane=True, reduceMotion=True)
    result = exchange(request, path)
    assert result["ok"]
    saved = AppSettings.load(path)
    assert saved.chrome_theme == "noir"
    assert saved.terminal_cursor_shape == "ibeam"
    assert saved.terminal_scrollback_lines == 30000
    assert saved.tabs_pane_visible and saved.reduce_motion
    assert json.loads(path.read_text())["unrelated"] == {"keep": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".appearance-*"))


def test_stale_revision_does_not_overwrite_desktop_changes(tmp_path):
    path = tmp_path / "config.json"
    request = save_request(path)
    path.write_text('{"unrelated": "new"}')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        exchange(request, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("chrome", "invalid"), ("fontSize", True), ("scrollback", 999999), ("uiScale", float("nan")), ("cursorColor", "red;"), ("fontFamily", "bad\nfont"), ("tabsPane", "yes")])
def test_invalid_preferences_never_write(tmp_path, field, value):
    path = tmp_path / "config.json"
    request = save_request(path, **{field: value})
    with pytest.raises(ValueError):
        exchange(request, path)
    assert not path.exists()


def test_symlink_and_missing_confirmation_rejected(tmp_path):
    path = tmp_path / "config.json"
    request = save_request(path)
    request.pop("confirmation")
    with pytest.raises(ValueError, match="Confirm"):
        exchange(request, path)
    target = tmp_path / "target.json"; target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(OSError):
        exchange({"mode": "read"}, path)
    assert target.read_text() == "{}"
