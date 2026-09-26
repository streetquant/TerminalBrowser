"""Explicit, quiescent repair of a saved terminal's configuration authority.

This operation never provisions a scope, switches accounts, rewrites a session
row, or starts a terminal. Only links to the explicitly expected old non-auth
source and the private managed Codex launcher may be replaced. Credential and
history paths are outside the migration plan.
"""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import codex_config_scope as config
from .models import SessionRecord
from .session_store import SessionStore
from .tmux_backend import TmuxBackend


class ConfigurationRebindError(config.CodexConfigScopeError):
    """The saved configuration binding cannot be changed safely."""


def _absolute(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigurationRebindError(f"{label} must be an explicit absolute path")
    return candidate


def _stamp(path: str | Path, *, dir_fd: int | None = None) -> tuple | None:
    try:
        value = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        os.readlink(path, dir_fd=dir_fd) if stat.S_ISLNK(value.st_mode) else None,
    )


def _directory_identity(path: Path) -> tuple:
    value = path.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_mode & 0o022
    ):
        raise ConfigurationRebindError(f"private configuration directory is unsafe: {path}")
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _source_snapshot(requested: Path, home: Path) -> tuple[Path, tuple, tuple]:
    source = requested.resolve(strict=True)
    if source == home or home in source.parents:
        raise ConfigurationRebindError("configuration source must be outside the private home")
    config._validate_canonical_root(source)
    assets = config._canonical_assets(source)
    identities = []
    for name, target, directory in assets:
        resolved = target.resolve(strict=True)
        if resolved == home or home in resolved.parents:
            raise ConfigurationRebindError("configuration source asset points into the private home")
        identities.append((name, str(resolved), _stamp(target), _stamp(resolved)))
    return source, assets, (_stamp(requested), _stamp(source), tuple(identities))


@dataclass(frozen=True)
class ConfigurationRebindPlan:
    private_home: Path
    expected_source: Path
    source: Path
    launcher: Path
    replaced_paths: tuple[str, ...]
    _requested_home: Path
    _requested_source: Path
    _home_identity: tuple
    _source_identity: tuple
    _assets: tuple
    _nodes: tuple[tuple[str, tuple], ...]
    _bin_identity: tuple | None
    _wrapper: tuple | None
    _launcher_identity: tuple | None
    _refresh_launcher: bool


def _preserved_launcher(home: Path) -> Path:
    try:
        return config.preserved_private_launcher(home)
    except config.CodexConfigScopeError as exc:
        raise ConfigurationRebindError(str(exc)) from exc


def inspect_configuration_rebind(
    private_home: str | Path,
    *,
    expected_source: str | Path,
    source: str | Path,
    refresh_launcher: bool = False,
    launcher_attestor: config.LauncherAttestor | None = None,
) -> ConfigurationRebindPlan:
    """Read-only preflight. This proves neither quiescence nor actual recovery.

    The old source may be absent. Its absolute spelling must match the saved
    binding and every existing shared link; unrelated links and copied private
    configuration require separate review. The new source is never inferred
    from ``CODEX_HOME`` or another agent's environment.
    """

    requested_home = _absolute(private_home, "private home")
    home = config._existing_private_home(requested_home)
    expected = _absolute(expected_source, "expected source")
    requested_source = _absolute(source, "source")
    canonical, assets, source_identity = _source_snapshot(requested_source, home)
    if expected == canonical:
        raise ConfigurationRebindError("new configuration source must differ from the expected old source")
    names = set(config._SHARED_FILES) | set(config._SHARED_DIRECTORIES)
    names.update(item.name for item in home.glob("*.config.toml"))
    # Validate the authority before its per-asset links, including replay.
    names_in_order = [config._CANONICAL_HOME_BINDING, *sorted(names)]
    nodes = []
    for name in names_in_order:
        node = _stamp(home / name)
        if node is None:
            if name == config._CANONICAL_HOME_BINDING:
                raise ConfigurationRebindError("saved private home has no canonical configuration binding")
            continue
        target = expected if name == config._CANONICAL_HOME_BINDING else expected / name
        if (
            not stat.S_ISLNK(node[2]) or node[3] != os.geteuid()
            or node[5] != 1 or node[9] != str(target)
        ):
            raise ConfigurationRebindError(
                f"saved configuration link does not match the expected source: {home / name}"
            )
        nodes.append((name, node))

    bin_path = home / "bin"
    bin_identity = _directory_identity(bin_path) if _stamp(bin_path) is not None else None
    wrapper = _stamp(bin_path / "codex") if bin_identity is not None else None
    if wrapper is not None and (
        not stat.S_ISREG(wrapper[2]) or wrapper[3] != os.geteuid()
        or wrapper[5] != 1 or wrapper[2] & 0o022
    ):
        raise ConfigurationRebindError("private Codex launcher has unsafe authority")
    if refresh_launcher:
        launcher = config.resolve_codex_launcher(home, launcher_attestor=launcher_attestor)
        if launcher is None:
            raise ConfigurationRebindError("no attested host Codex launcher is available")
    else:
        if wrapper is None:
            raise ConfigurationRebindError("no existing private launcher can be preserved; an attested refresh is required")
        launcher = _preserved_launcher(home)
        if _stamp(bin_path / "codex") != wrapper:
            raise ConfigurationRebindError("private Codex launcher changed during inspection")
    return ConfigurationRebindPlan(
        private_home=home, expected_source=expected, source=canonical,
        launcher=launcher,
        replaced_paths=tuple(name for name, _ in nodes) + (("bin/codex",) if refresh_launcher and wrapper else ()),
        _requested_home=requested_home, _requested_source=requested_source,
        _home_identity=_directory_identity(home), _source_identity=source_identity,
        _assets=assets, _nodes=tuple(nodes), _bin_identity=bin_identity,
        _wrapper=wrapper, _launcher_identity=_stamp(launcher) if refresh_launcher else None,
        _refresh_launcher=refresh_launcher,
    )


