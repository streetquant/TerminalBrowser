"""Optional, nonsecret evidence retained alongside a v1 session recipe.

The digest protects bytes; it does not grant native launch authority. Kernel
observations and native producer evidence remain distinct. Neither a rollout
header nor an executable name establishes the original managed runtime.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import selectors
import shlex
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from .session_recipe import parse_session_recipe

SCHEMA = "termdeck.recovery-authority-context.v1"
MAX_CONTEXT = 65536
PATH_SELECTORS = (
    "CODEX_HOME", "CODEX_AUTH_HOME", "CODEX_SQLITE_HOME", "CODEX_RUNTIME_ROOT",
    "CODEX_BIN", "CODEX_OPENCODE_LAUNCHER", "CODEX_DIRECT_REAL",
)
BINDINGS = frozenset({
    "scope_home", "sqlite_home", "runtime_root", "workspace_root", "cwd",
    "history_root", "launcher", "rollout",
})
NATIVE_SCHEMA = "codex.native-launch-context.v1"
NATIVE_BINDINGS = BINDINGS | {"selected_auth_home", "catalog_database"}
NATIVE_OBSERVATION = {"pid", "uid", "boot_id", "kernel_start_ticks", "parent_pid", "mount_namespace"}
_digest_cache: dict[tuple, str] = {}
_digest_lock = threading.Lock()


class RecoveryAuthorityError(ValueError):
    """Retained evidence is malformed, stale or contradicted."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def record_cas(record) -> str:
    """Bind an observation to every field in its selected SessionRecord."""
    return sha256(canonical(asdict(record)))


def intent_digest(record) -> str:
    return sha256(canonical({"command": record.command, "cwd": record.cwd,
                             "auth_home": record.codex_home, "selected_account": record.selected_account}))


def thread_id(command: str | None) -> str | None:
    try:
        tokens = shlex.split(command or "")
        if tokens.count("resume") != 1:
            return None
        index = tokens.index("resume")
        value = tokens[index + 1]
        if str(uuid.UUID(value)) != value:
            return None
        return value
    except (IndexError, ValueError):
        return None


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and "\x00" not in value and len(value) <= 4096


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryAuthorityError("duplicate context key")
        result[key] = value
    return result


def _validate_binding(binding: object) -> None:
    if not isinstance(binding, dict) or not {"path", "uid", "filesystemId", "inode"} <= binding.keys():
        raise RecoveryAuthorityError("incomplete physical observation")
    if set(binding) - {"path", "uid", "filesystemId", "inode", "sha256", "header_sha256"}:
        raise RecoveryAuthorityError("unknown physical observation field")
    if not _path(binding["path"]) or any(type(binding[k]) is not int or binding[k] < 0 for k in ("uid", "filesystemId", "inode")):
        raise RecoveryAuthorityError("invalid physical observation")
    for key in ("sha256", "header_sha256"):
        if key in binding and not _digest(binding[key]):
            raise RecoveryAuthorityError("invalid physical digest")


