from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from termdeck.models import SessionRecord
from termdeck.session_recipe import (
    SessionRecipeError,
    automatic_recovery_allowed,
    build_session_recipe,
    encode_session_recipe,
    parse_session_recipe,
    recipe_from_record,
    record_recipe_fields,
    recovery_command_specificity,
    resolve_working_directory,
    should_restart_recipe,
)


class SessionRecipeTests(unittest.TestCase):
    def test_round_trip_is_canonical_and_tool_agnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipe = build_session_recipe(
                "python3 -m http.server 8080",
                tmp,
                environment={"PORT": "8080", "MODE": "prod"},
                restart_policy="on-failure",
                origin="launch",
            )
            encoded, digest = encode_session_recipe(recipe)
            self.assertEqual(parse_session_recipe(encoded, digest), recipe)
            self.assertLess(encoded.index('"MODE"'), encoded.index('"PORT"'))
            payload = recipe.to_payload()
            self.assertNotIn("codex", str(payload["command"]).casefold())
            self.assertNotIn("claude", str(payload["command"]).casefold())
            self.assertEqual(payload["schema"], "termdeck.session-recipe.v1")

    def test_digest_or_noncanonical_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            encoded, digest = encode_session_recipe(build_session_recipe("bash", tmp))
            with self.assertRaisesRegex(SessionRecipeError, "digest mismatch"):
                parse_session_recipe(encoded + " ", digest)
            with self.assertRaisesRegex(SessionRecipeError, "digest mismatch"):
                parse_session_recipe(encoded, "0" * 64)

    def test_environment_is_explicit_bounded_and_process_local_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SessionRecipeError, "process-local"):
                build_session_recipe("bash", tmp, environment={"TMUX": "/tmp/socket"})
            with self.assertRaisesRegex(SessionRecipeError, "invalid key"):
                build_session_recipe("bash", tmp, environment={"BAD-NAME": "x"})

    def test_relative_cwd_is_made_absolute_without_requiring_it_to_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipe = build_session_recipe(
                "python worker.py",
                "missing/project",
                base_directory=tmp,
            )
            self.assertEqual(recipe.cwd, str(Path(tmp, "missing/project").resolve()))

    def test_missing_cwd_recovers_from_nearest_existing_parent_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone" / "project"
            recipe = build_session_recipe("python worker.py", missing)
            resolved = resolve_working_directory(recipe)
            self.assertTrue(resolved.degraded)
            self.assertEqual(resolved.path, Path(tmp).resolve())
            self.assertFalse(missing.exists())
            self.assertEqual(
                resolved.environment["TERMDECK_RECOVERY_ORIGINAL_CWD"],
                str(missing),
            )

    def test_restart_policy_distinguishes_crash_normal_exit_and_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            on_failure = build_session_recipe("bash", tmp, restart_policy="on-failure")
            always = build_session_recipe("bash", tmp, restart_policy="always")
            manual = build_session_recipe("bash", tmp, restart_policy="manual")
            self.assertTrue(should_restart_recipe(on_failure, session_missing=True))
            self.assertTrue(should_restart_recipe(on_failure, session_missing=False, exit_status=137))
            self.assertFalse(should_restart_recipe(on_failure, session_missing=False, exit_status=0))
            self.assertTrue(should_restart_recipe(always, session_missing=False, exit_status=0))
            self.assertFalse(automatic_recovery_allowed(manual))

    def test_opaque_resume_is_ranked_without_knowing_the_application(self) -> None:
        self.assertGreater(
            recovery_command_specificity("my-tool --resume abc"),
            recovery_command_specificity("my-tool"),
        )
        self.assertGreater(
            recovery_command_specificity("another resume thread-1"),
            recovery_command_specificity("another --verbose"),
        )

    def test_record_fallback_uses_explicit_recovery_recipe_then_original_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = SessionRecord(
                id="td-a",
                name="worker",
                tag="dev",
                command="python worker.py",
                cwd=tmp,
                tmux_name="termdeck_worker",
                created_at="now",
                last_opened_at="now",
                recovery_command="workerctl resume job-7",
                recovery_cwd=tmp,
            )
            legacy = recipe_from_record(record)
            self.assertEqual(legacy.command, "workerctl resume job-7")  # type: ignore[union-attr]

            encoded, digest, policy = record_recipe_fields(
                command=record.command,
                cwd=record.cwd,
                restart_policy="always",
            )
            persisted = replace(
                record,
                recovery_recipe=encoded,
                recovery_recipe_sha256=digest,
                restart_policy=policy,
                recovery_command="tampered legacy value",
            )
            parsed = recipe_from_record(persisted)
            self.assertEqual(parsed.command, "python worker.py")  # type: ignore[union-attr]
            self.assertEqual(parsed.restart_policy, "always")  # type: ignore[union-attr]

    def test_external_record_has_no_implicit_recovery_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = SessionRecord(
                id="td-ext",
                name="external",
                tag="external",
                command="bash",
                cwd=tmp,
                tmux_name="external",
                created_at="now",
                last_opened_at="now",
                external=True,
            )
            self.assertIsNone(recipe_from_record(record))


if __name__ == "__main__":
    unittest.main()
