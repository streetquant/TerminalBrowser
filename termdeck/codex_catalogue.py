"""Read codex-auth login profiles without running or reconfiguring codex-auth.

Only the explicitly supplied root is inspected. Alias and launch-home metadata
select paths, while validated credential content establishes account identity.
All descriptors are read-only; this module never creates catalogue state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .codex_credentials import (
    Credential, CredentialError, Observation, _read, _unique, observe_credential, read,
)


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LIMIT = 1024


@dataclass(frozen=True)
class AccountCatalogue:
    root: Path
    credentials: dict[str, Path]
    registry: dict[str, Any] | None
    canonical: Observation | None = None
    profile_layout: bool = False
    fingerprint: str = ""
    alternate_credentials: dict[str, tuple[Path, ...]] = field(default_factory=dict)
    credential_snapshots: dict[Path, Credential] = field(default_factory=dict, repr=False)
    private_catalogue_ready: bool = False


def _name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or value.casefold() == "registry":
        raise CredentialError("System account catalogue has an unsafe account name")
    return value


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Snapshot:
    """Anchor descendant reads to direct directory descriptors and revalidate."""

    def __init__(self, root: Path):
        self.root = root
        self.directories: dict[Path, int] = {}
        self.checked: dict[tuple[int | None, str], os.stat_result | None] = {}

    def __enter__(self):
        self.directory(self.root)
        return self

    def __exit__(self, *_):
        for fd in self.directories.values():
            os.close(fd)

    def inspect(self, directory: Path | None, name: str) -> os.stat_result | None:
        fd = self.directory(directory) if directory is not None else None
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        previous = self.checked.setdefault((fd, name), info)
        if ((previous is None) != (info is None)
                or previous is not None and _identity(previous) != _identity(info)):
            raise CredentialError("System account catalogue changed during observation")
        return info

    def directory(self, path: Path) -> int:
        if path in self.directories:
            return self.directories[path]
        parent = path.parent if path != self.root else None
        name = path.name if parent is not None else str(path)
        info = self.inspect(parent, name)
        if (info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise CredentialError("System account catalogue directory is unsafe or missing")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=self.directories.get(parent))
        if _identity(os.fstat(fd)) != _identity(info):
            os.close(fd)
            raise CredentialError("System account catalogue changed during observation")
        self.directories[path] = fd
        return fd

    def entries(self, directory: Path) -> list[str]:
        names = []
        with os.scandir(self.directory(directory)) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > _LIMIT:
                    raise CredentialError("System account catalogue exceeds the entry limit")
        return sorted(names)

    def metadata(self, name: str, *, limit: int = 65536) -> bytes | None:
        if self.inspect(self.root, name) is None:
            return None
        return _read(Path(name), dir_fd=self.directory(self.root), limit=limit)

    def credential(self, home: Path, name: str = "auth.json") -> tuple[Path, Credential]:
        info = self.inspect(home, name)
        if info is None:
            raise CredentialError("System account profile credentials are missing or revoked")
        path = home / name
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise CredentialError("System account credential link is unsafe")
            target = os.readlink(name, dir_fd=self.directory(home))
            path = Path(os.path.normpath(home / target))
            # codex-auth pins legacy profiles directly to a saved account.
            # It never needs a chain through another switchable auth.json.
            if path.parent != self.root / "accounts" or path.suffix != ".json":
                raise CredentialError("System account credential link escapes its saved account store")
            _name(path.stem)
            target_info = self.inspect(path.parent, path.name)
            if target_info is None or not stat.S_ISREG(target_info.st_mode):
                raise CredentialError("System account credential link is unsafe or revoked")
        return path, read(Path(path.name), dir_fd=self.directory(path.parent))

    def profile(self, value: object) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise CredentialError("System account profile path is invalid")
        path = Path(value)
        if (not path.is_absolute() or ".." in path.parts or str(path) != value
                or path != self.root and path.parent != self.root / "login-homes"):
            raise CredentialError("System account profile path is outside the configured source")
        if path != self.root:
            _name(path.name)
        self.directory(path)
        return path

    def validate(self) -> None:
        for (fd, name), before in self.checked.items():
            try:
                after = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                after = None
            if ((before is None) != (after is None)
                    or before is not None and _identity(before) != _identity(after)):
                raise CredentialError("System account catalogue changed during observation")


def _same_account(first: Credential, second: Credential) -> bool:
    return first.digest == second.digest or (
        first.identity is not None and first.identity == second.identity
    )


def read_private_profiles(home: Path, source: AccountCatalogue) -> AccountCatalogue:
    """Merge only this scope's proven snapshots into a system catalogue read.

    A private copy grants no account authority: its key and identity must still
    be present in the current system source. No reads create or refresh copies.
    """
    if not home.exists():
        return source
    try:
        with _Snapshot(home) as snapshot:
            accounts = home / "accounts"
            info = snapshot.inspect(home, "accounts")
            if info is None:
                snapshot.validate()
                return source
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != os.geteuid() or accounts.resolve(strict=True) != source.root / "accounts":
                    raise CredentialError("Private account catalogue link is outside its configured source")
                snapshot.validate()
                return source
            snapshot.directory(accounts)
            registry_info = snapshot.inspect(accounts, "registry.json")
            if registry_info is not None:
                private_registry = json.loads(_read(Path("registry.json"), dir_fd=snapshot.directory(accounts)),
                                              object_pairs_hook=_unique)
                if not isinstance(private_registry, dict):
                    raise CredentialError("Private account registry is malformed")
            credentials = dict(source.credentials)
            snapshots = dict(source.credential_snapshots)
            alternates = {key: set(paths) for key, paths in source.alternate_credentials.items()}
            copied = {}
            selectors = {name: row["account_key"] for row in (source.registry or {}).get("accounts", [])
                         for name in row["selectors"]}
            for filename in snapshot.entries(accounts):
                if filename == "registry.json" or not filename.endswith(".json"):
                    continue
                name = _name(Path(filename).stem)
                key = selectors.get(name)
                if key is None:
                    continue  # Revoked copies never enter the selectable set.
                path = accounts / filename
                entry = snapshot.inspect(accounts, filename)
                if entry is None:
                    raise CredentialError("Private account catalogue changed during observation")
                if stat.S_ISLNK(entry.st_mode):
                    if entry.st_uid != os.geteuid() or path.resolve(strict=True) not in alternates[key]:
                        raise CredentialError("Private account credential link is outside its configured source")
                    continue  # Previous shared layouts need stopped migration.
                private = read(Path(filename), dir_fd=snapshot.directory(accounts))
                current = snapshots[credentials[key]]
                if not _same_account(private, current):
                    raise CredentialError("Private saved account conflicts with its current system identity")
                if private.digest != current.digest and (
                    private.refreshed is None or current.refreshed is None or private.refreshed == current.refreshed
                ):
                    raise CredentialError("Private saved account has conflicting credential snapshots")
                snapshots[path] = private
                alternates.setdefault(key, set()).add(path)
                if private.digest == current.digest or private.refreshed > current.refreshed:
                    credentials[key] = path
                if name == key:
                    copied[key] = private
            snapshot.validate()
            ready = registry_info is not None and all(
                key in copied and copied[key].digest == snapshots[path].digest for key, path in credentials.items())
            return AccountCatalogue(source.root, credentials, source.registry, source.canonical, True,
                                    source.fingerprint, {key: tuple(paths) for key, paths in alternates.items()},
                                    snapshots, ready)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        if isinstance(exc, CredentialError):
            raise
        raise CredentialError("Private account catalogue is malformed, unsafe, or changed") from exc


def read_profiles(root: Path, legacy: dict[str, Path], registry: dict | None) -> AccountCatalogue | None:
    """Adapt codex-auth's profiles when its metadata exists at this source root.

    Repeated names for one profile become selectors for one account. Multiple
    snapshots of that account require either equal content or an unambiguous
    refresh order. A stale selected home remains visibly stale.
    """
    try:
        with _Snapshot(root) as snapshot:
            present = {name: snapshot.inspect(root, name) for name in (
                "login-homes", "account-profiles.json", "next-login-home",
            )}
            if not any(info is not None for info in present.values()):
                snapshot.validate()
                return None
            if registry is not None:
                raise CredentialError("System login profiles conflict with the account registry layout")
            aliases_raw = snapshot.metadata("account-profiles.json")
            aliases = json.loads(aliases_raw, object_pairs_hook=_unique) if aliases_raw is not None else {}
            if not isinstance(aliases, dict) or len(aliases) > _LIMIT:
                raise CredentialError("System account profile registry is invalid")
            marker = snapshot.metadata("next-login-home", limit=4096)
            selected_home = snapshot.profile(marker.decode("utf-8").strip()) if marker is not None else root

            # name -> (credential path, snapshot). Keep selectors separate from
            # credential identity so aliases never create duplicate identities.
            entries: dict[str, tuple[Path, Credential]] = {}
            snapshots: list[tuple[str, Path, Credential]] = []
            spellings: dict[str, str] = {}
            legacy_names = set(legacy)
            profile_names: set[str] = set()
            alias_names: set[str] = set()

            def add(name: str, value: tuple[Path, Credential], *, alias: bool = False):
                _name(name)
                previous_spelling = spellings.setdefault(name.casefold(), name)
                if previous_spelling != name:
                    raise CredentialError("System account selectors differ only by case")
                previous = entries.get(name)
                if previous is not None and (
                    not _same_account(previous[1], value[1])
                    or not alias and previous[0] != value[0]
                ):
                    raise CredentialError("System account selector has conflicting credentials")
                entries[name] = value
                snapshots.append((name, *value))

            # Native aliases are authoritative for their exact names. Validate
            # each target before considering a legacy credential with that name.
            alias_credentials = {}
            for name, value in aliases.items():
                _name(name)
                alias_names.add(name)
                alias_credentials[name] = snapshot.credential(snapshot.profile(value))
            if legacy:
                if len(legacy) > _LIMIT:
                    raise CredentialError("System account catalogue exceeds the entry limit")
                for name, path in legacy.items():
                    value = snapshot.credential(path.parent, path.name)
                    authoritative = alias_credentials.get(name)
                    if authoritative is not None and not _same_account(authoritative[1], value[1]):
                        continue  # A shadowed snapshot cannot retain this selector.
                    add(name, value)
            for name, value in alias_credentials.items():
                add(name, value, alias=True)
            if present["login-homes"] is not None:
                profiles = root / "login-homes"
                for name in snapshot.entries(profiles):
                    if not name.startswith("login-"):
                        continue
                    profile = snapshot.profile(str(profiles / name))
                    if snapshot.inspect(profile, "auth.json") is None:
                        continue  # An unfinished login is not a saved account.
                    profile_names.add(name)
                    add(name, snapshot.credential(profile))

            grouped: dict[str, list[tuple[str, Path, Credential]]] = {}
            for name, path, credential in snapshots:
                identity = "identity:" + credential.identity if credential.identity else "digest:" + credential.digest
                grouped.setdefault(identity, []).append((name, path, credential))
            credentials: dict[str, Path] = {}
            alternate_credentials: dict[str, tuple[Path, ...]] = {}
            saved: dict[str, Credential] = {}
            rows = []
            for group in grouped.values():
                names = {name for name, _, _ in group}
                key = min(names & legacy_names or names & profile_names or names)
                # Prefer a live profile to its equal legacy copy, retaining a
                # stable target across an ordinary refresh of that profile.
                group.sort(key=lambda row: (row[1].parent == root / "accounts", str(row[1]), row[0]))
                newest = group[0]
                for candidate in group[1:]:
                    old, new = newest[2], candidate[2]
                    if old.digest == new.digest:
                        continue
                    if old.refreshed is None or new.refreshed is None or old.refreshed == new.refreshed:
                        raise CredentialError("System account has conflicting credential snapshots")
                    if new.refreshed > old.refreshed:
                        newest = candidate
                credentials[key] = newest[1]
                alternate_credentials[key] = tuple(sorted({path for _, path, _ in group}, key=str))
                saved[key] = newest[2]
                labels = names & alias_names
                rows.append({"account_key": key, "alias": min(labels) if labels else key,
                             "account_name": key, "selectors": sorted(names)})

            if snapshot.inspect(selected_home, "auth.json") is None:
                if marker is not None:
                    raise CredentialError("Selected system account profile is missing or revoked")
                canonical = Observation(None, "unselected")
            else:
                _, current = snapshot.credential(selected_home)
                canonical = observe_credential(current, saved)
            snapshot.validate()
            rows.sort(key=lambda row: row["account_key"].casefold())
            private_registry = {"accounts": rows, "active_account_key": None}
            fingerprint = hashlib.sha256(json.dumps({
                "registry": private_registry, "selected_home": str(selected_home),
                "selected_digest": canonical.credential.digest if canonical.credential else None,
                "credentials": {key: [str(credentials[key]), value.digest]
                                for key, value in saved.items()},
                "alternates": {key: [str(path) for path in paths]
                               for key, paths in alternate_credentials.items()},
                "snapshots": {str(path): credential.digest for _, path, credential in snapshots},
            }, sort_keys=True).encode()).hexdigest()
            return AccountCatalogue(root, credentials, private_registry, canonical, True, fingerprint,
                                    alternate_credentials, {path: credential for _, path, credential in snapshots})
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, CredentialError):
            raise
        raise CredentialError("System account catalogue is malformed, unsafe, or changed") from exc
