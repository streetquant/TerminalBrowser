"""Bounded host copies of explicitly selected client files.

Only the authenticated web bridge supplies Actor. No client input selects a
server destination. Operations use the enrolled-machine database transaction
as the revocation fence, plus descriptor-relative storage and publication.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata
import uuid

MAX_FILES = 256
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_NAMESPACE_BYTES = 512 * 1024 * 1024
MAX_CHUNK_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 384 * 1024
MAX_OBJECTS = 4096
UPLOAD_TTL_MS = 24 * 60 * 60 * 1000


class StagingError(ValueError):
    def __init__(self, message, code="invalid-request"):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _id(value, prefix, length=32):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + "[0-9a-f]{" + str(length) + "}", value):
        raise StagingError("Invalid identity")
    return value


def _text(value, maximum=1024, *, empty=False):
    if not isinstance(value, str) or len(value.encode()) > maximum or (not empty and not value) or any(unicodedata.category(c).startswith("C") for c in value):
        raise StagingError("Invalid display metadata")
    return value


def _relative(value):
    value = _text(value)
    parts = value.split("/")
    if len(parts) > 16 or any(not p or p in {".", ".."} or p.startswith(".") or len(p.encode()) > 200
                             or any(c in p for c in "\\:%") or p.endswith((" ", ".")) for p in parts):
        raise StagingError("File names must be relative, normalized paths")
    if any(unicodedata.normalize("NFC", p) != p for p in parts):
        raise StagingError("Normalize file names to NFC before upload")
    return value


def _check_file(info, device, *, immutable=False):
    modes = {0o400} if immutable else {0o400, 0o600}
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_dev != device
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) not in modes):
        raise StagingError("Unsafe staged file", "unsafe-storage")


class Directory:
    """Pinned directory tree; every descendant is opened without following links."""
    def __init__(self, path, *, create=False):
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise StagingError("Storage root must be absolute", "unsafe-storage")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open("/", flags)
        try:
            root = os.fstat(current)
            self._check_ancestor(root)
            chain = [self._receipt(root)]
            for index, part in enumerate(self.path.parts[1:]):
                if create and index == len(self.path.parts) - 2:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
                nxt = os.open(part, flags, dir_fd=current)
                try:
                    opened = os.fstat(nxt)
                    after = os.stat(part, dir_fd=current, follow_symlinks=False)
                    if self._receipt(before) != self._receipt(opened) or self._receipt(after) != self._receipt(opened):
                        raise StagingError("Storage ancestor changed while opening", "unsafe-storage")
                    self._check_ancestor(opened)
                    chain.append(self._receipt(opened))
                except BaseException:
                    os.close(nxt)
                    raise
                os.close(current)
                current = nxt
            self.fd = current
            self.identity = os.fstat(self.fd)
            self._check_dir(self.identity)
            self.chain = tuple(chain)
            current = -1
        finally:
            if current >= 0:
                os.close(current)

    @staticmethod
    def _receipt(info):
        return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode)

    @staticmethod
    def _check_ancestor(info):
        mode = stat.S_IMODE(info.st_mode)
        # Root-owned sticky temporary parents (for example /tmp) protect each
        # entry from other users. Their children still need trusted ownership
        # and non-writable ancestry; the selected private root must be 0700.
        sticky_temporary = info.st_uid == 0 and mode == 0o1777
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or (mode & 0o022 and not sticky_temporary)):
            raise StagingError("Storage ancestors must have trusted ownership and permissions", "unsafe-storage")

    def _check_dir(self, info):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StagingError("Storage directories must be private and owned", "unsafe-storage")
        if hasattr(self, "identity") and info.st_dev != self.identity.st_dev:
            raise StagingError("Storage device changed", "unsafe-storage")

    def check(self):
        other = Directory(self.path)
        try:
            if other.chain != self.chain:
                raise StagingError("Storage directory ancestry changed", "unsafe-storage")
        finally:
            other.close()

    @contextmanager
    def directory(self, relative="", *, create=False):
        descriptor = os.dup(self.fd)
        try:
            for part in relative.split("/") if relative else []:
                if part in {"", ".", ".."} or "/" in part or "\\" in part:
                    raise StagingError("Invalid storage component")
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                self._check_dir(os.fstat(descriptor))
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def file(self, relative, *, create=False, write=False, immutable=False):
        parent, _, leaf = relative.rpartition("/")
        with self.directory(parent, create=create) as directory:
            flags = (os.O_RDWR if write or create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open(leaf, flags, 0o600, dir_fd=directory)
            try:
                info = os.fstat(fd)
                _check_file(info, self.identity.st_dev, immutable=immutable)
                if create:
                    os.fsync(directory)
                yield fd, info
                after = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                _check_file(after, self.identity.st_dev, immutable=immutable)
                if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                    raise StagingError("Staged file was replaced", "unsafe-storage")
            finally:
                os.close(fd)

    def close(self):
        os.close(self.fd)


@contextmanager
def _database(directory, filename, *, create=False, readonly=False):
    if create:
        try:
            with directory.file(filename, create=True) as (fd, _info):
                os.fsync(fd)
        except FileExistsError:
            pass
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            with directory.file(filename + suffix):
                pass
        except FileNotFoundError:
            pass
    # SQLite opens a stable descriptor-relative pathname. The containing FD
    # stays live throughout this process; it is never used as an exec address.
    with directory.file(filename) as (_fd, before):
        connection = sqlite3.connect(f"file:/proc/self/fd/{directory.fd}/{filename}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if create:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA max_page_count=32768")
            after = os.stat(filename, dir_fd=directory.fd, follow_symlinks=False)
            if after.st_ino != before.st_ino:
                raise StagingError("Database identity changed", "unsafe-storage")
            yield connection
        finally:
            connection.close()


@dataclass(frozen=True)
class Actor:
    account_id: str
    machine_id: str
    session_id: str
    credential_hash: str
    credential_generation: int

    def validate(self):
        _id(self.account_id, "acc_", 64)
        _id(self.machine_id, "mach_")
        _id(self.session_id, "ses_")
        _id(self.credential_hash, "", 64)
        if type(self.credential_generation) is not int or self.credential_generation < 1:
            raise StagingError("Invalid machine generation", "unauthorized")


@dataclass
class _AuthorizationFence:
    directory: Directory
    actor: Actor
    machine: dict
    now: int
    active: bool = True


_AUTHORIZATION_FENCE = ContextVar("termdeck_staging_authorization", default=None)


@contextmanager
def authorized(auth_directory, actor, *, now_ms=None, readonly=False):
    """Revalidate both credentials; mutations retain revocation serialization.

    Read-only launch admission can run inside an existing serialized operation.
    It does not replace the final execution or mutation authorization fence.
    """
    actor.validate()
    current = _AUTHORIZATION_FENCE.get()
    if readonly and current is not None and current.active and Path(auth_directory) == current.directory.path:
        if actor != current.actor:
            raise StagingError("Nested staging authorization does not match", "unauthorized")
        # POSIX record locks are released when this process closes *any* fd
        # for the SQLite file. Do not reopen it inside the serialized fence.
        current.directory.check()
        yield dict(current.machine), current.now
        current.directory.check()
        return
    directory = Directory(auth_directory)
    try:
        with _database(directory, "termdeck-web-auth-v1.sqlite3", readonly=True) as auth, _database(directory, "termdeck-machines-v1.sqlite3", readonly=readonly) as machines:
            machines.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            auth.execute("BEGIN")
            metadata = auth.execute("SELECT host_id,owner_id,last_observed_at_ms FROM auth_metadata WHERE singleton=1").fetchone()
            now = max(int(time.time() * 1000) if now_ms is None else now_ms, metadata["last_observed_at_ms"])
            expected = "acc_" + hashlib.sha256(b"termdeck/account-namespace/v1\0" + metadata["host_id"].encode() + b"\0" + metadata["owner_id"].encode()).hexdigest()
            owner = machines.execute("SELECT host,owner,version FROM identity WHERE singleton=1").fetchone()
            session = auth.execute("SELECT 1 FROM auth_sessions s JOIN devices d ON s.device_id=d.device_id WHERE s.session_id=? AND s.revoked_at_ms IS NULL AND d.revoked_at_ms IS NULL AND s.expires_at_ms>?", (actor.session_id, now)).fetchone()
            machine = machines.execute("SELECT * FROM machines WHERE machine_id=? AND credential_hash=? AND generation=? AND revoked_at_ms IS NULL", (actor.machine_id, bytes.fromhex(actor.credential_hash), actor.credential_generation)).fetchone()
            if (expected != actor.account_id or not session or not machine or not owner
                    or tuple(owner) != (metadata["host_id"], metadata["owner_id"], 1)):
                raise StagingError("Staging authorization failed", "unauthorized")
            fence = _AuthorizationFence(directory, actor, dict(machine), now)
            token = _AUTHORIZATION_FENCE.set(fence) if not readonly else None
            try:
                yield dict(machine), now
            finally:
                fence.active = False
                if token is not None:
                    _AUTHORIZATION_FENCE.reset(token)
            directory.check()
            machines.commit()
    finally:
        directory.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(
 object_id TEXT NOT NULL, generation INTEGER NOT NULL, account_id TEXT NOT NULL, machine_id TEXT NOT NULL,
 uploader_session TEXT NOT NULL, display_path TEXT NOT NULL, label TEXT NOT NULL, created_ms INTEGER NOT NULL,
 updated_ms INTEGER NOT NULL, status TEXT NOT NULL, files TEXT NOT NULL, size INTEGER NOT NULL,
 digest TEXT, previous_generation INTEGER, PRIMARY KEY(object_id,generation));
CREATE TABLE IF NOT EXISTS requests(account_id TEXT NOT NULL,machine_id TEXT NOT NULL,session_id TEXT NOT NULL,
 request_id TEXT NOT NULL,body_digest TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(account_id,machine_id,session_id,request_id));
CREATE TABLE IF NOT EXISTS attachments(attachment_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,machine_id TEXT NOT NULL,
 object_id TEXT NOT NULL,generation INTEGER NOT NULL,digest TEXT NOT NULL,target TEXT NOT NULL,created_ms INTEGER NOT NULL);
"""


