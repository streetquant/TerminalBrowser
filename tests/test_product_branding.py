"""Keep visible branding consistent without renaming compatibility identities."""

import ast
from pathlib import Path
import re

import pytest

from termdeck.desktop_integration import APP_ID, build_autostart_entry, build_desktop_entry


ROOT = Path(__file__).resolve().parents[1]
RETIRED_BRANDING = re.compile(r"\b(?:TermDeck|T1000|T-1000)\b")
VISIBLE_PYTHON = (
    "gtk_app.py", "cli.py", "quickstart.py", "command_palette.py",
    "control_plan.py", "diagnostics.py", "session_search.py", "shortcuts.py",
)


@pytest.mark.parametrize("filename", VISIBLE_PYTHON)
def test_visible_python_strings_use_t1000(filename: str) -> None:
    tree = ast.parse((ROOT / "termdeck" / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        # D-Bus identity is deliberately stable across this branding-only change.
        value = node.value.replace("dev.t1000.T1000", "")
        assert not RETIRED_BRANDING.search(value), (filename, node.lineno, value)


def test_desktop_names_change_without_changing_application_identity() -> None:
    assert APP_ID == "io.github.t1000.T1000"
    for descriptor in (build_desktop_entry("t1000 gui"), build_autostart_entry()):
        assert "Name=t1000\n" in descriptor
        assert "Name=T1000\n" not in descriptor


def test_browser_branding_preserves_existing_authentication_identifiers() -> None:
    assets = ROOT / "web/crates/termdeck-web-server/assets"
    script = (assets / "app.js").read_text(encoding="utf-8")
    assert '"X-TermDeck-CSRF": csrf' in script
    assert "window.TermDeckTerminal" in script
    assert 'document.title = "t1000"' in script
    assert 'link("t1000",' in script
    assert not RETIRED_BRANDING.search(script.replace("X-TermDeck-CSRF", ""))


def test_server_pages_have_no_retired_visible_branding() -> None:
    server = (ROOT / "web/crates/termdeck-web-server/src/lib.rs").read_text(encoding="utf-8")
    assert "Choose your t1000 password" in server
    assert "Sign in to t1000" in server
    assert "<title>t1000</title>" in server
    assert "Recovery pairing · t1000" in server
    production_source = server.split("#[cfg(test)]", 1)[0]
    assert not re.search(r"\bTermDeck\b", production_source)
    assert 'HeaderValue::from_static("TermDeckSession")' in server