def _validate_native(value: object) -> dict:
    """Validate a retained nonsecret producer envelope, not current admission."""
    keys = {"schema_version", "state", "code", "launch_authorized", "execution_proved", "producer",
            "logical_identity", "bindings", "catalog", "launcher", "observation", "owner", "missing"}
    if (not isinstance(value, dict) or set(value) != keys or value["schema_version"] != NATIVE_SCHEMA
            or value["state"] != "observed" or value["launch_authorized"] is not False
            or value["execution_proved"] is not True or value["missing"] != []
            or not isinstance(value["code"], str) or not value["code"] or len(value["code"]) > 128):
        raise RecoveryAuthorityError("incomplete observed native producer")
    producer = value["producer"]
    if (not isinstance(producer, dict) or set(producer) != {"kind", "path", "sha256", "module_path", "module_sha256", "api"}
            or producer["kind"] != "native.codex" or producer["api"] != "observe-owned-process"
            or not _path(producer["path"]) or not _path(producer["module_path"])
            or not _digest(producer["sha256"]) or not _digest(producer["module_sha256"])):
        raise RecoveryAuthorityError("unsupported native producer provenance")
    identity = value["logical_identity"]
    if not isinstance(identity, dict) or set(identity) != {"thread_id", "scope_key", "scope_home", "selected_auth_home",
            "runtime_root", "workspace_root", "cwd", "history_root", "sqlite_home", "scope_id"}:
        raise RecoveryAuthorityError("incomplete native logical identity")
    if (thread_id("codex resume " + str(identity["thread_id"])) != identity["thread_id"]
            or not _digest(identity["scope_id"]) or not isinstance(identity["scope_key"], str)
            or not identity["scope_key"] or len(identity["scope_key"]) > 256
            or any(not _path(identity[k]) for k in ("scope_home", "runtime_root", "workspace_root", "cwd", "history_root", "sqlite_home"))
            or (identity["selected_auth_home"] is not None and not _path(identity["selected_auth_home"]))):
        raise RecoveryAuthorityError("invalid native logical identity")
    bindings = value["bindings"]
    required = {"scope_home", "runtime_root", "workspace_root", "cwd", "history_root", "sqlite_home", "catalog_database", "rollout", "launcher"}
    if not isinstance(bindings, dict) or not required <= bindings.keys() or set(bindings) - NATIVE_BINDINGS:
        raise RecoveryAuthorityError("incomplete native physical bindings")
    for binding in bindings.values():
        _validate_binding(binding)
    if not _digest(bindings["rollout"].get("header_sha256")):
        raise RecoveryAuthorityError("missing native rollout metadata digest")
    for name in required - {"catalog_database", "rollout", "launcher"}:
        if bindings[name]["path"] != identity[name]:
            raise RecoveryAuthorityError("native logical and physical homes disagree")
    if identity["selected_auth_home"] is not None and ("selected_auth_home" not in bindings
            or bindings["selected_auth_home"]["path"] != identity["selected_auth_home"]):
        raise RecoveryAuthorityError("native selected auth binding is incomplete")
    catalog = value["catalog"]
    if (not isinstance(catalog, dict) or set(catalog) != {"database_resolution", "catalog_database", "native_metadata_sha256"}
            or not _path(catalog["catalog_database"]) or catalog["catalog_database"] != bindings["catalog_database"]["path"]
            or (catalog["native_metadata_sha256"] is not None and not _digest(catalog["native_metadata_sha256"]))):
        raise RecoveryAuthorityError("invalid native catalog evidence")
    resolution = catalog["database_resolution"]
    if (not isinstance(resolution, dict) or set(resolution) != {"effective_home", "resolution_source", "configuration_scope", "database_files", "query_mode"}
            or resolution["effective_home"] != identity["sqlite_home"]
            or not isinstance(resolution["database_files"], list) or len(resolution["database_files"]) > 32
            or any(not _path(p) for p in resolution["database_files"])
            or any(not isinstance(resolution[k], str) or len(resolution[k]) > 256 for k in ("resolution_source", "configuration_scope", "query_mode"))):
        raise RecoveryAuthorityError("invalid supported native database resolution")
    launcher = value["launcher"]
    if (not isinstance(launcher, dict) or set(launcher) != {"qualification_state", "qualification_sha256", "qualification_schema", "trust_anchor",
            "release_id", "native_version", "chain", "native_executable"}
            or launcher["qualification_state"] != "qualified"
            or launcher["qualification_schema"] != "codex.native-launch-qualification.v1"
            or launcher["trust_anchor"] != "consumer-approved-qualification-sha256"
            or not _digest(launcher["qualification_sha256"])
            or any(not isinstance(launcher[k], str) or not launcher[k] or len(launcher[k]) > 256 for k in ("release_id", "native_version"))
            or not isinstance(launcher["chain"], list) or not launcher["chain"] or len(launcher["chain"]) > 16):
        raise RecoveryAuthorityError("invalid native launcher qualification")
    for binding in [*launcher["chain"], launcher["native_executable"]]:
        _validate_binding(binding)
        if not _digest(binding.get("sha256")):
            raise RecoveryAuthorityError("missing qualified executable digest")
    if launcher["native_executable"]["path"] != bindings["launcher"]["path"]:
        raise RecoveryAuthorityError("qualified executable and observation disagree")
    def generation(observation):
        if (not isinstance(observation, dict) or set(observation) != NATIVE_OBSERVATION
                or any(type(observation[k]) is not int or observation[k] < 0 for k in ("pid", "uid", "kernel_start_ticks", "parent_pid"))
                or observation["pid"] <= 1 or observation["kernel_start_ticks"] <= 0
                or not isinstance(observation["boot_id"], str) or not observation["boot_id"] or len(observation["boot_id"]) > 64
                or not isinstance(observation["mount_namespace"], str) or len(observation["mount_namespace"]) > 256):
            raise RecoveryAuthorityError("invalid native owner generation")
    generation(value["observation"])
    owner = value["owner"]
    if (not isinstance(owner, dict) or set(owner) != {"generation", "lease", "claim_path", "claim_sha256", "enrollment"}
            or not _path(owner["claim_path"]) or not _digest(owner["claim_sha256"])):
        raise RecoveryAuthorityError("incomplete native owner evidence")
    generation(owner["generation"])
    _validate_binding(owner["lease"])
    enrollment = owner["enrollment"]
    if (not isinstance(enrollment, dict) or set(enrollment) != {"source", "thread_id", "binding", "sha256"}
            or enrollment["source"] not in {"committed-native-index", "owned-native-registration"}
            or enrollment["thread_id"] != identity["thread_id"] or not _digest(enrollment["sha256"])):
        raise RecoveryAuthorityError("invalid owned native enrollment")
    _validate_binding(enrollment["binding"])
    return value