class StagingStore:
    def __init__(self, state_root, auth_directory, *, session_database=None, create=True):
        self.root = Path(state_root) / "client-files"
        self.auth_directory = Path(auth_directory)
        self.session_database = Path(session_database or Path(state_root) / "sessions.sqlite3")
        # Do not create trees via mkdir(parents=True), which follows links.
        parent = Directory(state_root)
        try:
            with parent.directory("client-files", create=create) as descriptor:
                self._root_chain = parent.chain + (Directory._receipt(os.fstat(descriptor)),)
            parent.check()
        finally:
            parent.close()

    def handle(self, actor, request, *, now_ms=None, internal_resolve=False):
        if not isinstance(request, dict) or len(_json(request).encode()) > MAX_REQUEST_BYTES:
            raise StagingError("Invalid or oversized staging request")
        allowed = {
            "list": {"offset"}, "inspect": {"reference"},
            "initiate": {"request_id", "files", "display_path", "label", "previous"},
            "chunk": {"reference", "file_index", "offset", "data"},
            "finalize": {"request_id", "reference"}, "remove": {"request_id", "reference"},
            "attach": {"request_id", "reference", "target"}, "resolve": {"attachment_id"},
            "detach": {"request_id", "reference", "attachment_id"},
            "download": {"reference", "file_index", "offset"},
            "cleanup": {"request_id"},
            "launch-options": set(),
            "create-window": {"request_id", "policy_revision", "name", "workspace_id", "workspace_path", "runtime", "tabs", "task", "attachments"},
        }
        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in allowed or set(request) - allowed[operation] - {"operation"}:
            raise StagingError("Unknown staging operation or field")
        if operation == "resolve" and not internal_resolve:
            raise StagingError("Paths are available only to an authorized internal launch", "unauthorized")
        with authorized(self.auth_directory, actor, now_ms=now_ms) as (machine, now):
            tree = Directory(self.root)
            try:
                if tree.chain != self._root_chain:
                    raise StagingError("Staging root ancestry changed", "unsafe-storage")
                with _database(tree, "metadata.sqlite3", create=True) as db:
                    db.executescript(SCHEMA)
                    db.execute("BEGIN IMMEDIATE")
                    if operation in {"initiate", "finalize", "remove", "attach", "detach", "cleanup", "create-window"}:
                        request_id = _id(request.get("request_id"), "")
                        key = (actor.account_id, actor.machine_id, actor.session_id, request_id)
                        digest = hashlib.sha256(_json(request).encode()).hexdigest()
                        prior = db.execute("SELECT body_digest,response FROM requests WHERE account_id=? AND machine_id=? AND session_id=? AND request_id=?", key).fetchone()
                        if prior:
                            if prior["body_digest"] != digest:
                                raise StagingError("Request ID was already used for different content", "conflict")
                            result = json.loads(prior["response"])
                            if "object" in result:
                                row = self._row(db, actor, result["object"]["reference"], digest=False)
                                if row["status"] == "allocating":
                                    row = self._allocate(db, tree, row, now)
                                result["object"] = self._public(row, machine)
                            if "attachment_id" in result and not db.execute("SELECT 1 FROM attachments WHERE attachment_id=?", (result["attachment_id"],)).fetchone():
                                raise StagingError("Replayed attachment has been detached", "stale-reference")
                            result["replayed"] = True
                            tree.check()
                            db.commit()
                            return result
                        if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= 65536:
                            raise StagingError("Staging receipt capacity reached", "capacity")
                    result = self._operate(db, tree, actor, request, machine, now)
                    tree.check()
                    if operation in {"initiate", "finalize", "remove", "attach", "detach", "cleanup", "create-window"}:
                        self._receipt(db, actor, request, result)
                    db.commit()
                    return result
            finally:
                tree.close()

    @staticmethod
    def _receipt(db, actor, request, result):
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,machine_id,session_id,request_id) DO UPDATE SET response=excluded.response",
                   (actor.account_id, actor.machine_id, actor.session_id, request["request_id"],
                    hashlib.sha256(_json(request).encode()).hexdigest(), _json(result)))

    @staticmethod
    def _row(db, actor, reference, *, digest=True):
        if not isinstance(reference, dict) or set(reference) != {"account_id", "machine_id", "object_id", "generation", "digest", "size"}:
            raise StagingError("Invalid staged reference")
        if reference["account_id"] != actor.account_id or reference["machine_id"] != actor.machine_id:
            raise StagingError("Staged object is unavailable", "unauthorized")
        _id(reference["object_id"], "obj_")
        if type(reference["generation"]) is not int or not 1 <= reference["generation"] <= MAX_OBJECTS:
            raise StagingError("Invalid generation")
        row = db.execute("SELECT * FROM objects WHERE object_id=? AND generation=? AND account_id=? AND machine_id=?", (reference["object_id"], reference["generation"], actor.account_id, actor.machine_id)).fetchone()
        if not row:
            raise StagingError("Staged object is unavailable", "unauthorized")
        if reference["size"] != row["size"] or (digest and reference["digest"] != row["digest"]):
            raise StagingError("Staged reference no longer matches its content", "stale-reference")
        return row

    @staticmethod
    def _reference(row):
        return {field: row[field] for field in ("account_id", "machine_id", "object_id", "generation", "digest", "size")}

    @staticmethod
    def _location(row):
        return f"{row['account_id']}/{row['machine_id']}/objects/{row['object_id']}/g{row['generation']}/content"

    def _public(self, row, machine):
        files = json.loads(row["files"])
        return {"reference": self._reference(row), "display_path": row["display_path"], "label": row["label"],
                "machine_label": machine["label"], "uploader_session": row["uploader_session"],
                "status": row["status"], "created_ms": row["created_ms"], "updated_ms": row["updated_ms"],
                "files": [{k: v for k, v in f.items() if k not in {"inode", "device"}} for f in files]}

    def _operate(self, db, tree, actor, request, machine, now):
        op = request["operation"]
        if op == "launch-options":
            from .agent_launch import options
            return options(self.root.parent)
        if op == "create-window":
            from .agent_launch import prepare
            return prepare(self, db, tree, actor, request, machine, now)
        if op == "list":
            offset = request.get("offset", 0)
            if type(offset) is not int or not 0 <= offset <= MAX_OBJECTS:
                raise StagingError("Invalid list offset")
            rows = db.execute("SELECT * FROM objects WHERE account_id=? AND machine_id=? ORDER BY created_ms DESC,object_id,generation LIMIT 51 OFFSET ?", (actor.account_id, actor.machine_id, offset)).fetchall()
            objects = []
            for row in rows[:50]:
                obj = self._public(row, machine)
                obj["file_count"] = len(obj.pop("files"))
                objects.append(obj)
            return {"ok": True, "objects": objects, "next_offset": offset + 50 if len(rows) > 50 else None}
        if op == "initiate":
            return self._initiate(db, tree, actor, request, machine, now)
        if op == "resolve":
            attachment = db.execute("SELECT * FROM attachments WHERE attachment_id=? AND account_id=? AND machine_id=?", (_id(request.get("attachment_id"), "att_"), actor.account_id, actor.machine_id)).fetchone()
            if not attachment:
                raise StagingError("Attachment is unavailable", "unauthorized")
            row = db.execute("SELECT * FROM objects WHERE object_id=? AND generation=?", (attachment["object_id"], attachment["generation"])).fetchone()
            if not row or row["digest"] != attachment["digest"] or row["status"] not in {"ready", "changed"}:
                raise StagingError("Attachment content is unavailable", "stale-reference")
            self._verify(tree, row)
            return {"ok": True, "reference": self._reference(row), "server_path": str(self.root / self._location(row)), "target": attachment["target"]}
        if op == "cleanup":
            rows = db.execute("SELECT * FROM objects WHERE account_id=? AND machine_id=? AND ((status IN ('allocating','uploading','finalizing') AND created_ms<?) OR status IN ('removing','expiring'))", (actor.account_id, actor.machine_id, now - UPLOAD_TTL_MS)).fetchall()
            for row in rows:
                self._discard(db, tree, row, now, status="removed" if row["status"] == "removing" else "failed")
            return {"ok": True, "expired_uploads": len(rows)}
        row = self._row(db, actor, request.get("reference"))
        if op == "inspect":
            obj = self._public(row, machine)
            obj["attachments"] = [dict(a) for a in db.execute("SELECT attachment_id,target,created_ms FROM attachments WHERE account_id=? AND machine_id=? AND object_id=? AND generation=?", (actor.account_id, actor.machine_id, row["object_id"], row["generation"]))]
            return {"ok": True, "object": obj}
        if op == "chunk":
            return self._chunk(db, tree, row, request, now)
        if op == "finalize":
            return self._finalize(db, tree, row, machine, now)
        if op == "download":
            if row["status"] not in {"ready", "changed"}:
                raise StagingError("Content is unavailable", "stale-reference")
            files = self._verify(tree, row)
            index, offset = request.get("file_index"), request.get("offset")
            if type(index) is not int or not 0 <= index < len(files) or type(offset) is not int or not 0 <= offset <= files[index]["size"]:
                raise StagingError("Invalid download range")
            with tree.file(self._location(row) + "/" + files[index]["path"], immutable=True) as (fd, info):
                self._identity(files[index], info)
                data = os.pread(fd, MAX_CHUNK_BYTES, offset)
            return {"ok": True, "data": base64.b64encode(data).decode(), "offset": offset, "size": files[index]["size"]}
        if op == "detach":
            attachment_id = _id(request.get("attachment_id"), "att_")
            removed = db.execute("DELETE FROM attachments WHERE attachment_id=? AND account_id=? AND machine_id=? AND object_id=? AND generation=?", (attachment_id, actor.account_id, actor.machine_id, row["object_id"], row["generation"]))
            if removed.rowcount != 1:
                raise StagingError("Attachment is unavailable", "unauthorized")
            return {"ok": True, "detached": attachment_id}
        if op == "remove":
            if db.execute("SELECT 1 FROM attachments WHERE object_id=? AND generation=?", (row["object_id"], row["generation"])).fetchone():
                raise StagingError("Attached content is retained; detach it before removal", "conflict")
            self._discard(db, tree, row, now)
            return {"ok": True, "status": "removed", "reference": self._reference(row)}
        if op == "attach":
            target = _text(request.get("target"), 128)
            if not re.fullmatch(r"(window|tab|task):[a-zA-Z0-9_-]{1,96}", target):
                raise StagingError("Invalid attachment target")
            if row["status"] != "ready":
                raise StagingError("Attach a ready current generation", "stale-reference")
            self._verify(tree, row)
            attachment_id = "att_" + uuid.uuid4().hex
            db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?)", (attachment_id,actor.account_id,actor.machine_id,row["object_id"],row["generation"],row["digest"],target,now))
            return {"ok": True, "attachment_id": attachment_id, "reference": self._reference(row), "target": target}
        raise StagingError("Unsupported staging operation")

    def _initiate(self, db, tree, actor, request, machine, now):
        entries = request.get("files")
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise StagingError("Select between 1 and 256 files")
        files, seen, components, total = [], set(), {}, 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"path", "size", "digest", "media_type", "modified_ms"}:
                raise StagingError("Invalid file metadata")
            name = _relative(entry.get("path"))
            normalized = unicodedata.normalize("NFKC", name).casefold()
            if normalized in seen or any(normalized.startswith(n + "/") or n.startswith(normalized + "/") for n in seen):
                raise StagingError("Colliding file names")
            seen.add(normalized)
            for depth in range(1, len(name.split("/")) + 1):
                prefix = "/".join(name.split("/")[:depth])
                folded = unicodedata.normalize("NFKC", prefix).casefold()
                if folded in components and components[folded] != prefix:
                    raise StagingError("Colliding directory names")
                components[folded] = prefix
            size = entry.get("size")
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                raise StagingError("File size limit exceeded", "capacity")
            total += size
            digest = entry.get("digest")
            if digest is not None:
                _id(digest, "", 64)
            media = _text(entry.get("media_type", "application/octet-stream"), 128)
            modified = entry.get("modified_ms")
            if modified is not None and (type(modified) is not int or not 0 <= modified <= 2**53):
                raise StagingError("Invalid modification metadata")
            files.append({"path": name, "size": size, "declared_digest": digest, "digest": None,
                          "media_type": media, "modified_ms": modified, "written": 0})
        if total > MAX_OBJECT_BYTES:
            raise StagingError("Object size limit exceeded", "capacity")
        count, used = db.execute("SELECT count(*),coalesce(sum(CASE WHEN status IN ('removed','failed') THEN 0 ELSE size END),0) FROM objects WHERE account_id=? AND machine_id=?", (actor.account_id, actor.machine_id)).fetchone()
        if count >= MAX_OBJECTS or used + total > MAX_NAMESPACE_BYTES:
            raise StagingError("Machine staging quota reached", "capacity")
        generation, previous, object_id = 1, None, "obj_" + uuid.uuid4().hex
        if request.get("previous") is not None:
            prior = self._row(db, actor, request["previous"])
            if prior["status"] != "ready" or db.execute("SELECT max(generation) FROM objects WHERE object_id=? AND status NOT IN ('failed','removed')", (prior["object_id"],)).fetchone()[0] != prior["generation"]:
                raise StagingError("Refresh requires the latest ready reference", "stale-reference")
            object_id, previous = prior["object_id"], prior["generation"]
            generation = db.execute("SELECT max(generation)+1 FROM objects WHERE object_id=?", (object_id,)).fetchone()[0]
        row = dict(object_id=object_id, generation=generation, account_id=actor.account_id,machine_id=actor.machine_id,
                   uploader_session=actor.session_id, display_path=_text(request.get("display_path", "Selected client files"), 4096),
                   label=_text(request.get("label", "Client files"), 256), created_ms=now, updated_ms=now, status="allocating",
                   files=files,size=total,digest=None,previous_generation=previous)
        row["files"] = _json(files)
        db.execute("INSERT INTO objects VALUES(:object_id,:generation,:account_id,:machine_id,:uploader_session,:display_path,:label,:created_ms,:updated_ms,:status,:files,:size,:digest,:previous_generation)", row)
        self._receipt(db, actor, request, {"ok": True, "object": self._public(row, machine)})
        db.commit()  # Durable allocation intent and request before filesystem work.
        row = self._allocate(db, tree, row, now)
        return {"ok": True, "object": self._public(row, machine)}

    def _allocate(self, db, tree, row, now):
        files = json.loads(row["files"])
        for file in files:
            if "inode" in file:
                with tree.file(self._location(row) + "/" + file["path"]) as (_fd, info):
                    self._identity(file, info)
                continue
            # Crash between creation and identity receipt leaves an empty,
            # quarantined leaf. Never adopt or delete a substituted leaf.
            with tree.file(self._location(row) + "/" + file["path"], create=True) as (fd, info):
                file.update(inode=info.st_ino, device=info.st_dev)
                os.fsync(fd)
            db.execute("UPDATE objects SET files=? WHERE object_id=? AND generation=?", (_json(files), row["object_id"], row["generation"]))
            db.commit()
        db.execute("UPDATE objects SET status='uploading',updated_ms=? WHERE object_id=? AND generation=?", (now, row["object_id"], row["generation"]))
        return db.execute("SELECT * FROM objects WHERE object_id=? AND generation=?", (row["object_id"], row["generation"])).fetchone()

    def _chunk(self, db, tree, row, request, now):
        if row["status"] != "uploading" or now - row["created_ms"] > UPLOAD_TTL_MS:
            raise StagingError("Upload is not active", "stale-reference")
        files = json.loads(row["files"])
        index, offset = request.get("file_index"), request.get("offset")
        if type(index) is not int or not 0 <= index < len(files) or type(offset) is not int or offset < 0:
            raise StagingError("Invalid upload offset")
        encoded = request.get("data")
        if not isinstance(encoded, str) or len(encoded) > (MAX_CHUNK_BYTES + 2) // 3 * 4:
            raise StagingError("Chunk limit exceeded", "capacity")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise StagingError("Invalid chunk encoding") from exc
        file = files[index]
        if not data or len(data) > MAX_CHUNK_BYTES or offset + len(data) > file["size"] or offset > file["written"]:
            raise StagingError("Invalid upload range")
        with tree.file(self._location(row) + "/" + file["path"], write=True) as (fd, info):
            self._identity(file, info)
            if offset < file["written"]:
                if offset + len(data) > file["written"] or os.pread(fd, len(data), offset) != data:
                    raise StagingError("Replayed chunk differs", "conflict")
            else:
                # A process death may have persisted bytes before its metadata
                # commit. Rewrite only that unpublished tail of this own file.
                os.ftruncate(fd, offset)
                done = 0
                while done < len(data):
                    written = os.pwrite(fd, data[done:], offset + done)
                    if written <= 0:
                        raise StagingError("Upload write failed", "failed")
                    done += written
                os.fsync(fd)
                file["written"] += len(data)
        db.execute("UPDATE objects SET files=?,updated_ms=? WHERE object_id=? AND generation=?", (_json(files), now, row["object_id"], row["generation"]))
        return {"ok": True, "written": file["written"], "file_index": index}

    @staticmethod
    def _identity(file, info):
        if (file["device"], file["inode"]) != (info.st_dev, info.st_ino):
            raise StagingError("Staged file identity changed", "unsafe-storage")

    def _verify(self, tree, row, *, uploading=False):
        files = json.loads(row["files"])
        for file in files:
            with tree.file(self._location(row) + "/" + file["path"], immutable=not uploading) as (fd, info):
                self._identity(file, info)
                if info.st_size != file["size"] or file["written"] != file["size"]:
                    raise StagingError("Upload is incomplete or changed", "stale-reference")
                with os.fdopen(os.dup(fd), "rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                expected = file["declared_digest"] if uploading else file["digest"]
                if expected is not None and digest != expected:
                    raise StagingError("Content digest mismatch", "stale-reference")
                file["digest"] = digest
        return files

    def _finalize(self, db, tree, row, machine, now):
        if row["status"] not in {"uploading", "finalizing"} or now - row["created_ms"] > UPLOAD_TTL_MS:
            raise StagingError("Upload is not active", "stale-reference")
        files = self._verify(tree, row, uploading=True)
        if row["previous_generation"] is not None:
            prior = db.execute("SELECT status FROM objects WHERE object_id=? AND generation=?", (row["object_id"], row["previous_generation"])).fetchone()
            if not prior or prior[0] != "ready":
                raise StagingError("Previous generation changed during refresh", "conflict")
        digest = hashlib.sha256(_json([{k: file[k] for k in ("path", "size", "digest")} for file in files]).encode()).hexdigest()
        db.execute("UPDATE objects SET status='finalizing' WHERE object_id=? AND generation=?", (row["object_id"], row["generation"]))
        db.commit()
        for file in files:
            with tree.file(self._location(row) + "/" + file["path"]) as (fd, info):
                self._identity(file, info)
                os.fchmod(fd, 0o400)
                os.fsync(fd)
        candidate = dict(row, files=_json(files))
        self._verify(tree, candidate)
        db.execute("UPDATE objects SET status='ready',digest=?,files=?,updated_ms=? WHERE object_id=? AND generation=?", (digest,_json(files),now,row["object_id"],row["generation"]))
        if row["previous_generation"] is not None:
            db.execute("UPDATE objects SET status='changed',updated_ms=? WHERE object_id=? AND generation=?", (now,row["object_id"],row["previous_generation"]))
        updated = db.execute("SELECT * FROM objects WHERE object_id=? AND generation=?", (row["object_id"],row["generation"])).fetchone()
        return {"ok": True, "object": self._public(updated, machine)}

    def _discard(self, db, tree, row, now, *, status="removed"):
        if row["status"] in {"removed", "failed"}:
            return
        files = json.loads(row["files"])
        db.execute("UPDATE objects SET status=?,updated_ms=? WHERE object_id=? AND generation=?", ("removing" if status == "removed" else "expiring", now, row["object_id"], row["generation"]))
        db.commit()  # Never report partially deleted content as ready after death.
        # Validate every leaf before deleting any known file. Never recursively
        # remove a namespace or follow an unexpected entry during cleanup.
        for file in files:
            try:
                with tree.file(self._location(row) + "/" + file["path"]) as (_fd, info):
                    if "inode" not in file:
                        raise StagingError("Unreceipted allocation is quarantined", "unsafe-storage")
                    self._identity(file, info)
            except FileNotFoundError:
                pass
        for file in files:
            parent, _, leaf = (self._location(row) + "/" + file["path"]).rpartition("/")
            if "inode" not in file:
                continue  # Not created; existing unreceipted files failed above.
            with tree.directory(parent) as fd:
                try:
                    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                    self._identity(file, info)
                    _check_file(info, tree.identity.st_dev)
                    os.unlink(leaf, dir_fd=fd)
                    os.fsync(fd)
                except FileNotFoundError:
                    pass
        db.execute("UPDATE objects SET status=?,updated_ms=? WHERE object_id=? AND generation=?", (status,now,row["object_id"],row["generation"]))


def command(args):
    import sys
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise StagingError("Request too large")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"actor", "request"}:
            raise StagingError("Invalid internal staging request")
        actor = Actor(**envelope["actor"])
        store = StagingStore(Path(args.db).absolute().parent, Path(args.auth_dir).absolute(), session_database=Path(args.db).absolute())
        result = store.handle(actor, envelope["request"])
        if envelope["request"].get("operation") == "create-window":
            from .agent_launch import start
            result = start(store, actor, result)
    except StagingError as exc:
        result = {"ok": False, "error": str(exc), "code": exc.code}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Do not print a credential, client body or internal absolute path.
        result = {"ok": False, "error": "Staging operation failed", "code": "storage-unavailable"}
    print(_json(result))
    return 0
