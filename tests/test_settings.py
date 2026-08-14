import json
import tempfile
import unittest
from pathlib import Path

from termdeck.models import SessionRecord
from termdeck.session_store import utc_now
from termdeck.settings import AppSettings, SettingsError, sample_settings_json


def record(tag="ai"):
    now = utc_now()
    return SessionRecord(
        id="td-settings",
        name="agent",
        tag=tag,
        command="codex",
        cwd="/tmp",
        tmux_name="td_agent",
        created_at=now,
        last_opened_at=now,
    )


class SettingsTests(unittest.TestCase):
    def test_missing_settings_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            settings = AppSettings.load(path)
            self.assertEqual(settings.path, path)
            self.assertEqual(settings.policy_for_record(record("none")).critical_cpu_percent, 300.0)

    def test_loads_global_and_per_tag_health_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "health": {"critical_cpu_percent": 111, "critical_memory": "2GiB"},
                "tags": {"ai": {"critical_cpu_percent": 222, "critical_memory": "5GiB"}},
            }), encoding="utf-8")
            settings = AppSettings.load(path)
            self.assertEqual(settings.policy_for_record(record("dev")).critical_cpu_percent, 111.0)
            self.assertEqual(settings.policy_for_record(record("ai")).critical_cpu_percent, 222.0)
            self.assertEqual(settings.policy_for_record(record("ai")).critical_memory_bytes, 5 * 1024 ** 3)
            summary = settings.budget_summary()
            self.assertIn("ai", summary["tag_policies"])

    def test_invalid_settings_raise_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"tags": []}), encoding="utf-8")
            with self.assertRaises(SettingsError):
                AppSettings.load(path)

    def test_reduce_motion_defaults_off_and_loads_both_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertFalse(AppSettings.load(missing).reduce_motion)

            canonical = Path(tmp) / "canonical.json"
            canonical.write_text(
                json.dumps({"appearance": {"reduce_motion": True}}), encoding="utf-8"
            )
            settings = AppSettings.load(canonical)
            self.assertTrue(settings.reduce_motion)
            self.assertTrue(settings.budget_summary()["appearance"]["reduce_motion"])

            alias = Path(tmp) / "alias.json"
            alias.write_text(
                json.dumps({"appearance": {"reduced_motion": True}}), encoding="utf-8"
            )
            self.assertTrue(AppSettings.load(alias).reduce_motion)

    def test_sample_settings_is_loadable_shape(self):
        payload = json.loads(sample_settings_json())
        self.assertIn("health", payload)
        self.assertIn("tags", payload)
        self.assertIn("appearance", payload)
        self.assertIn("terminal", payload["appearance"])

    def test_tabs_pane_visibility_defaults_hidden_and_supports_legacy_focus_mode(self):
        from termdeck.settings import save_terminal_options

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = AppSettings.load(root / "missing.json")
            self.assertFalse(missing.tabs_pane_visible)

            explicit_path = root / "explicit.json"
            explicit_path.write_text(
                json.dumps({"appearance": {"tabs_pane_visible": True}}),
                encoding="utf-8",
            )
            self.assertTrue(AppSettings.load(explicit_path).tabs_pane_visible)

            hidden_legacy_path = root / "hidden-legacy.json"
            hidden_legacy_path.write_text(
                json.dumps({"appearance": {"focus_mode": True}}),
                encoding="utf-8",
            )
            self.assertFalse(AppSettings.load(hidden_legacy_path).tabs_pane_visible)

            visible_legacy_path = root / "visible-legacy.json"
            visible_legacy_path.write_text(
                json.dumps({"appearance": {"focus_mode": False}}),
                encoding="utf-8",
            )
            self.assertTrue(AppSettings.load(visible_legacy_path).tabs_pane_visible)

            save_terminal_options(visible_legacy_path, tabs_pane_visible=False)
            payload = json.loads(visible_legacy_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["appearance"]["tabs_pane_visible"], False)
            self.assertNotIn("focus_mode", payload["appearance"])
            self.assertFalse(AppSettings.load(visible_legacy_path).tabs_pane_visible)

    def test_terminal_options_load_and_apply_to_theme(self):
        from termdeck.models import TerminalTheme
        from termdeck.settings import save_terminal_options
        from termdeck.ui_theme import build_chrome_css

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "appearance": {
                            "ui_font": "Inter 12",
                            "ui_font_scale": 1.15,
                            "refresh_interval_seconds": 9.5,
                            "terminal_palette": "nord",
                            "terminal": {
                                "font": "JetBrains Mono 14",
                                "cursor_shape": "ibeam",
                                "cursor_color": "#88c0d0",
                                "cursor_blink": False,
                                "scrollback_lines": 5000,
                                "use_system_font": False,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = AppSettings.load(path)
            self.assertEqual(settings.terminal_font, "JetBrains Mono 14")
            self.assertEqual(settings.terminal_cursor_shape, "ibeam")
            self.assertEqual(settings.terminal_cursor_color, "#88c0d0")
            self.assertFalse(settings.terminal_cursor_blink)
            self.assertEqual(settings.terminal_scrollback_lines, 5000)
            self.assertEqual(settings.terminal_palette, "nord")
            self.assertEqual(settings.ui_font, "Inter 12")
            self.assertAlmostEqual(settings.ui_font_scale or 0, 1.15, places=2)
            self.assertAlmostEqual(settings.ui_refresh_interval_seconds, 9.5, places=2)
            themed = settings.apply_to_theme(TerminalTheme())
            self.assertEqual(themed.font, "JetBrains Mono 14")
            self.assertEqual(themed.cursor_shape, "ibeam")
            self.assertEqual(themed.cursor_color, "#88c0d0")
            self.assertFalse(themed.cursor_blink)
            self.assertEqual(themed.scrollback_lines, 5000)
            css = build_chrome_css(
                settings.chrome_theme,
                settings.ui_density,
                ui_font=settings.ui_font,
                ui_font_scale=settings.ui_font_scale,
            )
            self.assertIn("Inter", css)

            saved = save_terminal_options(
                path,
                font="Monospace 16",
                cursor_shape="underline",
                cursor_blink=False,
                scrollback_lines=8000,
                side_pane_position="left",
                terminal_palette="one-dark",
                ui_font="Cantarell 11",
                ui_font_scale=1.1,
            )
            reloaded = AppSettings.load(saved)
            self.assertEqual(reloaded.terminal_font, "Monospace 16")
            self.assertEqual(reloaded.terminal_cursor_shape, "underline")
            self.assertFalse(reloaded.terminal_cursor_blink)
            self.assertEqual(reloaded.terminal_scrollback_lines, 8000)
            self.assertEqual(reloaded.side_pane_position, "left")
            self.assertEqual(reloaded.terminal_palette, "one-dark")
            self.assertEqual(reloaded.ui_font, "Cantarell 11")
            self.assertAlmostEqual(reloaded.ui_font_scale or 0, 1.1, places=2)

    def test_cursor_blinking_alias_loads_and_saves_canonical_cursor_blink(self):
        from termdeck.models import TerminalTheme
        from termdeck.settings import save_terminal_options

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"appearance": {"terminal": {"cursor_blinking": False}}}),
                encoding="utf-8",
            )

            settings = AppSettings.load(path)
            self.assertFalse(settings.terminal_cursor_blink)
            self.assertFalse(settings.apply_to_theme(TerminalTheme(cursor_blink=True)).cursor_blink)
            self.assertEqual(settings.terminal_options_dict()["cursor_blink"], False)

            save_terminal_options(path, cursor_blink=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            terminal = payload["appearance"]["terminal"]
            self.assertEqual(terminal["cursor_blink"], True)
            self.assertNotIn("cursor_blinking", terminal)
            self.assertTrue(AppSettings.load(path).terminal_cursor_blink)


if __name__ == "__main__":
    unittest.main()
