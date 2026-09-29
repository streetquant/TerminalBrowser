"""Canonical cwd receipts for authenticated host-user terminal launches.

A receipt pins the chosen directory and its canonical ancestry. It grants no
filesystem isolation: the child retains the host user's ordinary OS access.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .client_files import StagingError, _text


_OPEN_DIRECTORY = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _path(value):
    _text(value, 4096)
    path = Path(value)
    if not path.is_absolute():
        raise StagingError("Enter an absolute server directory")
    return path


def _identity(info):
    return [info.st_dev, info.st_ino]


class WorkspaceDirectory:
    """A searchable host directory held by descriptor until exec."""

    def __init__(self, path, *, state_root=None, protected=(), pin=None, inspect=True):
        # Retain call compatibility; neither state nor descendants restrict cwd.
        self.fd = -1
        supplied = _path(path)
        try:
            self.path = supplied.resolve(strict=True) if pin is None else supplied
            if pin is not None and (
                not isinstance(pin, dict) or set(pin) != {"version", "chain"}
                or type(pin["version"]) is not int or pin["version"] != 2
            ):
                raise StagingError("Server directory receipt requires a new host-user launch", "conflict")
            self.fd, self.chain = self._open(self.path)
            if not os.access(f"/proc/self/fd/{self.fd}", os.X_OK, effective_ids=True):
                raise StagingError("Server directory is not searchable", "unavailable-workspace")
            if pin is not None and pin["chain"] != self.chain:
                raise StagingError("Server directory identity changed; select it again", "conflict")
            self.check()
        except StagingError:
            self.close()
            raise
        except (OSError, RuntimeError) as error:
            self.close()
            message = "Server directory identity changed; select it again" if pin is not None else "Server directory is missing, inaccessible, or changed"
            raise StagingError(message, "conflict" if pin is not None else "unavailable-workspace") from error

    @staticmethod
    def _open(path):
        current = os.open("/", _OPEN_DIRECTORY)
        try:
            chain = [_identity(os.fstat(current))]
            for part in path.parts[1:]:
                if part in {".", ".."}:
                    raise StagingError("Server directory receipt is not canonical", "conflict")
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
                next_fd = os.open(part, _OPEN_DIRECTORY, dir_fd=current)
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
        return {"version": 2, "chain": self.chain}

    def check(self):
        descriptor = -1
        try:
            descriptor, chain = self._open(self.path)
            if chain != self.chain or _identity(os.fstat(self.fd)) != chain[-1]:
                raise StagingError("Server directory identity changed; select it again", "conflict")
        except OSError as error:
            raise StagingError("Server directory identity changed; select it again", "conflict") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @contextmanager
    def entered(self):
        """Enter the pinned object, rechecking names before the caller execs.

        Even a rename after the final check cannot redirect cwd to a replacement.
        The descriptor is close-on-exec; it grants no extra inherited handles.
        Restore cwd if exec fails (or a test substitutes the exec operation).
        """
        previous = os.open(".", _OPEN_DIRECTORY)
        try:
            self.check()
            try:
                os.fchdir(self.fd)
            except OSError as error:
                raise StagingError("Server directory is no longer searchable", "unavailable-workspace") from error
            self.check()
            if _identity(os.stat(".")) != self.chain[-1]:
                raise StagingError("Server directory changed before execution", "conflict")
            yield
        finally:
            os.fchdir(previous)
            os.close(previous)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def select(path, *, state_root=None, protected=()):
    """Return server-generated metadata for a new immutable host-user plan."""
    directory = WorkspaceDirectory(path)
    try:
        return {"path": str(directory.path), "label": directory.path.name or "/",
                "source": "server-directory", "pin": directory.pin}
    finally:
        directory.close()


def immediate_directories(path, *, limit=128):
    """List searchable direct children of an admitted host directory.

    The list is only a picker snapshot. Each chosen path is admitted and pinned
    again when its launch plan is created, so a renamed child cannot redirect a
    launch to a replacement object.
    """
    parent = WorkspaceDirectory(path)
    descriptor = -1
    try:
        descriptor = os.open(parent.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if _identity(os.fstat(descriptor)) != parent.chain[-1]:
            raise StagingError("Server directory changed while listing", "conflict")
        children = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name in {".", ".."} or "/" in entry.name or "\x00" in entry.name:
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    child = WorkspaceDirectory(str(parent.path / entry.name))
                    try:
                        if child.chain[:-1] != parent.chain or child.chain[-1] != _identity(info):
                            continue
                        children.append({"name": entry.name, "path": str(child.path)})
                    finally:
                        child.close()
                except (OSError, StagingError):
                    # A child may be private or change during the listing.
                    continue
                if len(children) > limit:
                    raise StagingError("Too many immediate directories; choose a narrower parent", "capacity")
        parent.check()
        return sorted(children, key=lambda item: (item["name"].casefold(), item["name"]))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        parent.close()


@contextmanager
def open_workspace(workspace, *, state_root=None, protected=(), inspect=True):
    if "pin" not in workspace:
        raise StagingError("Server directory receipt requires a new host-user launch", "conflict")
    directory = WorkspaceDirectory(workspace["path"], pin=workspace["pin"])
    try:
        yield directory
    finally:
        directory.close()
