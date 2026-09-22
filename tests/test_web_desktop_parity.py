"""Keep shared desktop/web visual identity tied to the desktop source."""

from pathlib import Path
import json
import re

import pytest

from termdeck.ui_theme import CHROME_TOKENS, TERMINAL_PALETTE_PACKS


@pytest.mark.parametrize(
    ("web_token", "desktop_token"),
    [
        ("bg", "bg"), ("raised", "bg_raised"), ("sunken", "bg_sunken"),
        ("fg", "fg"), ("muted", "fg_muted"), ("border", "border"),
        ("accent", "accent"), ("danger", "danger"),
        ("warning", "warning"), ("success", "success"),
    ],
)
def test_web_workspace_uses_desktop_dark_chrome(web_token, desktop_token):
    css = (Path(__file__).resolve().parents[1] / "web/crates/termdeck-web-server/assets/app.css").read_text()
    block = re.search(r"body\.workspace-page\s*\{([^}]+)\}", css).group(1)
    expected = getattr(CHROME_TOKENS["dark"], desktop_token)
    assert f"--td-{web_token}: {expected};" in block


def test_all_web_terminal_palettes_match_desktop_exactly():
    palettes = json.loads((Path(__file__).resolve().parents[1] / "web/frontend/src/terminal-palettes.json").read_text())
    assert palettes == json.loads(json.dumps(dict(TERMINAL_PALETTE_PACKS)))