def _check_authority(plan: ConfigurationRebindPlan) -> None:
    if (
        config._existing_private_home(plan._requested_home) != plan.private_home
        or _directory_identity(plan.private_home) != plan._home_identity
    ):
        raise ConfigurationRebindError("saved private home changed during configuration rebind")
    source, assets, identity = _source_snapshot(plan._requested_source, plan.private_home)
    if (source, assets, identity) != (plan.source, plan._assets, plan._source_identity):
        raise ConfigurationRebindError("authorized configuration source changed during rebind")
    if plan._refresh_launcher and _stamp(plan.launcher) != plan._launcher_identity:
        raise ConfigurationRebindError("attested Codex launcher changed during rebind")
    if not plan._refresh_launcher and _stamp(plan.private_home / "bin" / "codex") != plan._wrapper:
        raise ConfigurationRebindError("preserved private launcher changed during rebind")


def _open_directory(path: Path, identity: tuple) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    value = os.fstat(descriptor)
    if (value.st_dev, value.st_ino, value.st_mode, value.st_uid) != identity:
        os.close(descriptor)
        raise ConfigurationRebindError("private configuration directory changed while opening")
    return descriptor


def _restore_moved(original_fd: int, name: str, backup_fd: int, saved: str) -> None:
    # Hard-link publication is no-replace, including when the saved inode is a
    # symlink. No concurrent/unrecognized destination is overwritten.
    os.link(saved, name, src_dir_fd=backup_fd, dst_dir_fd=original_fd, follow_symlinks=False)
    os.unlink(saved, dir_fd=backup_fd)


def _publish_wrapper(directory_fd: int, content: str) -> None:
    name = f".codex-rebind-{uuid.uuid4().hex}"
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o700, dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, "codex", src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
    finally:
        os.unlink(name, dir_fd=directory_fd)


