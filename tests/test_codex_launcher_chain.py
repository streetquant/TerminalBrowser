import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck import codex_config_scope as scope


class CodexLauncherChainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.user = self.root / "user"
        self.bin = self.user / ".local" / "bin"
        self.libexec = self.user / ".local" / "libexec"
        self.bin.mkdir(parents=True)
        self.libexec.mkdir()
        self.private = self.root / "auth-fixture"
        self.contained = self.bin / "codex-contained-user"
        self.helper = self.libexec / "codex-contained-user.sh"
        self.router = self.bin / "codex-session-router"
        self.entry = self.bin / "codex"
        self.helper.write_text(
            "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
            "# CODEX_CANONICAL_WORKSPACE_HELPER CODEX_RUNTIME_SCOPE_HELPER CODEX_VENDOR_RUNTIME_ROOT CODEX_NPM_GLOBAL_PREFIX\n"
            'printf "%s\\n" "$@" > "$TERMDECK_CHAIN_UNIT_RECORD"\n'
        )
        self.helper.chmod(0o700)
        self.contained.write_text("#!/bin/sh\nexec " + shlex.quote(str(self.helper)) + ' "$@"\n')
        self.contained.chmod(0o700)
        self.router.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'readonly contained_codex="${CODEX_CONTAINED_USER:-' + str(self.contained) + '}"\n'
            'exec "$contained_codex" "$@"\n'
        )
        self.router.chmod(0o700)
        self.entry.symlink_to(self.router.name)
        self.router_digest = hashlib.sha256(self.router.read_bytes()).hexdigest()
        self.environment = mock.patch.dict(
            os.environ, {"HOME": str(self.user), "PATH": "/usr/bin:/bin"}, clear=True
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for patch in (
            mock.patch.object(scope.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.user))),
            mock.patch.object(scope, "_MANAGED_CODEX_ROUTER_SHA256", self.router_digest),
            mock.patch.object(scope, "_MANAGED_CODEX_COMPILED_SHA256", hashlib.sha256(self.contained.read_bytes()).hexdigest()),
            mock.patch.object(scope, "_MANAGED_CODEX_NPM_HELPER_SHA256", frozenset({hashlib.sha256(self.helper.read_bytes()).hexdigest()})),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def resolve(self):
        return scope.resolve_codex_launcher(self.private, user_home=self.user)

    def prepare(self):
        source = self.root / "source"
        source.mkdir()
        (source / "config.toml").write_text("model = 'fixture'\n")
        return scope.prepare_shared_codex_configuration(self.private, source)

    def test_reviewed_router_chain_is_recognized_without_attestor(self):
        self.assertEqual(self.resolve(), self.router)
        self.assertEqual(self.prepare().launcher_path, self.router)

    def test_direct_contained_entry_remains_recognized(self):
        self.entry.unlink()
        self.entry.symlink_to(self.contained.name)
        self.assertEqual(self.resolve(), self.contained)

    def test_reviewed_ordinary_npm_helper_is_recognized(self):
        self.helper.write_text(
            self.helper.read_text().replace("CODEX_VENDOR_RUNTIME_ROOT", "CODEX_NPM_GLOBAL_PREFIX")
        )
        digest = hashlib.sha256(self.helper.read_bytes()).hexdigest()
        with mock.patch.object(scope, "_MANAGED_CODEX_NPM_HELPER_SHA256", frozenset({digest})):
            self.assertEqual(self.resolve(), self.router)

    def test_unreviewed_ordinary_npm_helper_is_rejected(self):
        self.helper.write_text(
            self.helper.read_text().replace("CODEX_VENDOR_RUNTIME_ROOT", "CODEX_NPM_GLOBAL_PREFIX")
        )
        with mock.patch.object(scope, "_MANAGED_CODEX_NPM_HELPER_SHA256", frozenset()):
            self.assertIsNone(self.resolve())

    def test_router_cannot_borrow_the_unpinned_legacy_vendor_contract(self):
        self.helper.write_text(self.helper.read_text().replace(" CODEX_NPM_GLOBAL_PREFIX", ""))
        self.assertIsNone(self.resolve())

    def test_routed_compiled_payload_byte_change_is_rejected(self):
        self.contained.write_text(self.contained.read_text() + "# unreviewed entry change\n")
        self.assertIsNone(self.resolve())

    def test_router_byte_change_is_rejected(self):
        self.router.write_text(self.router.read_text() + "# unreviewed change\n")
        self.assertIsNone(self.resolve())

    def test_foreign_entry_copy_cannot_borrow_router_attestation(self):
        foreign = self.root / "other-bin"
        foreign.mkdir()
        (foreign / "codex").symlink_to(self.router)
        with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(foreign / "codex")}):
            with self.assertRaisesRegex(scope.CodexConfigScopeError, "not an attested"):
                self.resolve()

    def test_real_router_target_override_is_not_an_entry_attestation(self):
        with mock.patch.dict(os.environ, {"TERMDECK_CODEX_BINARY": str(self.router)}):
            with self.assertRaisesRegex(scope.CodexConfigScopeError, "not an attested"):
                self.resolve()

    def test_writable_contained_helper_and_parent_are_rejected(self):
        for path in (self.contained, self.helper, self.router, self.bin, self.libexec):
            with self.subTest(path=path.name):
                before = path.stat().st_mode & 0o777
                path.chmod(before | 0o020)
                self.assertIsNone(self.resolve())
                path.chmod(before)

    def test_symlinked_router_or_contained_payload_is_rejected(self):
        for path in (self.router, self.contained):
            with self.subTest(path=path.name):
                moved = path.with_name(path.name + "-moved")
                path.rename(moved)
                path.symlink_to(moved.name)
                self.assertIsNone(self.resolve())
                path.unlink()
                moved.rename(path)

    def test_hardlinked_router_or_helper_is_rejected(self):
        for path in (self.router, self.helper):
            with self.subTest(path=path.name):
                extra = self.root / (path.name + "-hardlink")
                os.link(path, extra)
                self.assertIsNone(self.resolve())
                extra.unlink()

    def test_helper_contract_mismatch_is_rejected(self):
        self.helper.write_text("#!/bin/sh\nexit 0\n")
        self.assertIsNone(self.resolve())

    def test_foreign_owner_is_rejected_on_the_open_descriptor(self):
        native_fstat = os.fstat
        def foreign_fstat(descriptor):
            value = list(native_fstat(descriptor))
            value[4] = os.geteuid() + 1
            return os.stat_result(value)
        with mock.patch.object(scope.os, "fstat", side_effect=foreign_fstat):
            self.assertIsNone(self.resolve())

    def test_bounded_reader_rejects_oversize_and_final_symlink(self):
        self.assertIsNone(scope._read_small_regular(self.router, maximum=4))
        link = self.root / "router-alias"
        link.symlink_to(self.router)
        self.assertIsNone(scope._read_small_regular(link, maximum=1024))

    def test_payload_replacement_during_read_is_rejected(self):
        replacement = self.root / "replacement"
        replacement.write_bytes(self.router.read_bytes())
        replacement.chmod(0o700)
        native_read = os.read
        replaced = False
        def replace_after_read(descriptor, length):
            nonlocal replaced
            data = native_read(descriptor, length)
            if not replaced:
                replaced = True
                os.replace(replacement, self.router)
            return data
        with mock.patch.object(scope.os, "read", side_effect=replace_after_read):
            self.assertIsNone(scope._read_small_regular(self.router, maximum=1024))
        self.assertTrue(replaced)

    def test_entry_change_after_chain_read_is_rejected(self):
        native_reader = scope._read_small_regular
        def change_entry(path, *, maximum):
            data = native_reader(path, maximum=maximum)
            if path == self.router:
                self.entry.unlink()
                self.entry.symlink_to(self.contained.name)
            return data
        with mock.patch.object(scope, "_read_small_regular", side_effect=change_entry):
            self.assertIsNone(self.resolve())

    def test_private_router_wrapper_keeps_native_sqlite_prefix(self):
        wrapper = self.prepare().wrapper_path
        record = self.root / "unit-argv.txt"
        env = {**os.environ, "TERMDECK_CHAIN_UNIT_RECORD": str(record)}
        result = subprocess.run([str(wrapper), "--version"], env=env, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record.read_text().splitlines(), [
            "-c", "sqlite_home=" + json.dumps(str(self.private)), "--version",
        ])

    def test_private_router_wrapper_rejects_dispatch_overrides_before_exec(self):
        wrapper = self.prepare().wrapper_path
        marker = self.root / "foreign-executed"
        foreign = self.root / "foreign"
        foreign.write_text("#!/bin/sh\n: > " + shlex.quote(str(marker)) + "\n")
        foreign.chmod(0o700)
        for key in ("CODEX_CONTAINED_USER", "CODEX_OPENROUTER"):
            for value in (str(foreign), ""):
                with self.subTest(key=key, empty=not value):
                    env = {**os.environ, key: value}
                    result = subprocess.run([str(wrapper), "--version"], env=env, capture_output=True, text=True, check=False, timeout=10)
                    self.assertEqual(result.returncode, 64)
                    self.assertIn("dispatch overrides", result.stderr)
                    self.assertFalse(marker.exists())

    def test_private_router_wrapper_rejects_runtime_overrides_before_exec(self):
        wrapper = self.prepare().wrapper_path
        record = self.root / "unit-helper-record"
        names = (
            'CODEX_CANONICAL_WORKSPACE_HELPER',
            'CODEX_RUNTIME_SCOPE_HELPER',
            'CODEX_NESTED_LAUNCH_BROKER',
            'CODEX_RESUME_RUNTIME_HELPER',
            'CODEX_RESUME_CATALOG_HELPER',
            'CODEX_WORKSPACE_HISTORY_HELPER',
            'CODEX_SHARED_APP_SERVER_CONTROL',
            'CODEX_SSD_UPDATE_HELPER',
            'CODEX_NATIVE_SESSION_CAPTURE_HELPER',
            'CODEX_DIRECT_REAL',
            'CODEX_NPM_BIN',
            'CODEX_NPM_GLOBAL_PREFIX',
            'CODEX_NESTED_LAUNCH',
            'CODEX_BROKER_CHILD',
            'CODEX_APPROVED_RUNTIME_PATH',
            'CODEX_APPROVED_RUNTIME_CACHE',
        )
        for key in names:
            for value in ("/unreviewed/unit-helper", ""):
                with self.subTest(key=key, empty=not value):
                    env = {**os.environ, key: value, "TERMDECK_CHAIN_UNIT_RECORD": str(record)}
                    result = subprocess.run([str(wrapper), "--version"], env=env, capture_output=True, text=True, check=False, timeout=10)
                    self.assertEqual(result.returncode, 64)
                    self.assertIn("runtime helper overrides", result.stderr)
                    self.assertFalse(record.exists())

    def test_direct_legacy_wrapper_preserves_existing_dispatch_contract(self):
        self.entry.unlink()
        self.entry.symlink_to(self.contained.name)
        wrapper = self.prepare().wrapper_path
        record = self.root / "legacy-unit-argv"
        env = {**os.environ, "CODEX_CONTAINED_USER": "/unused/unit-override",
               "TERMDECK_CHAIN_UNIT_RECORD": str(record)}
        result = subprocess.run([str(wrapper), "--version"], env=env, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record.read_text().splitlines(), [
            "-c", "sqlite_home=" + json.dumps(str(self.private)), "--version",
        ])

    def test_chain_change_before_wrapper_publication_is_rejected(self):
        self.router.write_text(self.router.read_text() + "# changed\n")
        with self.assertRaisesRegex(scope.CodexConfigScopeError, "chain changed"):
            scope._wrapper_content(self.private, self.router)


if __name__ == "__main__":
    unittest.main()
