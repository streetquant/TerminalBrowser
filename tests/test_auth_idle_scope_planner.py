"""Negative controls for the strict descendant-only idle account planner."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from termdeck.tmux_backend import TmuxBackend, _AuthPaneIdentity


class IdleScopePlannerTests(unittest.TestCase):
    def setUp(self):
        shell = Path("/usr/bin/tcsh").stat()
        self.record = SimpleNamespace(command="/usr/bin/tcsh -f -i", cwd="/owned/cwd",
                                      codex_home="/owned/home", auth_scope_id="owned-scope")
        self.pane = _AuthPaneIdentity("owned", "%1", 101, "usr/bin/tcsh", "/owned/cwd")
        root = {"pid": 101, "ppid": 100, "start": 10, "state": b"S",
                "uid": os.geteuid(), "exe": "/usr/bin/tcsh",
                "exe_device": shell.st_dev, "exe_inode": shell.st_ino,
                "cwd": "/owned/cwd", "argv": ("-usr/bin/tcsh", "-f", "-i")}
        child = {**root, "pid": 102, "ppid": 101, "start": 11}
        payload = TmuxBackend._with_explicit_private_environment(
            self.record.command,
            (("CODEX_HOME", "/owned/home"), ("TERMDECK_AUTH_SCOPE_ID", "owned-scope"),
             ("CODEX_TERMDECK_GATE_ROOT", "/owned/home/.termdeck-codex-lifetime"),
             ("PATH", "/owned/home/bin:/usr/bin")),
        )
        root["argv"] = ("-csh", "-c", payload)
        self.nodes = (root, child)

    def check(self, nodes=None, *, after=None, pane=None, scope=True, tree=True):
        before = self.nodes if nodes is None else nodes
        following = before if after is None else after
        with mock.patch.object(Path, "resolve", lambda path, **kwargs: path), mock.patch.object(
            TmuxBackend, "_private_auth_idle_scope_snapshot", side_effect=[before, following],
        ), mock.patch.object(
            TmuxBackend, "_private_auth_process_environment_ready", return_value=scope,
        ) as scope_check, mock.patch.object(
            TmuxBackend, "_private_auth_process_tree_environment_ready", return_value=tree,
        ) as tree_check:
            result = TmuxBackend._private_auth_idle_tcsh_tree_environment_ready(
                self.record, pane or self.pane, 10,
            )
        return result, scope_check, tree_check

    def test_exact_owned_outer_inner_TCsh_uses_authoritative_tree(self):
        result, scope_check, tree_check = self.check()
        self.assertTrue(result)
        self.assertEqual(scope_check.call_args_list, [
            mock.call(self.record, 101, 10, require_private_path=False),
            mock.call(self.record, 102, 11, require_private_path=True),
        ])
        tree_check.assert_called_once_with(self.record, 101)

    def test_foreign_root_owner_refused(self):
        nodes = copy.deepcopy(self.nodes); nodes[0]["uid"] = os.geteuid() + 1
        self.assertFalse(self.check(nodes)[0])

    def test_foreign_child_owner_refused(self):
        nodes = copy.deepcopy(self.nodes); nodes[1]["uid"] = os.geteuid() + 1
        self.assertFalse(self.check(nodes)[0])

    def test_unknown_or_native_or_login_descendant_refused(self):
        for exe in ("/untrusted/wrapper", "/owned/native/codex", "/home/arch/.local/bin/codex-auth"):
            with self.subTest(exe=exe):
                nodes = copy.deepcopy(self.nodes); nodes[1]["exe"] = exe
                self.assertFalse(self.check(nodes)[0])

    def test_unknown_current_foreground_refused(self):
        for command in ("codex", "codex-auth", "untrusted", "python"):
            with self.subTest(command=command):
                pane = _AuthPaneIdentity("owned", "%1", 101, command, "/owned/cwd")
                self.assertFalse(self.check(pane=pane)[0])

    def test_wrong_child_cwd_or_parent_refused(self):
        for key, value in (("cwd", "/foreign"), ("ppid", 99)):
            with self.subTest(key=key):
                nodes = copy.deepcopy(self.nodes); nodes[1][key] = value
                self.assertFalse(self.check(nodes)[0])

    def test_wrong_root_start_or_replaced_binary_refused(self):
        for key, value in (("start", 99), ("exe_inode", self.nodes[0]["exe_inode"] + 1)):
            with self.subTest(key=key):
                nodes = copy.deepcopy(self.nodes); nodes[0][key] = value
                self.assertFalse(self.check(nodes)[0])

    def test_changed_descendant_during_predicate_refused(self):
        after = copy.deepcopy(self.nodes); after[1]["start"] += 1
        self.assertFalse(self.check(after=after)[0])

    def test_missing_or_incomplete_snapshot_refused(self):
        with mock.patch.object(TmuxBackend, "_private_auth_idle_scope_snapshot", return_value=None):
            self.assertFalse(TmuxBackend._private_auth_idle_tcsh_tree_environment_ready(self.record, self.pane, 10))
        nodes = copy.deepcopy(self.nodes); del nodes[1]["cwd"]
        self.assertFalse(self.check(nodes)[0])

    def test_private_scope_or_private_PATH_refusal_preserved(self):
        self.assertFalse(self.check(scope=False)[0])
        self.assertFalse(self.check(tree=False)[0])

    def test_foreign_argv_or_stopped_descendant_refused(self):
        for key, value in (("argv", ("/usr/bin/tcsh", "-c", "login")), ("state", b"T")):
            with self.subTest(key=key):
                nodes = copy.deepcopy(self.nodes); nodes[1][key] = value
                self.assertFalse(self.check(nodes)[0])

    def test_changed_outer_payload_or_wrong_private_PATH_refused(self):
        for argv in (
            ("-csh", "-c", "/usr/bin/tcsh -f -i"),
            ("-csh", "-c", self.nodes[0]["argv"][2].replace("/owned/home/bin:", "/foreign/bin:")),
            ("-csh", "-c", self.nodes[0]["argv"][2].replace("CODEX_HOME=/owned/home", "CODEX_HOME=/foreign")),
        ):
            with self.subTest(argv=argv):
                nodes = copy.deepcopy(self.nodes); nodes[0]["argv"] = argv
                self.assertFalse(self.check(nodes)[0])

    def test_changed_record_recipe_refused(self):
        self.record.command = "/usr/bin/tcsh -c unknown"
        self.assertFalse(self.check()[0])

    def test_changed_pane_cwd_refused(self):
        pane = _AuthPaneIdentity("owned", "%1", 101, "tcsh", "/foreign")
        self.assertFalse(self.check(pane=pane)[0])

    def test_snapshot_with_additional_or_unreadable_descendants_refused(self):
        for kids in ({102, 103}, set()):
            with self.subTest(kids=kids), mock.patch.object(Path, "iterdir", return_value=[Path("/proc/101/task/101")]):
                with mock.patch.object(Path, "open", mock.mock_open(read_data=b" ".join(str(x).encode() for x in kids))):
                    self.assertIsNone(TmuxBackend._private_auth_idle_scope_snapshot(101))
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("unavailable")):
            self.assertIsNone(TmuxBackend._private_auth_idle_scope_snapshot(101))


if __name__ == "__main__":
    unittest.main()