def parse_context(encoded: str | None, digest: str | None) -> dict:
    if not isinstance(encoded, str) or len(encoded.encode()) > MAX_CONTEXT or not _digest(digest):
        raise RecoveryAuthorityError("context and digest must be bounded and stored together")
    if not hmac.compare_digest(sha256(encoded), digest):
        raise RecoveryAuthorityError("context digest mismatch")
    try:
        value = json.loads(encoded, object_pairs_hook=_object)
    except (TypeError, ValueError) as exc:
        raise RecoveryAuthorityError("invalid context JSON") from exc
    keys = {"schema_version", "session_id", "application", "thread_id", "command_sha256", "cwd",
            "recipe_sha256", "environment_sha256", "intent_sha256", "selected_auth_home", "observation", "selectors",
            "bindings", "scope_producer", "native_producer"}
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != SCHEMA or value["application"] != "codex":
        raise RecoveryAuthorityError("unsupported context schema")
    if canonical(value) != encoded or not isinstance(value["session_id"], str):
        raise RecoveryAuthorityError("noncanonical context")
    try:
        if str(uuid.UUID(value["thread_id"])) != value["thread_id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise RecoveryAuthorityError("invalid native thread identity") from exc
    if not _path(value["cwd"]) or any(not _digest(value[key]) for key in ("command_sha256", "recipe_sha256", "environment_sha256", "intent_sha256")):
        raise RecoveryAuthorityError("invalid recipe binding")
    if value["selected_auth_home"] is not None and not _path(value["selected_auth_home"]):
        raise RecoveryAuthorityError("invalid selected auth home")
    observation = value["observation"]
    if not isinstance(observation, dict) or set(observation) != {"boot_id", "pid", "start_ticks", "uid"}:
        raise RecoveryAuthorityError("incomplete process generation")
    if not isinstance(observation["boot_id"], str) or len(observation["boot_id"]) > 64 or any(type(observation[k]) is not int or observation[k] < 0 for k in ("pid", "start_ticks", "uid")):
        raise RecoveryAuthorityError("invalid process generation")
    if observation["pid"] <= 1 or observation["start_ticks"] <= 0:
        raise RecoveryAuthorityError("invalid process generation")
    selectors = value["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != set(PATH_SELECTORS) or any(v is not None and not _path(v) for v in selectors.values()):
        raise RecoveryAuthorityError("invalid nonsecret selectors")
    if not isinstance(value["bindings"], dict) or set(value["bindings"]) - BINDINGS:
        raise RecoveryAuthorityError("unknown native binding")
    for binding in value["bindings"].values():
        _validate_binding(binding)
    producer = value["scope_producer"]
    if producer is not None:
        if not isinstance(producer, dict) or set(producer) != {"path", "sha256", "schema_version", "scope_key", "scope_digest", "thread_id", "home", "cwd", "boot_id"}:
            raise RecoveryAuthorityError("invalid scope producer evidence")
        if not _path(producer["path"]) or not _digest(producer["sha256"]) or not _digest(producer["scope_digest"]) or not _path(producer["home"]) or not _path(producer["cwd"]) or producer["thread_id"] != value["thread_id"]:
            raise RecoveryAuthorityError("contradicted scope producer evidence")
        if producer["schema_version"] != "codex.direct-runtime-scope.v3" or not isinstance(producer["scope_key"], str) or not isinstance(producer["boot_id"], str):
            raise RecoveryAuthorityError("unsupported scope producer")
    if value["native_producer"] is not None:
        native = _validate_native(value["native_producer"])
        observed = native["observation"]
        if (native["logical_identity"]["thread_id"] != value["thread_id"] or native["logical_identity"]["cwd"] != value["cwd"]
                or any(observed[k] != observation[v] for k, v in (("pid", "pid"), ("uid", "uid"), ("boot_id", "boot_id"), ("kernel_start_ticks", "start_ticks")))):
            raise RecoveryAuthorityError("native producer contradicts observed recipe generation")
        for name, binding in native["bindings"].items():
            if name in BINDINGS:
                expected = native["launcher"]["native_executable"] if name == "launcher" else binding
                if value["bindings"].get(name) != expected:
                    raise RecoveryAuthorityError("retained native bindings contradict producer")
    return value


def encode_context(value: Mapping) -> tuple[str, str]:
    encoded = canonical(dict(value))
    digest = sha256(encoded)
    parse_context(encoded, digest)
    return encoded, digest


def bind_context(encoded: str, digest: str, record, recipe_json: str, recipe_digest: str) -> tuple[str, str]:
    value = parse_context(encoded, digest)
    recipe = parse_session_recipe(recipe_json, recipe_digest)
    if (value["session_id"] != record.id or value["thread_id"] != thread_id(recipe.command)
            or value["command_sha256"] != sha256(recipe.command) or value["cwd"] != recipe.cwd
            or value["environment_sha256"] != sha256(canonical(recipe.environment_dict()))
            or value["intent_sha256"] != intent_digest(record)
            or value["selected_auth_home"] != (record.codex_home or None)):
        raise RecoveryAuthorityError("context contradicts intended recipe or selected auth home")
    value["recipe_sha256"] = recipe_digest
    return encode_context(value)


def context_matches_record(record) -> bool:
    encoded, digest = getattr(record, "recovery_authority_context", None), getattr(record, "recovery_authority_context_sha256", None)
    if encoded is None and digest is None:
        return True
    try:
        rebound = bind_context(encoded, digest, record, record.recovery_recipe, record.recovery_recipe_sha256)
        return rebound == (encoded, digest)
    except (RecoveryAuthorityError, ValueError, TypeError):
        return False


def retain_stronger_context(old_pair: tuple[str, str], new_pair: tuple[str, str]) -> tuple[str, str]:
    """Keep original evidence when an unchanged owner yields a partial probe.

    This retains history only. It cannot establish a current native lease or
    grant a cold launch. Contradictory homes/generations need owning continuity.
    """
    old, new = parse_context(*old_pair), parse_context(*new_pair)
    if old["thread_id"] != new["thread_id"]:
        return new_pair
    old_native, new_native = old["native_producer"], new["native_producer"]
    old_home, new_home = old["bindings"].get("scope_home"), new["bindings"].get("scope_home")
    # A prior unqualified CODEX_HOME observation can identify the auth home.
    # Only a qualified producer may distinguish it from the original scope.
    correcting_auth_claim = (old_native is None and new_native is not None and old_home is not None
                            and old_home["path"] == new_native["logical_identity"]["selected_auth_home"])
    weak_auth_claim = (old_native is not None and new_native is None and new_home is not None
                       and new_home["path"] == old_native["logical_identity"]["selected_auth_home"])
    if old_home and new_home and not correcting_auth_claim and not weak_auth_claim:
        if (old_home["path"], old_home["uid"], old_home["inode"]) != (new_home["path"], new_home["uid"], new_home["inode"]):
            raise RecoveryAuthorityError("observed native home changed; supported continuity required")
    if old_native is not None and new_native is None:
        if old["observation"] != new["observation"]:
            raise RecoveryAuthorityError("new native generation requires qualified observation")
        for name in ("command_sha256", "cwd", "environment_sha256", "intent_sha256", "selected_auth_home"):
            if old[name] != new[name]:
                raise RecoveryAuthorityError("partial observation contradicts retained native context")
        for name in ("cwd", "launcher"):
            if new["bindings"].get(name) != old["bindings"].get(name):
                raise RecoveryAuthorityError("partial runtime change requires qualified observation")
        for name, claim in new["selectors"].items():
            if claim is not None and old["selectors"][name] is not None and claim != old["selectors"][name]:
                raise RecoveryAuthorityError("partial selector change requires qualified observation")
        return old_pair
    if (new_native is None and old["observation"] == new["observation"]
            and all(old[k] == new[k] for k in ("command_sha256", "cwd", "environment_sha256", "intent_sha256", "selected_auth_home"))
            and all(old["bindings"].get(k) == v for k, v in new["bindings"].items())
            and all(v is None or v == old["selectors"][k] for k, v in new["selectors"].items())
            and old["scope_producer"] is not None and new["scope_producer"] is None):
        return old_pair
    return new_pair


def physical_drift(saved: Mapping, current: Mapping) -> str:
    """Classify observations without granting remount or migration admission."""
    if saved["path"] != current["path"] or saved["uid"] != current["uid"]:
        return "FOREIGN_BINDING"
    if saved["inode"] != current["inode"]:
        return "PHYSICAL_REPLACEMENT_REQUIRES_NATIVE_CONTINUITY"
    if saved["filesystemId"] != current["filesystemId"]:
        return "REMOUNT_OBSERVATION_REQUIRES_NATIVE_CONTINUITY"
    if saved.get("sha256") != current.get("sha256"):
        return "RUNTIME_UPDATE_REQUIRES_QUALIFICATION"
    return "UNCHANGED"


@dataclass(frozen=True)
class AuthorityObservation:
    expected_record_sha256: str
    encoded: str | None
    sha256: str | None
    is_current: Callable[[], bool] | None = None


@dataclass(frozen=True)
class RecoveryObservationBatch:
    metadata: Mapping[str, tuple[str | None, str | None]]
    authority: Mapping[str, AuthorityObservation]


@dataclass(frozen=True)
class NativeContextObserver:
    """A consumer-approved read-only native observer, never selected by a pane.

    Activation supplies this configuration through the owning GTK/backend
    environment. Child claim selectors cannot choose a helper or qualification.
    Source and qualification pins are checked before and after each invocation.
    """
    observer_path: str
    observer_sha256: str
    producer_path: str
    producer_sha256: str
    module_path: str
    module_sha256: str
    qualification_path: str
    qualification_sha256: str
    timeout: float = 1.0

    def __post_init__(self):
        for name in ("observer", "producer", "module", "qualification"):
            if not _path(getattr(self, name + "_path")) or not _digest(getattr(self, name + "_sha256")):
                raise RecoveryAuthorityError("invalid consumer-approved native observer pin")
        if not 0 < self.timeout <= 2:
            raise RecoveryAuthorityError("native observer timeout exceeds bound")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        environment = os.environ if environment is None else environment
        path = environment.get("TERMDECK_NATIVE_CONTEXT_CONFIG")
        digest = environment.get("TERMDECK_NATIVE_CONTEXT_CONFIG_SHA256")
        if path is None and digest is None:
            return None
        if not _path(path) or not _digest(digest):
            raise RecoveryAuthorityError("native observer activation pin is incomplete")
        data, _ = _read_bound(Path(path), MAX_CONTEXT)
        if not hmac.compare_digest(sha256(data), digest):
            raise RecoveryAuthorityError("native observer activation digest mismatch")
        value = json.loads(data, object_pairs_hook=_object)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "observer", "producer", "module", "qualification"}
                or value["schema_version"] != "termdeck.native-context-observer.v1"):
            raise RecoveryAuthorityError("unsupported native observer activation")
        fields = {}
        for name in ("observer", "producer", "module", "qualification"):
            pin = value[name]
            if not isinstance(pin, dict) or set(pin) != {"path", "sha256"}:
                raise RecoveryAuthorityError("invalid native observer activation source")
            fields[name + "_path"], fields[name + "_sha256"] = pin["path"], pin["sha256"]
        return cls(**fields)

    def _check_pins(self):
        for name in ("observer", "producer", "module", "qualification"):
            data, _ = _read_bound(Path(getattr(self, name + "_path")), 8 * 1024 * 1024)
            if not hmac.compare_digest(sha256(data), getattr(self, name + "_sha256")):
                raise RecoveryAuthorityError("consumer-approved native source changed")
            if name == "qualification":
                qualification = json.loads(data, object_pairs_hook=_object)
                files = qualification.get("producer_files") if isinstance(qualification, dict) else None
                if (not isinstance(qualification, dict)
                        or qualification.get("schema_version") != "codex.native-launch-qualification.v1"
                        or not isinstance(files, list) or not files or len(files) > 64
                        or any(not isinstance(pin, dict) or set(pin) != {"path", "sha256"}
                               or not _path(pin["path"]) or not _digest(pin["sha256"]) for pin in files)):
                    raise RecoveryAuthorityError("consumer qualification does not cover native sources")
                covered = {(pin["path"], pin["sha256"]) for pin in files}
                required = {(getattr(self, source + "_path"), getattr(self, source + "_sha256"))
                            for source in ("observer", "producer", "module")}
                if not required <= covered:
                    raise RecoveryAuthorityError("consumer qualification does not cover native sources")

    def _invoke(self, request: dict) -> tuple[int, bytes]:
        request_bytes = (canonical(request) + "\n").encode()
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-B", self.observer_path, "observe"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, close_fds=True,
        )
        output, error = bytearray(), bytearray()
        try:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            deadline = time.monotonic() + self.timeout
            with selectors.DefaultSelector() as reader:
                os.set_blocking(process.stdin.fileno(), False)
                reader.register(process.stdin, selectors.EVENT_WRITE, None)
                written = 0
                for pipe, target, maximum in ((process.stdout, output, MAX_CONTEXT), (process.stderr, error, 8192)):
                    os.set_blocking(pipe.fileno(), False)
                    reader.register(pipe, selectors.EVENT_READ, (target, maximum))
                while reader.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RecoveryAuthorityError("native observer exceeded deadline")
                    for key, _event in reader.select(remaining):
                        if key.data is None:
                            written += os.write(key.fd, request_bytes[written:])
                            if written == len(request_bytes):
                                reader.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        target, maximum = key.data
                        data = os.read(key.fd, 8192)
                        if not data:
                            reader.unregister(key.fileobj)
                        else:
                            target.extend(data)
                            if len(target) > maximum:
                                raise RecoveryAuthorityError("native observer output exceeded bound")
            try:
                status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise RecoveryAuthorityError("native observer exceeded deadline") from exc
            return status, bytes(output)
        finally:
            try:
                if process.poll() is None:
                    process.kill()  # Only this freshly spawned, owned observer.
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired as exc:
                        raise RecoveryAuthorityError("owned observer cleanup exceeded deadline") from exc
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

    def observe(self, generation: Mapping, claim_path: str | None, claim_digest: str | None,
                native_thread: str, cwd: str) -> dict | None:
        if claim_path is None and claim_digest is None:
            return None
        if not _path(claim_path) or not _digest(claim_digest):
            raise RecoveryAuthorityError("native claim selectors are incomplete")
        request = {"schema_version": "codex.native-launch-context.observe.v1", "claim_path": claim_path,
                   "claim_context_sha256": claim_digest, "process": {
                       "pid": generation["pid"], "uid": generation["uid"], "boot_id": generation["boot_id"],
                       "kernel_start_ticks": generation["start_ticks"]},
                   "qualification": {"path": self.qualification_path, "sha256": self.qualification_sha256},
                   "expected_thread_id": native_thread}
        self._check_pins()
        status, data = self._invoke(request)
        self._check_pins()
        value = json.loads(data, object_pairs_hook=_object)
        if isinstance(value, dict) and value.get("state") == "unavailable":
            if (set(value) != {"schema_version", "state", "code", "launch_authorized", "execution_proved", "missing"}
                    or value["schema_version"] != NATIVE_SCHEMA or value["launch_authorized"] is not False
                    or value["execution_proved"] is not False):
                raise RecoveryAuthorityError("invalid unavailable native observation")
            return None
        if status != 0:
            raise RecoveryAuthorityError("native observer failed")
        native = _validate_native(value)
        producer, observed = native["producer"], native["observation"]
        if ((producer["path"], producer["sha256"], producer["module_path"], producer["module_sha256"])
                != (self.observer_path, self.observer_sha256, self.module_path, self.module_sha256)
                or native["launcher"]["qualification_sha256"] != self.qualification_sha256
                or native["owner"]["claim_path"] != claim_path
                or native["logical_identity"]["thread_id"] != native_thread or native["logical_identity"]["cwd"] != cwd
                or any(observed[k] != generation[v] for k, v in (("pid", "pid"), ("uid", "uid"), ("boot_id", "boot_id"), ("kernel_start_ticks", "start_ticks")))):
            raise RecoveryAuthorityError("native observer contradicts approved provenance or owner")
        return native


