import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from termdeck.checkpointing import CHECKPOINT_SCHEMA, DmtcpCheckpointManager
from termdeck.models import SessionRecord


def _record(**changes) -> SessionRecord:
    values = {
        "id": "td-checkpoint",
        "name": "worker",
        "tag": "dev",
        "command": "python worker.py",
        "cwd": "/workspace",
        "tmux_name": "termdeck_dev_worker",
        "created_at": "now",
        "last_opened_at": "now",
    }
    values.update(changes)
    return SessionRecord(**values)


class DmtcpCheckpointManagerTests(unittest.TestCase):
    @staticmethod
    def _which(name: str) -> str:
        return f"/usr/bin/{name}"

    def test_process_checkpointing_is_disabled_by_default_for_tmux_tabs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.checkpointing.shutil.which",
            side_effect=self._which,
        ):
            manager = DmtcpCheckpointManager(root=tmp)
            record = _record()

            self.assertFalse(manager.available)
            self.assertEqual(manager.wrap_launch_command(record, record.command), record.command)
            self.assertIsNone(manager.restart_command(record))
            self.assertIn("disabled for TermDeck tmux tabs", manager.unavailable_detail())
            result = manager.checkpoint(record)

        self.assertFalse(result.ok)
        self.assertFalse(result.exact)
        self.assertEqual(result.action, "unsupported")
        self.assertIn("disabled for TermDeck tmux tabs", result.detail)

    def test_wrap_launch_command_uses_one_private_coordinator_per_tab(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.checkpointing.shutil.which",
            side_effect=self._which,
        ):
            manager = DmtcpCheckpointManager(root=tmp, enabled=True)
            record = _record()
            command = manager.wrap_launch_command(record, record.command)
            argv = shlex.split(command)

        self.assertEqual(argv[0], "/usr/bin/dmtcp_launch")
        self.assertIn("--new-coordinator", argv)
        self.assertEqual(argv[argv.index("--coord-port") + 1], "0")
        self.assertEqual(argv[argv.index("--ckpt-signal") + 1], "30")
        self.assertEqual(argv[-3:], ["/bin/sh", "-lc", "python worker.py"])
        self.assertIn(str(Path(tmp) / record.id / "coordinator.port"), argv)

    def test_restart_command_requires_a_valid_local_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.checkpointing.shutil.which",
            side_effect=self._which,
        ):
            manager = DmtcpCheckpointManager(root=tmp, enabled=True)
            record = _record()
            directory = manager.session_dir(record)
            directory.mkdir(parents=True)
            image = directory / "ckpt_python_1.dmtcp"
            image.write_bytes(b"checkpoint")
            manager.manifest_file(record).write_text(
                json.dumps(
                    {
                        "schema": CHECKPOINT_SCHEMA,
                        "session_id": record.id,
                        "checkpoint_signal": manager.checkpoint_signal,
                        "files": [image.name],
                    }
                ),
                encoding="utf-8",
            )

            command = manager.restart_command(record)
            self.assertIsNotNone(command)
            argv = shlex.split(command or "")

        self.assertEqual(argv[:3], ["/usr/bin/env", "DMTCP_SIGCKPT=30", "/usr/bin/dmtcp_restart"])
        self.assertEqual(argv[-1], str(image))

    def test_checkpoint_commits_only_after_images_exist(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.checkpointing.shutil.which",
            side_effect=self._which,
        ):
            manager = DmtcpCheckpointManager(root=tmp, enabled=True)
            record = _record()
            directory = manager.session_dir(record)
            directory.mkdir(parents=True)
            manager.port_file(record).write_text("32123\n", encoding="utf-8")
            commands = []

            def create_checkpoint(*args, **_kwargs):
                commands.append(args[0])
                (directory / "ckpt_python_1.dmtcp").write_bytes(b"new-state")
                return subprocess.CompletedProcess([], 0, "checkpoint complete", "")

            with mock.patch(
                "termdeck.checkpointing.subprocess.run",
                side_effect=create_checkpoint,
            ):
                result = manager.checkpoint(record)

            manifest = json.loads(manager.manifest_file(record).read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        self.assertTrue(result.exact)
        self.assertEqual(result.image_count, 1)
        self.assertEqual(
            commands,
            [["/usr/bin/dmtcp_command", "--coord-port", "32123", "--bcheckpoint"]],
        )
        self.assertEqual(manifest["schema"], CHECKPOINT_SCHEMA)
        self.assertEqual(manifest["files"], ["ckpt_python_1.dmtcp"])

    def test_failed_checkpoint_restores_the_last_good_generation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "termdeck.checkpointing.shutil.which",
            side_effect=self._which,
        ):
            manager = DmtcpCheckpointManager(root=tmp, enabled=True)
            record = _record()
            directory = manager.session_dir(record)
            directory.mkdir(parents=True)
            manager.port_file(record).write_text("32123\n", encoding="utf-8")
            image = directory / "ckpt_python_old.dmtcp"
            image.write_bytes(b"old-state")
            old_manifest = {
                "schema": CHECKPOINT_SCHEMA,
                "session_id": record.id,
                "checkpoint_signal": manager.checkpoint_signal,
                "files": [image.name],
            }
            manager.manifest_file(record).write_text(
                json.dumps(old_manifest),
                encoding="utf-8",
            )

            failed = subprocess.CompletedProcess([], 1, "", "checkpoint rejected")
            with mock.patch(
                "termdeck.checkpointing.subprocess.run",
                return_value=failed,
            ):
                result = manager.checkpoint(record)

            restored_manifest = json.loads(
                manager.manifest_file(record).read_text(encoding="utf-8")
            )

            self.assertFalse(result.ok)
            self.assertEqual(image.read_bytes(), b"old-state")
            self.assertEqual(restored_manifest, old_manifest)


if __name__ == "__main__":
    unittest.main()