def _apply_rebind(plan: ConfigurationRebindPlan, validate_record) -> Path:
    home = plan.private_home
    with config._scope_preparation_lock(home):
        _check_authority(plan)
        for name, expected in plan._nodes:
            if _stamp(home / name) != expected:
                raise ConfigurationRebindError("saved configuration binding changed before rebind")
        if _stamp(home / "bin" / "codex") != plan._wrapper:
            raise ConfigurationRebindError("private Codex launcher changed before rebind")
        if plan._bin_identity is not None and _directory_identity(home / "bin") != plan._bin_identity:
            raise ConfigurationRebindError("private launcher directory changed before rebind")
        validate_record()

        home_fd = _open_directory(home, plan._home_identity)
        bin_fd = backup_fd = None
        anchor = Path(f"/proc/self/fd/{home_fd}")
        backup = None
        created_bin = False
        moved: list[tuple[int, str, str]] = []
        created: list[tuple[str, tuple]] = []
        wrapper_attempted = False
        binding_copied = False
        binding_committed = False
        committed_binding = None
        binding_temporary = f".termdeck-config-rebind-{uuid.uuid4().hex}"
        binding_temporary_stamp = None
        binding = config._CANONICAL_HOME_BINDING
        binding_before = dict(plan._nodes)[binding]
        wrapper_content = config._wrapper_content(home, plan.launcher)
        try:
            if plan._bin_identity is None:
                os.mkdir("bin", 0o700, dir_fd=home_fd)
                created_bin = True
            bin_identity = _directory_identity(anchor / "bin")
            if plan._bin_identity is not None and bin_identity != plan._bin_identity:
                raise ConfigurationRebindError("private launcher directory changed during rebind")
            bin_fd = _open_directory(anchor / "bin", bin_identity)
            backup = config._new_migration_backup(anchor)
            backup_fd = _open_directory(backup, _directory_identity(backup))

            for name, expected in plan._nodes:
                if name == binding:
                    continue
                _check_authority(plan)
                if _stamp(name, dir_fd=home_fd) != expected:
                    raise ConfigurationRebindError("private configuration changed before backup")
                os.rename(name, name, src_dir_fd=home_fd, dst_dir_fd=backup_fd)
                moved.append((home_fd, name, name))
            if plan._refresh_launcher and plan._wrapper is not None:
                if _stamp("codex", dir_fd=bin_fd) != plan._wrapper:
                    raise ConfigurationRebindError("private Codex launcher changed before backup")
                os.rename("codex", "codex-launcher", src_dir_fd=bin_fd, dst_dir_fd=backup_fd)
                moved.append((bin_fd, "codex", "codex-launcher"))
            for name, target, directory in plan._assets:
                _check_authority(plan)
                os.symlink(target, name, dir_fd=home_fd, target_is_directory=directory)
                created.append((name, _stamp(name, dir_fd=home_fd)))
            if plan._refresh_launcher:
                wrapper_attempted = True
                _publish_wrapper(bin_fd, wrapper_content)
            _check_authority(plan)
            if _directory_identity(anchor / "bin") != bin_identity:
                raise ConfigurationRebindError("private launcher directory changed during publication")
            validate_record()

            # Commit the binding last and atomically. Keep its old symlink inode
            # in the backup; the live binding is never temporarily absent.
            if _stamp(binding, dir_fd=home_fd) != binding_before:
                raise ConfigurationRebindError("expected canonical binding changed before commit")
            os.link(binding, binding, src_dir_fd=home_fd, dst_dir_fd=backup_fd, follow_symlinks=False)
            binding_copied = True
            copied_binding = _stamp(binding, dir_fd=home_fd)
            if copied_binding[:5] != binding_before[:5] or copied_binding[5] != 2 or copied_binding[9] != binding_before[9]:
                raise ConfigurationRebindError("expected canonical binding changed during commit")
            os.symlink(plan.source, binding_temporary, dir_fd=home_fd, target_is_directory=True)
            binding_temporary_stamp = _stamp(binding_temporary, dir_fd=home_fd)
            if _stamp(binding, dir_fd=home_fd) != copied_binding:
                raise ConfigurationRebindError("expected canonical binding changed before atomic replacement")
            os.replace(binding_temporary, binding, src_dir_fd=home_fd, dst_dir_fd=home_fd)
            binding_committed = True
            binding_temporary_stamp = None
            committed_binding = _stamp(binding, dir_fd=home_fd)
            _check_authority(plan)
            if _directory_identity(anchor / "bin") != bin_identity:
                raise ConfigurationRebindError("private launcher directory changed during commit")
            validate_record()
            final_plan = config._configuration_plan(home, plan.source)
            status = config._status_from_plan(home, plan.source, plan.launcher, final_plan)
            if (
                not status.configuration_ready
                or any(item[3] != "shared" for item in final_plan.assets)
                or (plan._refresh_launcher and not status.launcher_ready)
            ):
                raise ConfigurationRebindError("configuration rebind did not reach canonical parity")
            for descriptor in (backup_fd, bin_fd, home_fd):
                os.fsync(descriptor)
            backup_root_fd = _open_directory(backup.parent, _directory_identity(backup.parent))
            try:
                os.fsync(backup_root_fd)
            finally:
                os.close(backup_root_fd)
            return home / config._CONFIG_BACKUP_ROOT / backup.name
        except Exception as exc:
            errors = []
            if binding_committed:
                try:
                    if committed_binding is None or _stamp(binding, dir_fd=home_fd) != committed_binding:
                        raise ConfigurationRebindError("canonical binding changed before rollback")
                    os.replace(binding, binding, src_dir_fd=backup_fd, dst_dir_fd=home_fd)
                    binding_copied = False
                except (OSError, config.CodexConfigScopeError) as error:
                    errors.append(str(error))
            elif binding_copied:
                try:
                    os.unlink(binding, dir_fd=backup_fd)
                    binding_copied = False
                except OSError as error:
                    errors.append(str(error))
            for name, expected in reversed(created):
                try:
                    if _stamp(name, dir_fd=home_fd) != expected:
                        raise ConfigurationRebindError(f"published configuration link changed before rollback: {name}")
                    os.unlink(name, dir_fd=home_fd)
                except (OSError, config.CodexConfigScopeError) as error:
                    errors.append(str(error))
            if wrapper_attempted and bin_fd is not None:
                try:
                    wrapper = Path(f"/proc/self/fd/{bin_fd}") / "codex"
                    node = _stamp(wrapper)
                    if node is not None:
                        if not stat.S_ISREG(node[2]) or wrapper.read_text(encoding="utf-8") != wrapper_content:
                            raise ConfigurationRebindError("private launcher changed before rollback")
                        os.unlink("codex", dir_fd=bin_fd)
                except (OSError, UnicodeError, config.CodexConfigScopeError) as error:
                    errors.append(str(error))
            for original_fd, name, saved in reversed(moved):
                try:
                    _restore_moved(original_fd, name, backup_fd, saved)
                except OSError as error:
                    errors.append(str(error))
            if not errors and backup is not None:
                config._remove_empty_backup(backup)
            if not errors and created_bin:
                try:
                    os.rmdir("bin", dir_fd=home_fd)
                except OSError as error:
                    errors.append(str(error))
            if errors:
                raise ConfigurationRebindError(
                    "configuration rebind failed; rollback evidence retained in the private backup: "
                    + "; ".join(errors)
                ) from exc
            if isinstance(exc, config.CodexConfigScopeError):
                raise
            raise ConfigurationRebindError("configuration rebind failed and was rolled back") from exc
        finally:
            try:
                if binding_temporary_stamp is not None:
                    if _stamp(binding_temporary, dir_fd=home_fd) != binding_temporary_stamp:
                        raise ConfigurationRebindError("temporary configuration binding changed; cleanup refused")
                    os.unlink(binding_temporary, dir_fd=home_fd)
            finally:
                for descriptor in (backup_fd, bin_fd, home_fd):
                    if descriptor is not None:
                        os.close(descriptor)