def _identity(info) -> tuple:
    return tuple(getattr(info, k) for k in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _read_bound(path: Path, maximum: int, *, header: bool = False) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.geteuid()} or before.st_nlink != 1 or before.st_mode & 0o022:
            raise RecoveryAuthorityError("unsafe evidence file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.readline(maximum + 1) if header else handle.read(maximum + 1)
        if len(data) > maximum or _identity(before) != _identity(os.fstat(descriptor)) or _identity(before) != _identity(path.lstat()):
            raise RecoveryAuthorityError("evidence changed during observation")
        return data, before
    finally:
        os.close(descriptor)


def path_binding(path: str | Path, *, owner: bool = False) -> dict:
    resolved = Path(path).resolve(strict=True)
    info = resolved.lstat()
    if (not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode)) or (owner and (info.st_uid != os.geteuid() or info.st_mode & 0o022)):
        raise RecoveryAuthorityError("unsafe physical binding")
    return {"path": str(resolved), "uid": info.st_uid, "filesystemId": info.st_dev, "inode": info.st_ino}


def process_generation(pid: int, *, proc: str = "/proc") -> dict:
    if type(pid) is not int or pid <= 1:
        raise RecoveryAuthorityError("invalid process generation")
    root = Path(proc) / str(pid)
    info = root.stat()
    if info.st_uid != os.geteuid():
        raise RecoveryAuthorityError("foreign process owner")
    try:
        text = (root / "stat").read_text()
        prefix, remainder = text.rsplit(")", 1)
        parts = remainder.split()
        if int(prefix.split("(", 1)[0].strip()) != pid or len(parts) < 20:
            raise ValueError
        start = int(parts[19])
        boot = (Path(proc) / "sys/kernel/random/boot_id").read_text().strip()
        if start <= 0 or not boot or len(boot) > 64:
            raise ValueError
    except (IndexError, ValueError) as exc:
        raise RecoveryAuthorityError("invalid process generation") from exc
    return {"pid": pid, "start_ticks": start, "uid": info.st_uid, "boot_id": boot}


