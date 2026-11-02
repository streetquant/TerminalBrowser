from __future__ import annotations

"""Tool-agnostic durable launch recipes for managed terminal sessions.

A recipe is deliberately smaller than a process checkpoint.  When tmux and its
pane survive, Termdeck reattaches to the exact process.  When the process,
tmux server, or host is lost, the recipe deterministically recreates the saved
command, working directory, and explicit environment overlay.  No agent name,
conversation format, or external executable is part of this contract.
"""

from dataclasses import dataclass
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Mapping

SESSION_RECIPE_SCHEMA = "termdeck.session-recipe.v1"
DEFAULT_RESTART_POLICY = "on-failure"
RESTART_POLICIES = frozenset({"always", "on-failure", "manual", "never"})
AUTO_RESTART_POLICIES = frozenset({"always", "on-failure"})

_MAX_COMMAND_BYTES = 256 * 1024
_MAX_CWD_BYTES = 16 * 1024
_MAX_ENVIRONMENT_ENTRIES = 256
_MAX_ENVIRONMENT_BYTES = 1024 * 1024
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,255}$")
_ORIGIN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# These values describe the *current* process/tmux attachment and are never
# portable launch inputs.  Explicit recipe creation fails closed rather than
# persisting a stale nested-terminal identity.
_NON_PORTABLE_ENVIRONMENT = frozenset(
    {"TMUX", "TMUX_PANE", "PWD", "OLDPWD", "SHLVL", "_"}
)


class SessionRecipeError(ValueError):
    """Raised when a durable session recipe is malformed or corrupted."""


@dataclass(frozen=True)
class SessionRecipe:
    command: str
    cwd: str
    environment: tuple[tuple[str, str], ...] = ()
    restart_policy: str = DEFAULT_RESTART_POLICY
    origin: str = "launch"
    schema: str = SESSION_RECIPE_SCHEMA

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "command": self.command,
            "cwd": self.cwd,
            "environment": {key: value for key, value in self.environment},
            "restart_policy": self.restart_policy,
            "origin": self.origin,
        }

    def environment_dict(self) -> dict[str, str]:
        return {key: value for key, value in self.environment}


@dataclass(frozen=True)
class ResolvedWorkingDirectory:
    path: Path
    degraded: bool
    original: str

    @property
    def environment(self) -> dict[str, str]:
        if not self.degraded:
            return {}
        return {
            "TERMDECK_RECOVERY_DEGRADED": "missing-working-directory",
            "TERMDECK_RECOVERY_ORIGINAL_CWD": self.original,
        }


