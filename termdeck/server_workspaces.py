"""Directory authority for authenticated server workspace selections.

The browser supplies an absolute server path, never a shell expression.  The
stored receipt pins its canonical directory ancestry; execution keeps an open
descriptor through bubblewrap's mount so a renamed path cannot change the grant.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .client_files import StagingError, _text


_RESERVED_NAMES = frozenset({
    ".termdeck", ".beads", ".bv", ".codex", ".ssh", ".gnupg",
    "terminal-auth", "t1000-managed", "web-launch-policy.json",
    "termdeck-machines-v1.sqlite3", "termdeck-machines-v1.sqlite3-wal",
    "termdeck-machines-v1.sqlite3-shm",
})
_SPECIAL_ROOTS = tuple(Path(p) for p in ("/proc", "/sys", "/dev", "/run"))
_MAX_ENTRIES = 1_000_000
_MAX_DEPTH = 128
_MAX_MASKS = 4096
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_PARENT = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _within(path, root):
    return path == root or root in path.parents


def _reserved(name):
    return name in _RESERVED_NAMES or name.startswith(".termdeck-")


def _path(value):
    _text(value, 4096)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise StagingError("Enter an absolute server directory without '..'")
    return path


def _identity(info):
    return [info.st_dev, info.st_ino]


def _inspection_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def protected_roots(state_root, protected=()):
    """Known private/runtime authority, independent of workspace registration."""
    state = Path(state_root)
    home = Path.home()
    roots = [state, *(p for p in state.parents if p.name == ".termdeck"),
             *(Path(p) for p in protected),
             home / ".codex", home / ".ssh", home / ".gnupg",
             home / ".config/termdeck", home / ".local/share/termdeck",
             home / ".config/systemd", Path("/etc/termdeck"),
             Path("/var/lib/termdeck-preparer"), Path("/usr/local/lib/termdeck"),
             Path("/usr/local/libexec/termdeck")]
    for key in ("TERMDECK_CODEX_ACCOUNT_SOURCE", "TERMDECK_TMUX_RUNTIME",
                "TERMDECK_TMUX_ROOT", "CODEX_TERMDECK_GATE_ROOT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            roots.append(Path(raw).expanduser())
    # These resolve names only; no protected file contents are inspected.
    return tuple(dict.fromkeys(candidate for p in roots
                               for candidate in (p.absolute(), p.resolve(strict=False))))


class WorkspaceDirectory:
    """A checked canonical directory and a bounded set of descendant masks."""

    def __init__(self, path, *, state_root, protected=(), pin=None, inspect=True):
        self.fd = -1
        self._inspected = {}
        supplied = _path(path)
        try:
            canonical = supplied.resolve(strict=True) if pin is None else supplied
            if (canonical == Path("/") or any(_within(canonical, p) for p in _SPECIAL_ROOTS)):
                raise StagingError("System and runtime control directories cannot be workspaces", "unsafe-storage")
            if any(_reserved(part) for part in canonical.parts):
                raise StagingError("Protected storage cannot be a workspace", "unsafe-storage")
            self.path = canonical
            self.protected = protected_roots(state_root, protected)
            if any(_within(canonical, root) for root in self.protected):
                raise StagingError("Protected storage cannot be a workspace", "unsafe-storage")
            self.fd, self.chain = self._open(canonical)
            access_path = f"/proc/self/fd/{self.fd}"
            if not os.access(access_path, os.R_OK | os.X_OK, effective_ids=True):
                raise StagingError("Server directory is not readable and searchable", "unavailable-workspace")
            self.read_only = not os.access(access_path, os.W_OK, effective_ids=True)
            if pin is not None:
                if (not isinstance(pin, dict) or set(pin) != {"version", "chain", "read_only"}
                        or pin["version"] != 1 or type(pin["read_only"]) is not bool
                        or pin["chain"] != self.chain):
                    raise StagingError("Server directory identity changed; select it again", "conflict")
                # A previously read-only selection cannot gain write authority.
                self.read_only = pin["read_only"] or self.read_only
            self.masks = self._masks() if inspect else ()
            self.check()
        except StagingError:
            self.close()
            raise
        except (OSError, RuntimeError) as error:
            self.close()
            raise StagingError("Server directory is missing, inaccessible, or changed", "unavailable-workspace") from error

    @staticmethod
    def _open(path):
        current = os.open("/", _OPEN_PARENT)
        try:
            chain = [_identity(os.fstat(current))]
            for index, part in enumerate(path.parts[1:]):
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
                flags = _OPEN_DIRECTORY if index == len(path.parts) - 2 else _OPEN_PARENT
                next_fd = os.open(part, flags, dir_fd=current)
                try:
                    opened = os.fstat(next_fd)
                    after = os.stat(part, dir_fd=current, follow_symlinks=False)
                    if (_identity(before) != _identity(opened) or _identity(after) != _identity(opened)
                            or not stat.S_ISDIR(opened.st_mode)):
                        raise StagingError("Server directory changed while opening", "conflict")
                    chain.append(_identity(opened))
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current)
                current = next_fd
            result = current, chain
            current = -1
            return result
        finally:
            if current >= 0:
                os.close(current)

    @property
    def pin(self):
        return {"version": 1, "chain": self.chain, "read_only": self.read_only}

    def check(self):
        descriptor, chain = self._open(self.path)
        try:
            if chain != self.chain or _identity(os.fstat(self.fd)) != chain[-1]:
                raise StagingError("Server directory identity changed; select it again", "conflict")
            for path, identity in self._inspected.items():
                if _inspection_identity(path.lstat()) != identity:
                    raise StagingError("Server directory changed while inspecting; try again", "conflict")
        finally:
            os.close(descriptor)

    def _masks(self):
        masks = []
        entries = 0

        def add(path, kind):
            if len(masks) >= _MAX_MASKS:
                raise StagingError("Server directory has too many protected locations; select a narrower directory", "capacity")
            if _within(self.path, path):
                raise StagingError("Protected storage alias would expose the workspace root", "unsafe-storage")
            self._inspected[path] = _inspection_identity(path.lstat())
            masks.append((str(path), kind))

        def walk(fd, path, depth):
            nonlocal entries
            self._inspected[path] = _inspection_identity(os.fstat(fd))
            if depth > _MAX_DEPTH:
                raise StagingError("Server directory is too deep to validate; select a narrower directory", "capacity")
            with os.scandir(fd) as listing:
                for entry in listing:
                    entries += 1
                    if entries > _MAX_ENTRIES:
                        raise StagingError("Server directory is too large to validate; select a narrower directory", "capacity")
                    target = path / entry.name
                    info = entry.stat(follow_symlinks=False)
                    hidden = _reserved(entry.name) or target in self.protected
                    if stat.S_ISLNK(info.st_mode):
                        # A reserved alias never grants its target. Mask the
                        # canonical target even when another alias reaches it;
                        # ordinary external links have no host mount to follow.
                        if hidden:
                            self._inspected[target] = _inspection_identity(info)
                            resolved = target.resolve(strict=True)
                            resolved_info = resolved.stat()
                            add(resolved, "directory" if stat.S_ISDIR(resolved_info.st_mode) else "file")
                        continue
                    if hidden:
                        add(target, "directory" if stat.S_ISDIR(info.st_mode) else "file")
                    elif stat.S_ISDIR(info.st_mode):
                        try:
                            child = os.open(entry.name, _OPEN_DIRECTORY, dir_fd=fd)
                        except PermissionError:
                            # Inaccessible children cannot be inspected. Keep
                            # them hidden even if their owner could chmod them.
                            add(target, "directory")
                            continue
                        try:
                            after = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                            if _identity(info) != _identity(os.fstat(child)) or _identity(info) != _identity(after):
                                raise StagingError("Server directory changed while inspecting", "conflict")
                            try:
                                walk(child, target, depth + 1)
                            except PermissionError:
                                add(target, "directory")
                        finally:
                            os.close(child)
                    elif not stat.S_ISREG(info.st_mode):
                        # Host sockets/devices/FIFOs never become a control API.
                        add(target, "file")

        walk(self.fd, self.path, 0)
        # A catalogue alias can reveal a parent of a mask already discovered.
        # Install only the outer masks so mount order cannot recreate data.
        selected = []
        for path, kind in sorted(set(masks), key=lambda value: (len(Path(value[0]).parts), value[0])):
            if not any(_within(Path(path), Path(parent)) for parent, _ in selected):
                selected.append((path, kind))
        return tuple(selected)

    def mount_arguments(self):
        """Caller must retain/pass fd to bwrap until its namespace is built."""
        self.check()
        args = ["--ro-bind-fd" if self.read_only else "--bind-fd", str(self.fd), str(self.path)]
        for path, kind in self.masks:
            if kind == "directory":
                # Empty private mountpoints also permit later, explicit
                # attachment/private-home mounts below masked runtime roots.
                args.extend(["--tmpfs", path])
            else:
                args.extend(["--ro-bind", "/dev/null", path])
        return args

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def select(path, *, state_root, protected=()):
    """Return server-generated metadata suitable for an immutable launch plan."""
    directory = WorkspaceDirectory(path, state_root=state_root, protected=protected)
    try:
        return {"path": str(directory.path), "label": directory.path.name,
                "source": "server-directory", "pin": directory.pin}
    finally:
        directory.close()


@contextmanager
def open_workspace(workspace, *, state_root, protected=(), inspect=True):
    if "pin" not in workspace:
        raise StagingError("Server directory receipt is unavailable", "conflict")
    directory = WorkspaceDirectory(workspace["path"], state_root=state_root,
                                   protected=protected, pin=workspace["pin"], inspect=inspect)
    try:
        yield directory
    finally:
        directory.close()