def _launcher_binding(path: Path) -> dict:
    path = path.resolve(strict=True)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022
            or before.st_uid not in {0, os.geteuid()} or before.st_nlink != 1):
        raise RecoveryAuthorityError("unsafe observed executable")
    key = (str(path), *_identity(before))
    with _digest_lock:
        digest = _digest_cache.get(key)
    if digest is None:
        # The stable physical pre-size is the finite byte bound. Native releases
        # can grow without changing their trust role; JSON limits do not apply
        # to a qualified executable. Never allocate the whole binary or follow
        # a growing file beyond the captured size.
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise RecoveryAuthorityError("executable replaced during observation")
            deadline = time.monotonic() + 2.0
            remaining = before.st_size
            hasher = hashlib.sha256()
            while remaining:
                if time.monotonic() >= deadline:
                    raise RecoveryAuthorityError("executable observation exceeded deadline")
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise RecoveryAuthorityError("executable truncated during observation")
                hasher.update(chunk)
                remaining -= len(chunk)
            if (os.read(descriptor, 1) or _identity(os.fstat(descriptor)) != _identity(before)
                    or _identity(path.lstat()) != _identity(before)):
                raise RecoveryAuthorityError("executable changed during observation")
            digest = hasher.hexdigest()
        finally:
            os.close(descriptor)
        with _digest_lock:
            if len(_digest_cache) >= 16:
                _digest_cache.pop(next(iter(_digest_cache)))
            _digest_cache[key] = digest
    if _identity(path.lstat()) != _identity(before):
        raise RecoveryAuthorityError("executable changed during observation")
    return {**path_binding(path), "sha256": digest}