def _clean_text(value: object, *, name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise SessionRecipeError(f"session recipe {name} must be text")
    clean = value.strip()
    if not clean:
        raise SessionRecipeError(f"session recipe {name} must not be empty")
    if "\x00" in clean:
        raise SessionRecipeError(f"session recipe {name} contains NUL")
    if len(clean.encode("utf-8")) > maximum_bytes:
        raise SessionRecipeError(f"session recipe {name} is too large")
    return clean


def _normalise_cwd(value: object, *, base_directory: str | Path | None = None) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    raw = _clean_text(value, name="cwd", maximum_bytes=_MAX_CWD_BYTES)
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        base = Path(base_directory).expanduser() if base_directory is not None else Path.cwd()
        requested = base / requested
    try:
        normalised = Path(os.path.abspath(os.fspath(requested)))
    except (OSError, RuntimeError, TypeError) as exc:
        raise SessionRecipeError("session recipe cwd cannot be normalised") from exc
    text = str(normalised)
    if not Path(text).is_absolute():
        raise SessionRecipeError("session recipe cwd must be absolute")
    if len(os.fsencode(text)) > _MAX_CWD_BYTES:
        raise SessionRecipeError("session recipe cwd is too large")
    return text


def _normalise_environment(
    environment: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if environment is None:
        return ()
    if not isinstance(environment, Mapping):
        raise SessionRecipeError("session recipe environment must be a mapping")
    if len(environment) > _MAX_ENVIRONMENT_ENTRIES:
        raise SessionRecipeError("session recipe environment has too many entries")
    rows: list[tuple[str, str]] = []
    total = 0
    for raw_key, raw_value in environment.items():
        if not isinstance(raw_key, str) or _ENVIRONMENT_KEY.fullmatch(raw_key) is None:
            raise SessionRecipeError("session recipe environment contains an invalid key")
        if raw_key in _NON_PORTABLE_ENVIRONMENT:
            raise SessionRecipeError(
                f"session recipe environment key {raw_key!r} is process-local"
            )
        if not isinstance(raw_value, str) or "\x00" in raw_value:
            raise SessionRecipeError(
                f"session recipe environment value for {raw_key!r} is invalid"
            )
        total += len(raw_key.encode("utf-8")) + len(raw_value.encode("utf-8"))
        if total > _MAX_ENVIRONMENT_BYTES:
            raise SessionRecipeError("session recipe environment is too large")
        rows.append((raw_key, raw_value))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def build_session_recipe(
    command: object,
    cwd: object,
    *,
    environment: Mapping[str, str] | None = None,
    restart_policy: str = DEFAULT_RESTART_POLICY,
    origin: str = "launch",
    base_directory: str | Path | None = None,
) -> SessionRecipe:
    clean_command = _clean_text(
        command,
        name="command",
        maximum_bytes=_MAX_COMMAND_BYTES,
    )
    clean_cwd = _normalise_cwd(cwd, base_directory=base_directory)
    clean_policy = str(restart_policy or DEFAULT_RESTART_POLICY).strip().casefold()
    if clean_policy not in RESTART_POLICIES:
        raise SessionRecipeError(
            f"session recipe restart_policy must be one of {sorted(RESTART_POLICIES)}"
        )
    clean_origin = str(origin or "launch").strip().casefold()
    if _ORIGIN.fullmatch(clean_origin) is None:
        raise SessionRecipeError("session recipe origin is invalid")
    return SessionRecipe(
        command=clean_command,
        cwd=clean_cwd,
        environment=_normalise_environment(environment),
        restart_policy=clean_policy,
        origin=clean_origin,
    )


def canonical_recipe_json(recipe: SessionRecipe) -> str:
    return json.dumps(
        recipe.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def recipe_digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def encode_session_recipe(recipe: SessionRecipe) -> tuple[str, str]:
    encoded = canonical_recipe_json(recipe)
    return encoded, recipe_digest(encoded)


def parse_session_recipe(encoded: object, digest: object) -> SessionRecipe:
    if not isinstance(encoded, str) or not encoded:
        raise SessionRecipeError("session recipe JSON is missing")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise SessionRecipeError("session recipe digest is missing or invalid")
    if recipe_digest(encoded) != digest:
        raise SessionRecipeError("session recipe digest mismatch")
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionRecipeError("session recipe JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "command",
        "cwd",
        "environment",
        "restart_policy",
        "origin",
    }:
        raise SessionRecipeError("session recipe fields are invalid")
    if payload.get("schema") != SESSION_RECIPE_SCHEMA:
        raise SessionRecipeError("session recipe schema is unsupported")
    environment = payload.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise SessionRecipeError("session recipe environment is invalid")
    recipe = build_session_recipe(
        payload.get("command"),
        payload.get("cwd"),
        environment=environment,
        restart_policy=str(payload.get("restart_policy") or ""),
        origin=str(payload.get("origin") or ""),
    )
    if canonical_recipe_json(recipe) != encoded:
        raise SessionRecipeError("session recipe JSON is not canonical")
    return recipe


def receipt_reenrollment(record: object) -> tuple[str, str]:
    """Validate an explicit reenrollment without accepting the old receipt.

    Only the digest may be replaced. The complete old canonical JSON must
    independently agree with the currently recorded command, cwd and policy;
    its explicit environment and origin remain byte-for-byte unchanged.
    Normal startup continues to reject an invalid stored digest.
    """
    encoded = getattr(record, "recovery_recipe", None)
    if not isinstance(encoded, str) or not encoded:
        raise SessionRecipeError("reenrollment requires an existing recipe payload")
    digest = recipe_digest(encoded)
    recipe = parse_session_recipe(encoded, digest)
    try:
        parse_session_recipe(encoded, getattr(record, "recovery_recipe_sha256", None))
    except SessionRecipeError:
        pass
    else:
        raise SessionRecipeError("the recorded receipt is already valid")
    command = str(getattr(record, "recovery_command", None) or
                  getattr(record, "command", "") or "").strip()
    cwd = str(getattr(record, "recovery_cwd", None) or
              getattr(record, "cwd", "") or "").strip()
    policy = str(getattr(record, "restart_policy", DEFAULT_RESTART_POLICY)
                 or DEFAULT_RESTART_POLICY).strip().casefold()
    if recipe.command != command or recipe.cwd != cwd or recipe.restart_policy != policy:
        raise SessionRecipeError("recipe payload disagrees with recorded launch inputs")
    return encoded, digest


def recovery_command_specificity(command: str | None) -> int:
    """Rank opaque recovery commands without knowing the owning application.

    A command carrying an explicit ``resume ID`` or ``--resume ID`` target is
    more specific than a bare launcher.  This generic rule preserves stronger
    application-native recovery metadata when a weaker observation arrives,
    without naming or importing any particular agent.
    """

    if not command or not command.strip():
        return 0
    try:
        parts = shlex.split(command)
    except ValueError:
        return 1
    for index, token in enumerate(parts):
        if token == "resume" and index + 1 < len(parts) and parts[index + 1].strip():
            return 3
        if token == "--resume" and index + 1 < len(parts) and parts[index + 1].strip():
            return 3
        if token.startswith("--resume=") and token.partition("=")[2].strip():
            return 3
    return 1


def recipe_from_record(
    record: object,
    *,
    allow_legacy_fallback: bool = True,
    allow_external: bool = False,
) -> SessionRecipe | None:
    external = bool(getattr(record, "external", False))
    encoded = getattr(record, "recovery_recipe", None)
    digest = getattr(record, "recovery_recipe_sha256", None)
    if encoded is not None or digest is not None:
        if external and not allow_external:
            return None
        return parse_session_recipe(encoded, digest)
    if not allow_legacy_fallback or (external and not allow_external):
        return None
    recovery_command = str(getattr(record, "recovery_command", "") or "").strip()
    command = recovery_command or str(getattr(record, "command", "") or "").strip()
    recovery_cwd = str(getattr(record, "recovery_cwd", "") or "").strip()
    cwd = recovery_cwd or str(getattr(record, "cwd", "") or "").strip()
    if not command or not cwd:
        return None
    return build_session_recipe(
        command,
        cwd,
        restart_policy=str(
            getattr(record, "restart_policy", DEFAULT_RESTART_POLICY)
            or DEFAULT_RESTART_POLICY
        ),
        origin="legacy-recovery" if recovery_command else "legacy-launch",
    )


def record_recipe_fields(
    *,
    command: object,
    cwd: object,
    recovery_command: str | None = None,
    recovery_cwd: str | None = None,
    environment: Mapping[str, str] | None = None,
    restart_policy: str = DEFAULT_RESTART_POLICY,
    origin: str = "launch",
) -> tuple[str, str, str]:
    primary = (recovery_command or "").strip() or command
    primary_cwd = (recovery_cwd or "").strip() or cwd
    recipe = build_session_recipe(
        primary,
        primary_cwd,
        environment=environment,
        restart_policy=restart_policy,
        origin=origin,
    )
    encoded, digest = encode_session_recipe(recipe)
    return encoded, digest, recipe.restart_policy


def automatic_recovery_allowed(recipe: SessionRecipe | None) -> bool:
    return bool(recipe is not None and recipe.restart_policy in AUTO_RESTART_POLICIES)


def should_restart_recipe(
    recipe: SessionRecipe,
    *,
    session_missing: bool,
    exit_status: int | None = None,
) -> bool:
    if recipe.restart_policy not in AUTO_RESTART_POLICIES:
        return False
    if session_missing or recipe.restart_policy == "always":
        return True
    return exit_status is None or exit_status != 0


def resolve_working_directory(
    recipe: SessionRecipe,
    *,
    home: str | Path | None = None,
) -> ResolvedWorkingDirectory:
    requested = Path(recipe.cwd)
    try:
        if requested.is_dir():
            canonical = requested.resolve(strict=True)
            if canonical.is_dir():
                return ResolvedWorkingDirectory(requested, False, recipe.cwd)
    except (OSError, RuntimeError) as exc:
        raise SessionRecipeError(
            f"session recipe working directory is unavailable: {recipe.cwd}"
        ) from exc
    raise SessionRecipeError(
        f"session recipe working directory is unavailable: {recipe.cwd}"
    )
