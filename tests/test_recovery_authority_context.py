"""Benign process/metadata fixtures; no native/provider/session execution."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from termdeck import recovery_authority_context as context
from termdeck import resources
from termdeck.models import SessionRecord
from termdeck.persistence import persist_live_sessions
from termdeck.session_recipe import parse_session_recipe, record_recipe_fields
from termdeck.session_store import SessionStore
from termdeck.tmux_backend import TmuxBackend, TmuxError

THREAD = "019f71b3-fa32-7251-bac3-c628f34dd154"
OTHER = "019f71b3-fa32-7251-bac3-c628f34dd155"


class NativeFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.cwd = self.root / "workspace"
        self.home = self.root / "scope"
        self.runtime = self.root / "runtime"
        self.proc = self.root / "proc"
        self.process = self.proc / "101"
        for path in (self.cwd, self.home / "sessions", self.runtime / "thread-scope-index", self.process / "fd", self.proc / "sys/kernel/random"):
            path.mkdir(parents=True, mode=0o700)
        self.exe = self.root / "codex"
        self.exe.write_bytes(b"owned benign executable fixture, never executed\n")
        self.exe.chmod(0o700)
        (self.process / "exe").symlink_to(self.exe)
        (self.process / "cwd").symlink_to(self.cwd)
        (self.process / "cmdline").write_bytes(b"codex\0resume\0" + THREAD.encode() + b"\0")
        (self.process / "task/101").mkdir(parents=True, mode=0o700)
        (self.process / "task/101/children").write_text("")
        (self.proc / "sys/kernel/random/boot_id").write_text("fixture-boot\n")
        self.set_start(1234)
        self.set_environment({"CODEX_HOME": str(self.home), "CODEX_RUNTIME_ROOT": str(self.runtime),
                              "OPENAI_API_KEY": "private-token-must-never-be-captured"})
        self.rollout = self.home / "sessions" / ("rollout-" + THREAD + ".jsonl")
        self.header = {"type": "session_meta", "payload": {"id": THREAD, "cwd": str(self.cwd), "source": "cli"}}
        self.rollout.write_text(json.dumps(self.header) + "\n" + json.dumps({"prompt": "PRIVATE_BODY_MUST_NOT_BE_CAPTURED"}) + "\n")
        self.rollout.chmod(0o600)
        (self.process / "fd/7").symlink_to(self.rollout)
        self.command = "codex resume " + THREAD
        recipe, digest, policy = record_recipe_fields(command=self.command, cwd=str(self.cwd))
        self.record = SessionRecord("fixture", "fixture", "default", self.command, str(self.cwd), "fixture", "created", "opened",
                                    recovery_command=self.command, recovery_cwd=str(self.cwd), recovery_recipe=recipe,
                                    recovery_recipe_sha256=digest, restart_policy=policy, last_pid=101)

    def set_start(self, value):
        fields = ["S", "1", "1", "1", "0", "0"] + ["0"] * 13 + [str(value)]
        (self.process / "stat").write_text("101 (fixture process) " + " ".join(fields))

    def set_environment(self, values):
        (self.process / "environ").write_bytes(b"\0".join(os.fsencode(k + "=" + v) for k, v in values.items()) + b"\0")

    def capture(self, record=None, command=None, cwd=None):
        return context.capture_codex_context(record or self.record, 101, command or self.command, cwd or str(self.cwd), proc=str(self.proc))


class QualifiedProducerFixture(NativeFixture):
    """Explicitly approved benign fake producer; never a real native launch."""
    def __init__(self, root):
        super().__init__(root)
        self.auth = self.root / "selected-auth"
        self.auth.mkdir(mode=0o700)
        self.claim = self.root / "claim.json"
        self.claim.write_text('{"benign":"owned claim fixture"}\n')
        self.claim.chmod(0o600)
        self.lease = self.root / "lease"
        self.lease.write_text("benign owned lease fixture")
        self.lease.chmod(0o600)
        self.database = self.home / "state_5.sqlite"
        self.database.write_bytes(b"benign metadata binding, not queried")
        self.database.chmod(0o600)
        self.producer = self.root / "producer.py"
        self.module = self.root / "context-module.py"
        self.qualification = self.root / "qualification.json"
        for path in (self.producer, self.module):
            path.write_text("# benign source pin, never executed\n")
            path.chmod(0o400)
        self.response = self.root / "response.json"
        self.helper = self.root / "observe.py"
        self.helper.write_text("import json,sys\nfrom pathlib import Path\n"
            + "request=json.load(sys.stdin)\n"
            + "Path(" + repr(str(self.root / "request.json")) + ").write_text(json.dumps(request))\n"
            + "sys.stdout.write(Path(" + repr(str(self.response)) + ").read_text())\n")
        self.helper.chmod(0o400)
        self.write_qualification()
        self.observer = context.NativeContextObserver(
            str(self.helper), context.sha256(self.helper.read_bytes()), str(self.producer), context.sha256(self.producer.read_bytes()),
            str(self.module), context.sha256(self.module.read_bytes()), str(self.qualification), context.sha256(self.qualification.read_bytes()))
        self.record = replace(self.record, codex_home=str(self.auth))
        self.set_environment({"CODEX_HOME": str(self.auth), "CODEX_RUNTIME_ROOT": str(self.runtime),
            "CODEX_NATIVE_LAUNCH_CLAIM_PATH": str(self.claim), "CODEX_NATIVE_LAUNCH_CONTEXT_SHA256": "a" * 64})
        self.native = self.make_native()
        self.write_response()

    def write_qualification(self, native_version="fixture-version"):
        source = lambda path: {"path": str(path), "sha256": context.sha256(path.read_bytes())}
        value = {"schema_version": "codex.native-launch-qualification.v1", "release_id": "fixture-release",
                 "native_version": native_version, "producer_files": [source(path) for path in (self.producer, self.module, self.helper)],
                 "launcher_chain": [source(self.helper)], "native_executables": [source(self.exe)]}
        if self.qualification.exists():
            self.qualification.chmod(0o600)
        self.qualification.write_text(context.canonical(value) + "\n")
        self.qualification.chmod(0o400)

    def make_native(self):
        executable = {**context.path_binding(self.exe), "sha256": context.sha256(self.exe.read_bytes())}
        generation = {"pid": 101, "uid": os.geteuid(), "boot_id": "fixture-boot", "kernel_start_ticks": 1234,
                      "parent_pid": 100, "mount_namespace": "mnt:[fixture]"}
        bindings = {name: context.path_binding(path) for name, path in {
            "scope_home": self.home, "selected_auth_home": self.auth, "runtime_root": self.runtime,
            "workspace_root": self.cwd, "cwd": self.cwd, "history_root": self.home / "sessions",
            "sqlite_home": self.home, "catalog_database": self.database, "launcher": self.exe}.items()}
        bindings["rollout"] = {**context.path_binding(self.rollout), "header_sha256": context.sha256(context.canonical(self.header))}
        return {"schema_version": context.NATIVE_SCHEMA, "state": "observed", "code": "OBSERVED",
            "launch_authorized": False, "execution_proved": True,
            "producer": {"kind": "native.codex", "path": str(self.helper), "sha256": self.observer.observer_sha256,
                         "module_path": str(self.module), "module_sha256": self.observer.module_sha256, "api": "observe-owned-process"},
            "logical_identity": {"thread_id": THREAD, "scope_key": "fixture-scope", "scope_home": str(self.home),
                "selected_auth_home": str(self.auth), "runtime_root": str(self.runtime), "workspace_root": str(self.cwd),
                "cwd": str(self.cwd), "history_root": str(self.home / "sessions"), "sqlite_home": str(self.home),
                "scope_id": context.sha256(str(os.geteuid()) + "\0" + str(self.home) + "\0fixture-scope")},
            "bindings": bindings, "catalog": {"catalog_database": str(self.database), "native_metadata_sha256": "b" * 64,
                "database_resolution": {"effective_home": str(self.home), "resolution_source": "explicit-scope-home",
                    "configuration_scope": "owned-fixture", "database_files": [str(self.database)], "query_mode": "read-only"}},
            "launcher": {"qualification_state": "qualified", "qualification_sha256": self.observer.qualification_sha256,
                "qualification_schema": "codex.native-launch-qualification.v1", "trust_anchor": "consumer-approved-qualification-sha256",
                "release_id": "fixture-release", "native_version": "fixture-version", "chain": [executable], "native_executable": executable},
            "observation": generation, "owner": {"generation": {**generation, "pid": 100, "parent_pid": 1},
                "lease": context.path_binding(self.lease), "claim_path": str(self.claim), "claim_sha256": context.sha256(self.claim.read_bytes()),
                "enrollment": {"source": "owned-native-registration", "thread_id": THREAD,
                               "binding": context.path_binding(self.claim), "sha256": context.sha256(self.claim.read_bytes())}},
            "missing": []}

    def write_response(self):
        self.response.write_text(context.canonical(self.native) + "\n")

    def capture(self, record=None):
        return context.capture_codex_context(record or self.record, 101, self.command, str(self.cwd),
                                             proc=str(self.proc), native_observer=self.observer)


class ProducerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = QualifiedProducerFixture(self.tmp.name)

    def test_approved_observer_captures_original_a_and_selected_b_without_credential_inference(self):
        fixture = self.fixture
        value = context.parse_context(*fixture.capture())
        self.assertEqual(value["selectors"]["CODEX_HOME"], str(fixture.auth))
        self.assertEqual(value["selected_auth_home"], str(fixture.auth))
        self.assertEqual(value["bindings"]["scope_home"]["path"], str(fixture.home))
        self.assertEqual(value["bindings"]["sqlite_home"]["path"], str(fixture.home))
        self.assertEqual(value["bindings"]["history_root"]["path"], str(fixture.home / "sessions"))
        self.assertTrue(value["native_producer"]["execution_proved"])
        self.assertFalse(value["native_producer"]["launch_authorized"])
        self.assertIsNone(value["scope_producer"])
        request = json.loads((fixture.root / "request.json").read_text())
        self.assertEqual(request["expected_thread_id"], THREAD)
        self.assertEqual(request["qualification"]["sha256"], fixture.observer.qualification_sha256)
        self.assertEqual(value["native_producer"]["producer"]["path"], fixture.observer.observer_path)
        self.assertNotEqual(fixture.observer.observer_path, fixture.observer.producer_path)

    def test_observed_scope_owner_cannot_impersonate_observer_role(self):
        self.fixture.native["producer"]["path"] = self.fixture.observer.producer_path
        self.fixture.native["producer"]["sha256"] = self.fixture.observer.producer_sha256
        self.fixture.write_response()
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "approved provenance"):
            self.fixture.capture()

    def test_approved_qualification_must_cover_owner_and_module_before_invocation(self):
        fixture = self.fixture
        for omitted in (fixture.producer, fixture.module):
            with self.subTest(omitted=omitted.name):
                fixture.write_qualification()
                value = json.loads(fixture.qualification.read_text())
                value["producer_files"] = [pin for pin in value["producer_files"] if pin["path"] != str(omitted)]
                fixture.qualification.chmod(0o600)
                fixture.qualification.write_text(context.canonical(value) + "\n")
                observer = replace(fixture.observer, qualification_sha256=context.sha256(fixture.qualification.read_bytes()))
                with mock.patch.object(context.NativeContextObserver, "_invoke") as invoke:
                    with self.assertRaisesRegex(context.RecoveryAuthorityError, "does not cover"):
                        observer.observe(context.process_generation(101, proc=str(fixture.proc)), str(fixture.claim), "a" * 64, THREAD, str(fixture.cwd))
                invoke.assert_not_called()

    def test_nonobject_qualification_isolated_before_invocation(self):
        fixture = self.fixture
        fixture.qualification.chmod(0o600)
        fixture.qualification.write_text("[]\n")
        observer = replace(fixture.observer, qualification_sha256=context.sha256(fixture.qualification.read_bytes()))
        with mock.patch.object(context.NativeContextObserver, "_invoke") as invoke:
            with self.assertRaisesRegex(context.RecoveryAuthorityError, "does not cover"):
                observer.observe(context.process_generation(101, proc=str(fixture.proc)), str(fixture.claim), "a" * 64, THREAD, str(fixture.cwd))
        invoke.assert_not_called()

    def test_child_selector_cannot_replace_consumer_approved_qualification(self):
        self.fixture.native["launcher"]["qualification_sha256"] = "c" * 64
        self.fixture.write_response()
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "approved provenance"):
            self.fixture.capture()

    def test_header_and_matching_digest_cannot_replace_owned_enrollment(self):
        self.fixture.native["owner"].pop("enrollment")
        self.fixture.write_response()
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "owner evidence"):
            self.fixture.capture()

    def test_full_producer_still_requires_a_current_owner_predicate_at_store_boundary(self):
        fixture = self.fixture
        store = SessionStore(fixture.root / "sessions.sqlite3")
        self.addCleanup(store.close)
        store.upsert(fixture.record)
        record = store.get("fixture")
        pair = fixture.capture(record)
        observation = context.AuthorityObservation(context.record_cas(record), *pair)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "current observed owner proof"):
            store.update_recovery_metadata([(record.id, fixture.command, str(fixture.cwd))],
                                           authority_observations={record.id: observation})
        self.assertEqual(store.get(record.id), record)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "metadata CAS path"):
            store.upsert(replace(record, recovery_authority_context=pair[0], recovery_authority_context_sha256=pair[1]))

    def test_foreign_native_thread_cwd_or_pid_cannot_attach_context(self):
        for section, key, value in (("logical_identity", "thread_id", OTHER), ("logical_identity", "cwd", str(self.fixture.root)),
                                    ("observation", "kernel_start_ticks", 5678)):
            with self.subTest(key=key):
                self.fixture.native = self.fixture.make_native()
                self.fixture.native[section][key] = value
                self.fixture.write_response()
                with self.assertRaises(context.RecoveryAuthorityError):
                    self.fixture.capture()

    def test_unavailable_or_prepared_producer_never_grants_observed_authority(self):
        self.fixture.native = {"schema_version": context.NATIVE_SCHEMA, "state": "unavailable", "code": "MISSING_AUTHORITY",
            "launch_authorized": False, "execution_proved": False, "missing": ["registered-thread"]}
        self.fixture.write_response()
        # With no qualified original home, the auth-home path cannot turn the
        # foreign open rollout into partial original-history authority.
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "history home"):
            self.fixture.capture()
        self.fixture.native = self.fixture.make_native()
        self.fixture.native["state"] = "prepared"
        self.fixture.native["execution_proved"] = False
        self.fixture.write_response()
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "incomplete observed"):
            self.fixture.capture()

    def test_concurrent_approved_source_change_during_observe_is_rejected(self):
        real_invoke = self.fixture.observer._invoke
        def invoke(request):
            result = real_invoke(request)
            self.fixture.module.chmod(0o600)
            self.fixture.module.write_text("# concurrent owned source writer\n")
            return result
        with mock.patch.object(context.NativeContextObserver, "_invoke", side_effect=invoke):
            with self.assertRaisesRegex(context.RecoveryAuthorityError, "source changed"):
                self.fixture.capture()

    def test_timeout_and_output_bound_reap_only_owned_fake_observer(self):
        fixture = self.fixture
        for body, message in (("import time; time.sleep(5)\n", "deadline"), ("import sys; sys.stdout.write('x'*70000)\n", "output exceeded")):
            with self.subTest(message=message):
                fixture.helper.chmod(0o600)
                fixture.helper.write_text(body)
                fixture.write_qualification()
                observer = replace(fixture.observer, observer_sha256=context.sha256(fixture.helper.read_bytes()),
                                   qualification_sha256=context.sha256(fixture.qualification.read_bytes()), timeout=0.1)
                started = time.monotonic()
                with self.assertRaisesRegex(context.RecoveryAuthorityError, message):
                    observer.observe(context.process_generation(101, proc=str(fixture.proc)), str(fixture.claim), "a" * 64, THREAD, str(fixture.cwd))
                self.assertLess(time.monotonic() - started, 1)

    def test_explicit_activation_manifest_must_match_owning_environment_pin(self):
        value = {"schema_version": "termdeck.native-context-observer.v1"}
        for name in ("observer", "producer", "module", "qualification"):
            value[name] = {"path": getattr(self.fixture.observer, name + "_path"), "sha256": getattr(self.fixture.observer, name + "_sha256")}
        path = self.fixture.root / "activation.json"
        path.write_text(context.canonical(value) + "\n")
        path.chmod(0o400)
        env = {"TERMDECK_NATIVE_CONTEXT_CONFIG": str(path), "TERMDECK_NATIVE_CONTEXT_CONFIG_SHA256": context.sha256(path.read_bytes())}
        self.assertEqual(context.NativeContextObserver.from_environment(env), self.fixture.observer)
        path.chmod(0o600)
        path.write_text(context.canonical({**value, "schema_version": "foreign"}) + "\n")
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "activation digest"):
            context.NativeContextObserver.from_environment(env)
        self.assertIsNone(context.NativeContextObserver.from_environment({"CODEX_NATIVE_LAUNCH_CLAIM_PATH": str(path)}))

    def test_weak_same_generation_retains_original_context_but_reuse_and_foreign_replacement_block(self):
        pair = self.fixture.capture()
        weak = context.parse_context(*pair)
        weak["native_producer"] = None
        self.assertEqual(context.retain_stronger_context(pair, context.encode_context(weak)), pair)
        weak["observation"]["start_ticks"] += 1
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "new native generation"):
            context.retain_stronger_context(pair, context.encode_context(weak))
        foreign = context.parse_context(*pair)
        foreign["bindings"]["scope_home"]["inode"] += 1
        foreign["native_producer"]["bindings"]["scope_home"]["inode"] += 1
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "supported continuity"):
            context.retain_stronger_context(pair, context.encode_context(foreign))

    def test_reviewed_runtime_update_can_replace_launcher_without_changing_original_scope(self):
        previous = self.fixture.capture()
        self.fixture.exe.write_bytes(b"benign qualified runtime upgrade fixture")
        self.fixture.write_qualification("fixture-version-new")
        self.fixture.observer = replace(self.fixture.observer,
            qualification_sha256=context.sha256(self.fixture.qualification.read_bytes()))
        self.fixture.native = self.fixture.make_native()
        self.fixture.native["launcher"]["native_version"] = "fixture-version-new"
        self.fixture.write_response()
        current = self.fixture.capture()
        self.assertEqual(context.retain_stronger_context(previous, current), current)

    def test_registered_observation_enrolls_context_under_existing_row_cas(self):
        fixture = self.fixture
        store = SessionStore(fixture.root / "sessions.sqlite3")
        self.addCleanup(store.close)
        store.upsert(fixture.record)
        record = store.get("fixture")
        backend = TmuxBackend(native_context_observer=fixture.observer)
        with mock.patch.object(resources, "_PROC", str(fixture.proc)), \
                mock.patch.object(backend, "pane_pids", return_value={record.id: 101}):
            batch = backend.recovery_observations([record])
            self.assertEqual(store.update_recovery_metadata([(record.id, *batch.metadata[record.id])],
                                                           authority_observations=batch.authority), 1)
        self.assertIsNotNone(context.parse_context(store.get("fixture").recovery_authority_context,
                                                  store.get("fixture").recovery_authority_context_sha256)["native_producer"])

    def test_cold_recipe_validation_does_not_promote_prior_boot_to_live_owner(self):
        fixture = self.fixture
        pair = fixture.capture()
        record = replace(fixture.record, recovery_authority_context=pair[0], recovery_authority_context_sha256=pair[1])
        (fixture.proc / "sys/kernel/random/boot_id").write_text("next-fixture-boot\n")
        self.assertTrue(context.context_matches_record(record))
        self.assertFalse(context.observation_is_current(record, *pair, proc=str(fixture.proc), native_observer=fixture.observer))

    def test_fresh_generic_launch_observes_new_uuid_before_derived_metadata_exists(self):
        fixture = self.fixture
        recipe, digest, policy = record_recipe_fields(command="codex", cwd=str(fixture.cwd))
        fixture.record = replace(fixture.record, command="codex", recovery_command=None, recovery_cwd=None,
                                 recovery_recipe=recipe, recovery_recipe_sha256=digest, restart_policy=policy,
                                 status="running")
        (fixture.process / "cmdline").write_bytes(b"codex\0")
        fixture.native["catalog"]["native_metadata_sha256"] = None
        fixture.write_response()
        with SessionStoreClosing(fixture.root / "fresh.sqlite3") as store:
            store.upsert(fixture.record)
            record = store.get("fixture")
            backend = TmuxBackend(native_context_observer=fixture.observer)
            with mock.patch.object(resources, "_PROC", str(fixture.proc)), \
                    mock.patch.object(backend, "pane_pids", return_value={record.id: 101}):
                batch = backend.recovery_observations([record])
                self.assertEqual(store.update_recovery_metadata([(record.id, *batch.metadata[record.id])],
                                                               authority_observations=batch.authority), 1)
            after = store.get(record.id)
            self.assertEqual(after.command, "codex")
            self.assertEqual(after.recovery_command, fixture.command)
            value = context.parse_context(after.recovery_authority_context, after.recovery_authority_context_sha256)
            self.assertEqual(value["thread_id"], THREAD)
            self.assertIsNone(value["native_producer"]["catalog"]["native_metadata_sha256"])
            self.assertFalse(value["native_producer"]["launch_authorized"])


class GtkCaptureWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = QualifiedProducerFixture(self.tmp.name)
        self.path = self.fixture.root / "gtk-worker.sqlite3"
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.store.upsert(replace(self.fixture.record, status="running"))
        self.record = self.store.get("fixture")
        self.backend = TmuxBackend(native_context_observer=self.fixture.observer)

    def window(self):
        tab = SimpleNamespace(record=self.record)
        return SimpleNamespace(tabs={self.record.id: tab}, app_context=SimpleNamespace(store=self.store, backend=self.backend),
            side=SimpleNamespace(refresh=lambda: None), master_window=None, refresh_tab_label=lambda value: setattr(tab, "record", value))

    def test_slow_owned_observer_keeps_gtk_refresh_responsive_and_single_in_flight(self):
        import threading
        from termdeck.gtk_app import TermDeckWindow
        entered, released = threading.Event(), threading.Event()
        actual = context.NativeContextObserver._invoke
        def slow(observer, request):
            entered.set()
            if not released.wait(timeout=1):
                raise context.RecoveryAuthorityError("fixture observer release deadline")
            return actual(observer, request)
        with mock.patch.object(context.NativeContextObserver, "_invoke", slow), \
                mock.patch.object(resources, "_PROC", str(self.fixture.proc)), \
                mock.patch.object(TmuxBackend, "pane_pids", side_effect=lambda rows: {row.id: 101 for row in rows}), \
                mock.patch.object(self.backend, "current_directories", return_value={}):
            window = self.window()
            try:
                started = time.monotonic()
                self.assertEqual(TermDeckWindow.refresh_tab_directories(window), 0)
                self.assertLess(time.monotonic() - started, 0.1)
                self.assertTrue(entered.wait(timeout=1))
                first = self.backend._native_context_job
                self.assertFalse(first.daemon)
                self.assertEqual(TermDeckWindow.refresh_tab_directories(window), 0)
                self.assertIs(self.backend._native_context_job, first)
            finally:
                released.set()
                if self.backend._native_context_job is not None:
                    self.backend._native_context_job.join(timeout=2)
            self.assertFalse(self.backend._native_context_job.is_alive())
        self.assertEqual(self.backend._native_context_job_updated, 1)
        self.assertIsNotNone(self.store.get("fixture").recovery_authority_context)

    def test_background_observation_cannot_overwrite_concurrent_intent(self):
        import threading
        entered, released = threading.Event(), threading.Event()
        actual = context.NativeContextObserver._invoke
        def slow(observer, request):
            entered.set()
            if not released.wait(timeout=1):
                raise context.RecoveryAuthorityError("fixture observer release deadline")
            return actual(observer, request)
        with mock.patch.object(context.NativeContextObserver, "_invoke", slow), \
                mock.patch.object(resources, "_PROC", str(self.fixture.proc)), \
                mock.patch.object(TmuxBackend, "pane_pids", return_value={self.record.id: 101}):
            try:
                self.assertTrue(self.backend.queue_native_context_observation(self.path, [self.record]))
                self.assertTrue(entered.wait(timeout=1))
                self.store.update(self.record.id, name="new concurrent owner intent")
            finally:
                released.set()
                if self.backend._native_context_job is not None:
                    self.backend._native_context_job.join(timeout=2)
            self.assertFalse(self.backend._native_context_job.is_alive())
        self.assertEqual(self.backend._native_context_job_updated, 0)
        self.assertEqual(self.store.get(self.record.id).name, "new concurrent owner intent")
        self.assertIsNone(self.store.get(self.record.id).recovery_authority_context)

    def test_shutdown_does_not_invoke_qualified_observer_or_erase_retained_context(self):
        pair = self.fixture.capture(self.record)
        observation = context.AuthorityObservation(context.record_cas(self.record), *pair, is_current=lambda: True)
        self.store.update_recovery_metadata([(self.record.id, self.fixture.command, str(self.fixture.cwd))],
                                           authority_observations={self.record.id: observation})
        before = self.store.get(self.record.id)
        with mock.patch.object(resources, "_PROC", str(self.fixture.proc)), \
                mock.patch.object(self.backend, "pane_pids", return_value={self.record.id: 101}), \
                mock.patch.object(context.NativeContextObserver, "_invoke", side_effect=AssertionError("no helper at shutdown")), \
                mock.patch.object(self.backend, "checkpoint_records", return_value=()):
            result = persist_live_sessions(self.store, self.backend, records=[before])
        self.assertEqual(result.metadata_updates, 0)
        self.assertEqual(self.store.get(self.record.id), before)


class WebCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = QualifiedProducerFixture(self.tmp.name)

    def test_absent_activation_does_not_open_or_create_database(self):
        from termdeck import cli
        missing = self.fixture.root / "absent.sqlite3"
        with mock.patch.object(context.NativeContextObserver, "from_environment", return_value=None), \
                mock.patch.object(cli, "SessionStore", side_effect=AssertionError("store must stay unopened")), \
                mock.patch.object(cli, "_print_json") as output:
            self.assertEqual(cli.cmd_observe_recovery_context(SimpleNamespace(db=missing, cursor=0, limit=1)), 0)
        self.assertFalse(missing.exists())
        self.assertFalse(output.call_args.args[0]["enabled"])

    def test_web_command_captures_shared_batch_and_rotates_only_one_row(self):
        from termdeck import cli
        fixture = self.fixture
        path = fixture.root / "web.sqlite3"
        with SessionStoreClosing(path) as store:
            store.upsert(replace(fixture.record, status="running"))
            store.upsert(replace(fixture.record, id="second", tmux_name="second", status="running"))
            store.upsert(replace(fixture.record, id="stopped", tmux_name="stopped", status="stopped"))
            before = {record.id: record for record in store.list()}
        backend = TmuxBackend(native_context_observer=fixture.observer)
        with mock.patch.object(context.NativeContextObserver, "from_environment", return_value=fixture.observer), \
                mock.patch.object(cli, "TmuxBackend", return_value=backend), \
                mock.patch.object(resources, "_PROC", str(fixture.proc)), \
                mock.patch.object(backend, "pane_pids", side_effect=lambda rows: {row.id: 101 for row in rows}), \
                mock.patch.object(cli, "_print_json") as output:
            self.assertEqual(cli.cmd_observe_recovery_context(SimpleNamespace(db=path, cursor=0, limit=1)), 0)
        self.assertEqual(output.call_args.args[0]["updated"], 1)
        self.assertEqual(output.call_args.args[0]["selected"], 1)
        self.assertEqual(output.call_args.args[0]["next_cursor"], 1)
        with SessionStoreClosing(path) as store:
            after = store.get("fixture")
            self.assertIsNotNone(context.parse_context(after.recovery_authority_context,
                                                      after.recovery_authority_context_sha256)["native_producer"])
            self.assertEqual(replace(after, recovery_authority_context=None, recovery_authority_context_sha256=None), before["fixture"])
            self.assertEqual(store.get("second"), before["second"])
            self.assertEqual(store.get("stopped"), before["stopped"])

    def test_web_capture_rejects_concurrent_row_change_under_shared_cas(self):
        from termdeck import cli
        fixture = self.fixture
        path = fixture.root / "web-cas.sqlite3"
        with SessionStoreClosing(path) as store:
            store.upsert(replace(fixture.record, status="running"))
        backend = TmuxBackend(native_context_observer=fixture.observer)
        real = backend.recovery_observations
        def raced(records):
            batch = real(records)
            with SessionStoreClosing(path) as writer:
                writer.update("fixture", name="concurrent owner fixture")
            return batch
        with mock.patch.object(context.NativeContextObserver, "from_environment", return_value=fixture.observer), \
                mock.patch.object(cli, "TmuxBackend", return_value=backend), \
                mock.patch.object(resources, "_PROC", str(fixture.proc)), \
                mock.patch.object(backend, "pane_pids", return_value={"fixture": 101}), \
                mock.patch.object(backend, "recovery_observations", side_effect=raced), \
                mock.patch.object(cli, "_print_json") as output:
            self.assertEqual(cli.cmd_observe_recovery_context(SimpleNamespace(db=path, cursor=0, limit=1)), 0)
        self.assertEqual(output.call_args.args[0]["updated"], 0)
        with SessionStoreClosing(path) as store:
            self.assertEqual(store.get("fixture").name, "concurrent owner fixture")
            self.assertIsNone(store.get("fixture").recovery_authority_context)

    def test_web_capture_partial_observation_preserves_prior_recipe(self):
        from termdeck import cli
        fixture = self.fixture
        path = fixture.root / "web-partial.sqlite3"
        with SessionStoreClosing(path) as store:
            store.upsert(replace(fixture.record, status="running"))
            before = store.get("fixture")
        batch = context.RecoveryObservationBatch({"fixture": (None, None)}, {})
        backend = SimpleNamespace(recovery_observations=lambda rows: batch)
        with mock.patch.object(context.NativeContextObserver, "from_environment", return_value=fixture.observer), \
                mock.patch.object(cli, "TmuxBackend", return_value=backend), mock.patch.object(cli, "_print_json") as output:
            self.assertEqual(cli.cmd_observe_recovery_context(SimpleNamespace(db=path, cursor=0, limit=1)), 0)
        self.assertEqual(output.call_args.args[0]["updated"], 0)
        with SessionStoreClosing(path) as store:
            self.assertEqual(store.get("fixture"), before)


@contextmanager
def SessionStoreClosing(path):
    store = SessionStore(path)
    try:
        yield store
    finally:
        store.close()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = NativeFixture(self.tmp.name)

    def test_observed_boundary_retains_nonsecret_original_home_and_header(self):
        encoded, digest = self.fixture.capture()
        value = context.parse_context(encoded, digest)
        self.assertEqual(value["thread_id"], THREAD)
        self.assertEqual(value["bindings"]["scope_home"]["path"], str(self.fixture.home))
        self.assertEqual(value["bindings"]["history_root"]["path"], str(self.fixture.home / "sessions"))
        self.assertEqual(value["bindings"]["rollout"]["header_sha256"], context.sha256(context.canonical(self.fixture.header)))
        self.assertNotIn("PRIVATE_BODY", encoded)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("OPENAI_API_KEY", encoded)
        self.assertIsNone(value["selected_auth_home"])
        self.assertIsNone(value["native_producer"])
        self.assertNotIn("role", value["bindings"]["launcher"])

    def test_sqlite_selector_does_not_override_unobserved_native_configuration(self):
        self.fixture.set_environment({"CODEX_HOME": str(self.fixture.home), "CODEX_SQLITE_HOME": str(self.fixture.cwd)})
        value = context.parse_context(*self.fixture.capture())
        self.assertEqual(value["selectors"]["CODEX_SQLITE_HOME"], str(self.fixture.cwd))
        self.assertNotIn("sqlite_home", value["bindings"])

    def test_absent_selector_is_explicit_and_not_inferred_from_selected_home(self):
        self.fixture.set_environment({})
        (self.fixture.process / "fd/7").unlink()
        value = context.parse_context(*self.fixture.capture())
        self.assertTrue(all(v is None for v in value["selectors"].values()))
        self.assertNotIn("scope_home", value["bindings"])
        self.assertNotIn("sqlite_home", value["bindings"])

    def test_pid_reuse_during_observation_is_rejected(self):
        generation = context.process_generation(101, proc=str(self.fixture.proc))
        with mock.patch.object(context, "process_generation", side_effect=[generation, {**generation, "start_ticks": 1235}]):
            with self.assertRaisesRegex(context.RecoveryAuthorityError, "generation changed"):
                self.fixture.capture()

    def test_foreign_rollout_cannot_supply_history_authority(self):
        other = self.fixture.root / "foreign"
        other.mkdir(mode=0o700)
        target = other / self.fixture.rollout.name
        target.write_text(json.dumps(self.fixture.header) + "\n")
        target.chmod(0o600)
        (self.fixture.process / "fd/7").unlink()
        (self.fixture.process / "fd/7").symlink_to(target)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "history home"):
            self.fixture.capture()

    def test_launcher_update_is_observed_without_assigning_qualification(self):
        original = context.parse_context(*self.fixture.capture())["bindings"]["launcher"]
        self.fixture.exe.write_bytes(b"benign upgraded fixture, never executed")
        current = context.parse_context(*self.fixture.capture())["bindings"]["launcher"]
        self.assertEqual(context.physical_drift(original, current), "RUNTIME_UPDATE_REQUIRES_QUALIFICATION")

    def test_equivalent_remount_is_observation_not_foreign_home(self):
        binding = context.path_binding(self.fixture.home)
        self.assertEqual(context.physical_drift(binding, {**binding, "filesystemId": binding["filesystemId"] + 1}),
                         "REMOUNT_OBSERVATION_REQUIRES_NATIVE_CONTINUITY")
        self.assertEqual(context.physical_drift(binding, {**binding, "inode": binding["inode"] + 1}),
                         "PHYSICAL_REPLACEMENT_REQUIRES_NATIVE_CONTINUITY")
        self.assertEqual(context.physical_drift(binding, {**binding, "path": str(self.fixture.cwd)}), "FOREIGN_BINDING")

    def test_digest_cannot_grant_managed_native_authority(self):
        value = context.parse_context(*self.fixture.capture())
        value["bindings"]["launcher"]["role"] = "managed-native"
        with self.assertRaises(context.RecoveryAuthorityError):
            context.encode_context(value)

    def test_duplicate_json_keys_are_rejected_even_with_matching_sha(self):
        encoded, _ = self.fixture.capture()
        changed = encoded[:-1] + ',"application":"codex"}'
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "JSON"):
            context.parse_context(changed, context.sha256(changed))

    def test_scope_producer_contradiction_is_rejected(self):
        path = self.fixture.runtime / "thread-scope-index" / ("thread-" + context.sha256(THREAD) + ".json")
        path.write_text(json.dumps({"schemaVersion": "codex.direct-runtime-scope.v3", "threadId": OTHER}))
        path.chmod(0o600)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "producer contradicts"):
            self.fixture.capture()

    def test_record_recipe_or_auth_home_change_blocks_retained_context(self):
        pair = self.fixture.capture()
        record = replace(self.fixture.record, recovery_authority_context=pair[0], recovery_authority_context_sha256=pair[1])
        self.assertTrue(context.context_matches_record(record))
        self.assertFalse(context.context_matches_record(replace(record, codex_home=str(self.fixture.home))))
        recipe, digest, _ = record_recipe_fields(command="codex resume " + OTHER, cwd=str(self.fixture.cwd))
        self.assertFalse(context.context_matches_record(replace(record, recovery_recipe=recipe, recovery_recipe_sha256=digest)))

    def test_non_object_header_is_a_precise_safe_failure(self):
        self.fixture.rollout.write_text("[]\n")
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "not an object"):
            self.fixture.capture()

    def test_non_object_scope_metadata_is_a_precise_safe_failure(self):
        path = self.fixture.runtime / "thread-scope-index" / ("thread-" + context.sha256(THREAD) + ".json")
        path.write_text("[]\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "scope producer metadata is not an object"):
            self.fixture.capture()

    def test_short_or_foreign_stat_does_not_escape_as_index_error(self):
        for value in ("101 (fixture) S 1", "102 (fixture) " + " ".join(["0"] * 20), "not a stat"):
            with self.subTest(value=value):
                (self.fixture.process / "stat").write_text(value)
                with self.assertRaisesRegex(context.RecoveryAuthorityError, "invalid process generation"):
                    self.fixture.capture()

    def test_source_replacement_during_read_is_rejected(self):
        real_fdopen = os.fdopen
        target = self.fixture.exe

        @contextmanager
        def changed_file(descriptor, *args, **kwargs):
            with real_fdopen(descriptor, *args, **kwargs) as original:
                class Reader:
                    def read(self, count):
                        result = original.read(count)
                        target.write_bytes(b"concurrent owned source writer")
                        return result
                yield Reader()

        with mock.patch.object(context.os, "fdopen", changed_file):
            with self.assertRaisesRegex(context.RecoveryAuthorityError, "changed during observation"):
                context._read_bound(target, 1024)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = NativeFixture(self.tmp.name)
        self.store = SessionStore(Path(self.tmp.name) / "sessions.sqlite3")
        self.addCleanup(self.store.close)
        self.store.upsert(self.fixture.record)

    def observation(self, record=None):
        record = record or self.store.get("fixture")
        pair = self.fixture.capture(record)
        return context.AuthorityObservation(context.record_cas(record), *pair)

    def enroll(self):
        return self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd))],
                                                   authority_observations={"fixture": self.observation()})

    def test_optional_enrollment_preserves_v1_bytes_windows_and_every_other_field(self):
        before = self.store.get("fixture")
        windows = [tuple(row) for row in self.store.conn.execute("SELECT * FROM remembered_windows")]
        self.assertEqual(self.enroll(), 1)
        after = self.store.get("fixture")
        self.assertEqual(replace(after, recovery_authority_context=None, recovery_authority_context_sha256=None), before)
        self.assertEqual([tuple(row) for row in self.store.conn.execute("SELECT * FROM remembered_windows")], windows)
        self.assertTrue(context.context_matches_record(after))

    def test_same_owner_exact_duplicate_is_idempotent(self):
        self.enroll()
        self.assertEqual(self.enroll(), 0)

    def test_current_row_cas_prevents_async_observation_overwrite(self):
        observation = self.observation()
        self.store.update("fixture", name="peer changed name", last_opened_at="opened")
        before = self.store.get("fixture")
        self.assertEqual(self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd))],
                         authority_observations={"fixture": observation}), 0)
        self.assertEqual(self.store.get("fixture"), before)

    def test_changed_uuid_invalidates_context_without_reusing_old_home_proof(self):
        self.enroll()
        self.store.update_recovery_metadata([("fixture", "codex resume " + OTHER, str(self.fixture.cwd))])
        self.assertIsNone(self.store.get("fixture").recovery_authority_context)

    def test_changed_cwd_invalidates_context(self):
        self.enroll()
        self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.root))])
        self.assertIsNone(self.store.get("fixture").recovery_authority_context)

    def test_changed_intended_command_invalidates_even_when_exact_resume_is_retained(self):
        self.enroll()
        self.store.update("fixture", command="another application")
        after = self.store.get("fixture")
        self.assertEqual(after.recovery_command, self.fixture.command)
        self.assertIsNone(after.recovery_authority_context)

    def test_reused_pid_before_store_commit_cannot_attach_context(self):
        record = self.store.get("fixture")
        pair = self.fixture.capture(record)
        observation = context.AuthorityObservation(context.record_cas(record), *pair,
                        is_current=lambda: context.observation_is_current(record, *pair, proc=str(self.fixture.proc)))
        self.fixture.set_start(1235)
        self.assertEqual(self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd))],
                         authority_observations={"fixture": observation}), 0)
        self.assertIsNone(self.store.get("fixture").recovery_authority_context)

    def test_shutdown_exit_keeps_historical_context_without_current_process_admission(self):
        self.enroll()
        before = self.store.get("fixture")
        class Backend:
            def recovery_observations(self, records, bounded=True):
                return context.RecoveryObservationBatch({"fixture": (None, None)}, {})
        result = persist_live_sessions(self.store, Backend(), records=[before])
        after = self.store.get("fixture")
        self.assertEqual(result.metadata_updates, 0)
        self.assertEqual(after, before)
        (self.fixture.process / "stat").unlink()
        self.assertTrue(context.context_matches_record(after))
        self.assertFalse(context.observation_is_current(after, after.recovery_authority_context,
                                                        after.recovery_authority_context_sha256, proc=str(self.fixture.proc)))

    def test_foreign_observed_home_requires_explicit_native_continuity(self):
        self.enroll()
        current = self.store.get("fixture")
        value = context.parse_context(*self.fixture.capture(current))
        value["bindings"]["scope_home"]["path"] = str(self.fixture.cwd)
        pair = context.encode_context(value)
        observation = context.AuthorityObservation(context.record_cas(current), *pair)
        with self.assertRaisesRegex(context.RecoveryAuthorityError, "continuity required"):
            self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd))],
                                               authority_observations={"fixture": observation})
        self.assertEqual(self.store.get("fixture"), current)

    def test_changed_policy_invalidates_context_but_runtime_update_keeps_original_evidence(self):
        self.enroll()
        self.store.update("fixture", status="running")
        self.assertIsNotNone(self.store.get("fixture").recovery_authority_context)
        self.store.update("fixture", restart_policy="never")
        self.assertIsNone(self.store.get("fixture").recovery_authority_context)

    def test_context_loss_clears_only_context_and_keeps_exact_resume(self):
        self.enroll()
        before = self.store.get("fixture")
        loss = context.AuthorityObservation(context.record_cas(before), None, None)
        self.assertEqual(self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd))],
                         authority_observations={"fixture": loss}), 1)
        self.assertEqual(self.store.get("fixture"), replace(before, recovery_authority_context=None, recovery_authority_context_sha256=None))

    def test_bad_context_rolls_back_entire_batch_before_any_write(self):
        record = replace(self.fixture.record, id="second", tmux_name="second")
        self.store.upsert(record)
        record = self.store.get("second")
        before = [tuple(row) for row in self.store.conn.execute("SELECT * FROM sessions ORDER BY id")]
        bad = context.AuthorityObservation(context.record_cas(record), "{}", "0" * 64)
        with self.assertRaises(context.RecoveryAuthorityError):
            self.store.update_recovery_metadata([("fixture", self.fixture.command, str(self.fixture.cwd)),
                                                ("second", self.fixture.command, str(self.fixture.cwd))],
                                               authority_observations={"fixture": self.observation(), "second": bad})
        self.assertEqual([tuple(row) for row in self.store.conn.execute("SELECT * FROM sessions ORDER BY id")], before)

    def test_normal_creation_does_not_synthesize_historical_authority(self):
        created = self.store.create("another", "default", self.fixture.command, str(self.fixture.cwd))
        self.assertIsNone(created.recovery_authority_context)
        self.assertIsNone(self.store.get(created.id).recovery_authority_context_sha256)

    def test_optional_columns_migrate_legacy_schema_without_backfilling_context_or_recipe(self):
        path = self.fixture.root / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        self.store.conn.backup(connection)
        connection.execute("ALTER TABLE sessions DROP COLUMN recovery_authority_context")
        connection.execute("ALTER TABLE sessions DROP COLUMN recovery_authority_context_sha256")
        connection.row_factory = sqlite3.Row
        before = dict(connection.execute("SELECT * FROM sessions WHERE id='fixture'").fetchone())
        old_reader = object.__new__(SessionStore)
        old_record = old_reader._row_to_record(connection.execute("SELECT * FROM sessions WHERE id='fixture'").fetchone())
        self.assertIsNone(old_record.recovery_authority_context)
        connection.commit()
        connection.close()
        migrated = SessionStore(path)
        self.addCleanup(migrated.close)
        after = dict(migrated.conn.execute("SELECT * FROM sessions WHERE id='fixture'").fetchone())
        self.assertIsNone(after.pop("recovery_authority_context"))
        self.assertIsNone(after.pop("recovery_authority_context_sha256"))
        self.assertEqual(after, before)

    def test_gtk_periodic_batch_persists_context_even_when_legacy_tuple_is_unchanged(self):
        from termdeck.gtk_app import TermDeckWindow
        record = self.store.get("fixture")
        observation = self.observation(record)
        class Backend:
            def recovery_observations(self, records):
                return context.RecoveryObservationBatch({record.id: (record.recovery_command, record.recovery_cwd)},
                                                        {record.id: observation})
        tab = SimpleNamespace(record=record)
        side = SimpleNamespace(refresh=lambda: None)
        window = SimpleNamespace(tabs={record.id: tab}, app_context=SimpleNamespace(store=self.store, backend=Backend()),
            side=side, master_window=None, refresh_tab_label=lambda value: setattr(tab, "record", value))
        self.assertEqual(TermDeckWindow.refresh_tab_directories(window), 1)
        after = self.store.get("fixture")
        self.assertIsNotNone(after.recovery_authority_context)
        self.assertEqual(replace(after, recovery_authority_context=None, recovery_authority_context_sha256=None), record)

    def test_automatic_backend_batch_enrolls_at_observed_boundary_without_changing_tuple_api(self):
        backend = TmuxBackend()
        record = self.store.get("fixture")
        with mock.patch.object(resources, "_PROC", str(self.fixture.proc)), \
                mock.patch.object(backend, "pane_pids", return_value={record.id: 101}):
            legacy = backend.recovery_metadata([record])
            batch = backend.recovery_observations([record])
            self.assertEqual(batch.metadata, legacy)
            self.assertEqual(legacy, {record.id: (self.fixture.command, str(self.fixture.cwd))})
            self.assertEqual(self.store.update_recovery_metadata(
                [(record.id, *batch.metadata[record.id])], authority_observations=batch.authority), 1)
        self.assertTrue(context.context_matches_record(self.store.get("fixture")))

    def test_malformed_observation_does_not_drop_other_persistence_updates(self):
        other = replace(self.fixture.record, id="second", tmux_name="second")
        self.store.upsert(other)
        backend = TmuxBackend()
        first, second = self.store.get("fixture"), self.store.get("second")
        self.fixture.rollout.write_text("[]\n")
        with mock.patch.object(resources, "_PROC", str(self.fixture.proc)), \
                mock.patch.object(backend, "pane_pids", return_value={first.id: 101, second.id: 102}), \
                mock.patch.object(resources, "_primary_codex_session_id", return_value=THREAD), \
                mock.patch.object(resources, "observe_recovery_metadata_with_context", side_effect=[
                    resources.observe_recovery_metadata_with_context(first, 101),
                    (True, "bash", str(self.fixture.cwd), None)]):
            batch = backend.recovery_observations([first, second])
        self.assertNotIn(first.id, batch.metadata)
        self.assertEqual(batch.metadata[second.id], ("bash", str(self.fixture.cwd)))
        self.assertNotIn(first.id, batch.authority)

    def test_shutdown_uses_new_batch_and_persists_context_when_command_unchanged(self):
        store = self.store
        observation = self.observation()
        class Backend:
            def recovery_observations(self, records, bounded=True):
                return context.RecoveryObservationBatch({"fixture": (records[0].recovery_command, records[0].recovery_cwd)},
                                                        {"fixture": observation})
        result = persist_live_sessions(store, Backend(), records=[store.get("fixture")])
        self.assertEqual(result.metadata_updates, 1)
        self.assertIsNotNone(store.get("fixture").recovery_authority_context)


class StreamingLauncherBindingTests(unittest.TestCase):
    """Finite sparse executable observations; no native executable is run."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exe = self.root / "owned-executable"
        context._digest_cache.clear()

    def tearDown(self):
        self.temporary.cleanup()
        context._digest_cache.clear()

    def sparse(self, size):
        with self.exe.open("wb") as stream:
            stream.truncate(size)
        self.exe.chmod(0o700)
        hasher = context.hashlib.sha256()
        zeros = bytes(1024 * 1024)
        remaining = size
        while remaining:
            amount = min(remaining, len(zeros))
            hasher.update(zeros[:amount]); remaining -= amount
        return hasher.hexdigest()

    def test_over_256_mib_uses_bounded_streaming_allocation(self):
        import tracemalloc
        expected = self.sparse(270 * 1024 * 1024 + 17)
        tracemalloc.start()
        try:
            observed = context._launcher_binding(self.exe)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(observed["sha256"], expected)
        self.assertLess(peak, 3 * 1024 * 1024)

    def test_over_512_mib_qualified_capture_preserves_producer_checks(self):
        fixture = QualifiedProducerFixture(self.root / "native")
        self.exe = fixture.exe
        expected = self.sparse(530 * 1024 * 1024 + 17)
        executable = {**context.path_binding(self.exe), "sha256": expected}
        fixture.native["bindings"]["launcher"] = context.path_binding(self.exe)
        fixture.native["launcher"]["native_executable"] = executable
        fixture.native["launcher"]["chain"] = [executable]
        qualification = json.loads(fixture.qualification.read_bytes())
        qualification["native_executables"] = [{"path": str(self.exe), "sha256": expected}]
        fixture.qualification.chmod(0o600)
        fixture.qualification.write_text(context.canonical(qualification) + "\n")
        fixture.qualification.chmod(0o400)
        fixture.observer = replace(fixture.observer, qualification_sha256=context.sha256(fixture.qualification.read_bytes()))
        fixture.native["launcher"]["qualification_sha256"] = fixture.observer.qualification_sha256
        fixture.write_response()
        encoded, digest = context.capture_codex_context(fixture.record, 101, fixture.command,
            str(fixture.cwd), proc=str(fixture.proc), native_observer=fixture.observer)
        value = context.parse_context(encoded, digest)
        self.assertEqual(value["native_producer"]["launcher"]["native_executable"]["sha256"], expected)
        self.assertEqual(value["native_producer"]["logical_identity"]["scope_home"], str(fixture.home))
        self.assertEqual(value["native_producer"]["logical_identity"]["selected_auth_home"], str(fixture.auth))

    def changed_read(self, change):
        self.sparse(1024 * 1024 + 17)
        original_read = os.read
        changed = False
        def read(descriptor, amount):
            nonlocal changed
            raw = original_read(descriptor, amount)
            if not changed:
                changed = True
                change()
            return raw
        with mock.patch.object(context.os, "read", side_effect=read):
            with self.assertRaises(context.RecoveryAuthorityError):
                context._launcher_binding(self.exe)

    def test_growth_is_rejected_at_original_size(self):
        def grow():
            with self.exe.open("ab") as stream:
                stream.write(b"grew after captured pre-size")
        self.changed_read(grow)

    def test_truncation_is_rejected(self):
        self.changed_read(lambda: self.exe.write_bytes(b""))

    def test_same_content_foreign_inode_replacement_is_rejected(self):
        def replace_inode():
            other = self.exe.with_name("replacement")
            other.write_bytes(bytes(1024 * 1024 + 17)); other.chmod(0o700)
            os.replace(other, self.exe)
        self.changed_read(replace_inode)

    def test_permissions_changed_during_stream_are_rejected(self):
        self.changed_read(lambda: self.exe.chmod(0o777))

    def test_initial_hardlink_alias_is_rejected(self):
        self.sparse(17)
        os.link(self.exe, self.exe.with_name("alias"))
        with self.assertRaises(context.RecoveryAuthorityError):
            context._launcher_binding(self.exe)

    def test_stream_deadline_rejects_without_caching(self):
        self.sparse(17)
        with mock.patch.object(context.time, "monotonic", side_effect=[0.0, 3.0]):
            with self.assertRaisesRegex(context.RecoveryAuthorityError, "deadline"):
                context._launcher_binding(self.exe)
        self.assertEqual(context._digest_cache, {})

    def test_exact_stable_identity_reuses_hash_without_another_read(self):
        expected = self.sparse(17)
        self.assertEqual(context._launcher_binding(self.exe)["sha256"], expected)
        with mock.patch.object(context.os, "read", side_effect=AssertionError("cache must not read")):
            self.assertEqual(context._launcher_binding(self.exe)["sha256"], expected)


if __name__ == "__main__":
    unittest.main()