def capture_codex_context(record, pid: int, command: str, cwd: str, *, proc: str = "/proc",
                          native_observer: NativeContextObserver | None = None) -> tuple[str, str] | None:
    """Capture one observed native boundary, without invoking native tooling.

    Only one open primary rollout header and one explicit scope-index receipt
    may be read. Missing defaults/qualification stay missing. No config, auth,
    transcript body, catalog, complete environment or prompt is persisted.
    """
    native_thread = thread_id(command)
    if native_thread is None or not _path(cwd):
        return None
    before = process_generation(pid, proc=proc)
    root = Path(proc) / str(pid)
    with (root / "environ").open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RecoveryAuthorityError("process environment exceeds observation bound")
    environment = {}
    claim_path = claim_digest = None
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        name = os.fsdecode(key)
        if separator and name in PATH_SELECTORS:
            decoded = os.fsdecode(value)
            if name in environment:
                raise RecoveryAuthorityError("duplicate native path selector")
            environment[name] = decoded if _path(decoded) else None
        elif separator and name == "CODEX_NATIVE_LAUNCH_CLAIM_PATH":
            if claim_path is not None:
                raise RecoveryAuthorityError("duplicate native claim selector")
            claim_path = os.fsdecode(value)
        elif separator and name == "CODEX_NATIVE_LAUNCH_CONTEXT_SHA256":
            if claim_digest is not None:
                raise RecoveryAuthorityError("duplicate native claim selector")
            claim_digest = os.fsdecode(value)
    selectors = {name: environment.get(name) for name in PATH_SELECTORS}
    bindings = {"cwd": path_binding(cwd), "launcher": _launcher_binding(root / "exe")}
    if Path(os.readlink(root / "cwd")).resolve(strict=True) != Path(cwd).resolve(strict=True):
        raise RecoveryAuthorityError("native cwd changed during observation")
    native = (native_observer.observe(before, claim_path, claim_digest, native_thread, cwd)
              if native_observer is not None else None)
    if native is not None:
        executable = native["launcher"]["native_executable"]
        if executable != bindings["launcher"]:
            raise RecoveryAuthorityError("qualified native executable changed during observation")
        selected = native["logical_identity"]["selected_auth_home"]
        if record.codex_home is not None and (selected is None
                or Path(selected).resolve(strict=True) != Path(record.codex_home).resolve(strict=True)):
            raise RecoveryAuthorityError("qualified native selected home contradicts saved intent")
        # A selected auth/login home is not the original scope/history home.
        # The qualified native boundary supplies both without environment inference.
        bindings = {name: dict(binding) for name, binding in native["bindings"].items() if name in BINDINGS}
        bindings["launcher"] = executable
        producer = None
        if before != process_generation(pid, proc=proc):
            raise RecoveryAuthorityError("native process generation changed during observation")
        recipe = parse_session_recipe(record.recovery_recipe, record.recovery_recipe_sha256)
        return encode_context({"schema_version": SCHEMA, "session_id": record.id, "application": "codex",
            "thread_id": native_thread, "command_sha256": sha256(command), "cwd": cwd,
            "recipe_sha256": record.recovery_recipe_sha256, "environment_sha256": sha256(canonical(recipe.environment_dict())),
            "selected_auth_home": record.codex_home or None, "intent_sha256": intent_digest(record),
            "observation": before, "selectors": selectors, "bindings": bindings, "scope_producer": producer,
            "native_producer": native})
    home = selectors["CODEX_HOME"]
    if home is not None:
        bindings["scope_home"] = path_binding(home, owner=True)
        try:
            bindings["history_root"] = path_binding(Path(home) / "sessions", owner=True)
        except (OSError, RecoveryAuthorityError):
            pass
    runtime = selectors["CODEX_RUNTIME_ROOT"]
    if runtime is not None:
        bindings["runtime_root"] = path_binding(runtime, owner=True)
    # An environment override is retained as a selector claim, not as the
    # effective SQLite home: home configuration has higher native precedence.
    rollout = None
    fd_names = list((root / "fd").iterdir())
    if len(fd_names) > 4096:
        raise RecoveryAuthorityError("descriptor observation exceeds bound")
    for fd in fd_names:
        try:
            target = Path(os.readlink(fd))
            if target.suffix != ".jsonl" or not target.name.endswith(native_thread + ".jsonl"):
                continue
            data, info = _read_bound(target, 65536, header=True)
            opened = fd.stat()
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise RecoveryAuthorityError("rollout descriptor was replaced")
            header = json.loads(data, object_pairs_hook=_object)
            if not isinstance(header, dict):
                raise RecoveryAuthorityError("native rollout metadata is not an object")
            payload = header.get("payload")
            if (header.get("type") != "session_meta" or not isinstance(payload, dict)
                    or payload.get("id") != native_thread or payload.get("cwd") != cwd or payload.get("source") != "cli"):
                continue
            if "history_root" not in bindings or not target.resolve().is_relative_to(Path(bindings["history_root"]["path"])):
                raise RecoveryAuthorityError("rollout does not belong to observed history home")
            candidate = {**path_binding(target, owner=True), "header_sha256": sha256(canonical(header))}
            if rollout is not None and rollout != candidate:
                raise RecoveryAuthorityError("ambiguous open primary rollout")
            rollout = candidate
        except (FileNotFoundError, PermissionError):
            continue
    if rollout is not None:
        bindings["rollout"] = rollout
    producer = None
    if runtime is not None and home is not None:
        path = Path(runtime) / "thread-scope-index" / ("thread-" + sha256(native_thread) + ".json")
        try:
            data, _ = _read_bound(path, 65536)
            value = json.loads(data, object_pairs_hook=_object)
            if not isinstance(value, dict):
                raise RecoveryAuthorityError("scope producer metadata is not an object")
            if (value.get("schemaVersion") != "codex.direct-runtime-scope.v3" or value.get("threadId") != native_thread
                    or value.get("registeredThreadId") != native_thread or value.get("registrationState") != "registered"
                    or value.get("codexHome") != home or value.get("cwd") != cwd):
                raise RecoveryAuthorityError("scope producer contradicts observed native boundary")
            if not {"scopeKey", "scopeDigest", "ownerBootId", "workspaceRoot"} <= value.keys():
                raise RecoveryAuthorityError("scope producer metadata is incomplete")
            producer = {"path": str(path), "sha256": sha256(data), "schema_version": value["schemaVersion"],
                        "scope_key": value["scopeKey"], "scope_digest": value["scopeDigest"], "thread_id": native_thread,
                        "home": home, "cwd": cwd, "boot_id": value["ownerBootId"]}
            bindings["workspace_root"] = path_binding(value["workspaceRoot"])
        except FileNotFoundError:
            pass
    if before != process_generation(pid, proc=proc):
        raise RecoveryAuthorityError("native process generation changed during observation")
    recipe = parse_session_recipe(record.recovery_recipe, record.recovery_recipe_sha256)
    value = {"schema_version": SCHEMA, "session_id": record.id, "application": "codex", "thread_id": native_thread,
             "command_sha256": sha256(command), "cwd": cwd, "recipe_sha256": record.recovery_recipe_sha256,
             "environment_sha256": sha256(canonical(recipe.environment_dict())), "selected_auth_home": record.codex_home or None,
             "intent_sha256": intent_digest(record),
             "observation": before, "selectors": selectors, "bindings": bindings, "scope_producer": producer,
             "native_producer": None}
    return encode_context(value)


def observation_is_current(record, encoded: str, digest: str, *, proc: str = "/proc",
                           native_observer: NativeContextObserver | None = None) -> bool:
    """Check a captured owner again at the store's write boundary."""
    try:
        value = parse_context(encoded, digest)
        return capture_codex_context(record, value["observation"]["pid"],
                                     record.recovery_command or parse_session_recipe(record.recovery_recipe, record.recovery_recipe_sha256).command,
                                     value["cwd"], proc=proc, native_observer=native_observer) == (encoded, digest)
    except (OSError, ValueError, TypeError):
        return False