def rebind_terminal_configuration(
    record: SessionRecord,
    store: SessionStore,
    backend: TmuxBackend,
    *,
    expected_source: str | Path,
    source: str | Path,
    refresh_launcher: bool = False,
    launcher_attestor: config.LauncherAttestor | None = None,
) -> dict[str, object]:
    """Repair one exact saved scope while retaining every session field.

    No durable recovery intent or running flag authorizes this operation:
    explicit source arguments and backend writer fencing are required. The
    registry transaction prevents account/scope/recipe changes through commit.
    Repeating a successful operation with the old expected source is refused.
    """

    if record.external or not record.codex_home or not record.auth_scope_id:
        raise ConfigurationRebindError("an internal saved terminal with an existing private scope is required")
    if store.conn.in_transaction:
        raise ConfigurationRebindError("configuration rebind requires an idle registry connection")
    plan = inspect_configuration_rebind(
        record.codex_home, expected_source=expected_source, source=source,
        refresh_launcher=refresh_launcher, launcher_attestor=launcher_attestor,
    )

    def validate_record() -> None:
        binding = getattr(store, "_database_binding", None)
        if binding is not None:
            binding.revalidate()
        if store.get(record.id) != record:
            raise ConfigurationRebindError("saved terminal changed before configuration rebind")
        other = store.conn.execute(
            "SELECT id FROM sessions WHERE id != ? AND (codex_home = ? OR auth_scope_id = ?) LIMIT 1",
            (record.id, record.codex_home, record.auth_scope_id),
        ).fetchone()
        if other is not None:
            raise ConfigurationRebindError("private scope is also bound to another saved terminal")
        if record.last_pid and Path(f"/proc/{record.last_pid}").exists():
            raise ConfigurationRebindError("saved terminal process is still present; quiescence is unproven")

    validate_record()
    with backend.auth_scope_writer_quiescence(record) as observation:
        if observation.get("quiescent") is not True:
            raise ConfigurationRebindError("terminal writer is still active; configuration rebind refused")
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            validate_record()
            backup = _apply_rebind(plan, validate_record)
        finally:
            # This transaction excludes competing row writers, but intentionally
            # contains no UPDATE/INSERT and changes no lifecycle/account fields.
            store.conn.rollback()
    return {
        "action": "auth-rebind-config", "ok": True, "id": record.id,
        "auth_scope_id": record.auth_scope_id, "private_home": str(plan.private_home),
        "expected_source": str(plan.expected_source), "source": str(plan.source),
        "backup_path": str(backup), "replaced_paths": list(plan.replaced_paths),
        "session_fields_preserved": True, "terminal_started": False,
        "configuration_ready": True, "launcher_preserved": not plan._refresh_launcher,
        "launcher_qualified": plan._refresh_launcher,
    }
