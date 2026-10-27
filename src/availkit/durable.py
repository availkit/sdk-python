"""Explicit, local Linux persistence. This module never calls the API.

Private originals may contain PII and public capabilities. Do not log exports.
See the SDK README for crash recovery and filesystem trust boundaries.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, ParamSpec, TypeVar
from functools import wraps

from .intents import RequestIntent
from ._validation import instant

State = Literal["prepared", "unknown", "confirmed", "rejected"]
MAX_RECORDS = 128
MAX_BYTES = 65536
MAX_REVISION = 255
MAX_ATTEMPTS = 64
_OPS = {"book", "cancel", "rescheduleBooking", "bookPublic", "cancelPublicBooking", "reschedulePublicBooking"}
_CODES = {"invalid_input", "invalid_storage", "storage_failed", "conflict", "not_found", "busy", "capacity", "invalid_record", "claim_active", "invalid_transition", "intent_window", "repair_required"}


class OperationStoreError(Exception):
    """Finite, credential-free diagnostics; raw causes are deliberately discarded."""
    def __init__(self, code: str):
        self.code = code if code in _CODES else "storage_failed"
        super().__init__("AvailKit operation store error (" + self.code + ")")


def _fail(code: str) -> None:
    raise OperationStoreError(code) from None



_P = ParamSpec("_P")
_T = TypeVar("_T")


def _safe(function: Callable[_P, _T]) -> Callable[_P, _T]:
    # Raise outside the except block: even __context__ must not retain raw
    # filesystem/SQLite exceptions with private paths, SQL or caller data.
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        code = "storage_failed"
        try:
            return function(*args, **kwargs)
        except OperationStoreError as error:
            code = error.code
        except Exception:
            pass
        raise OperationStoreError(code) from None
    return wrapped


def _identifier(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        _fail("invalid_input")
    return value


def _original(intent: RequestIntent) -> bytes:
    try:
        if not isinstance(intent, RequestIntent) or intent.operation not in _OPS:
            _fail("invalid_input")
        if len(intent.path) > 1024 or len(intent.scope) > 4096:
            _fail("invalid_input")
        if intent.operation in {"book", "cancel", "rescheduleBooking"} and intent.headers:
            _fail("invalid_input")
        if len(intent.headers) > 2 or any(len(k) > 64 or len(v) > 128 for k, v in intent.headers):
            _fail("invalid_input")
        data = intent.export_private()
        if len(data) > MAX_BYTES - 1024:
            _fail("invalid_input")
        return data
    except OperationStoreError:
        raise
    except Exception:
        _fail("invalid_input")


def _binding(intent: RequestIntent) -> str:
    return hashlib.sha256(_original(intent)).hexdigest()


@dataclass(frozen=True, repr=False)
class OperationRecord:
    """Safe repr; private material is available only through explicit methods."""
    revision: int
    state: State
    attempts: int
    _data: bytes

    @property
    def active(self) -> bool:
        return json.loads(self._data)["claim"] is not None

    def original(self) -> RequestIntent:
        return RequestIntent.from_private(json.dumps(json.loads(self._data)["intent"], separators=(",", ":")).encode())

    def attempt_token(self) -> str | None:
        """Private local CAS token, not proof that a worker is alive/dead."""
        return json.loads(self._data)["claim"]

    def __repr__(self) -> str:
        return f"<AvailKit OperationRecord state={self.state} revision={self.revision} active={self.active}>"


def _record(data: bytes) -> OperationRecord:
    try:
        if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
            _fail("invalid_record")
        v = json.loads(data)
        if (type(v) is not dict or set(v) != {"version", "intent", "binding", "revision", "state", "claim", "attempts"}
                or type(v["version"]) is not int or v["version"] != 1
                or type(v["revision"]) is not int or not 0 <= v["revision"] <= MAX_REVISION
                or type(v["attempts"]) is not int or not 0 <= v["attempts"] <= MAX_ATTEMPTS
                or v["state"] not in ("prepared", "unknown", "confirmed", "rejected")
                or v["claim"] is not None and (type(v["claim"]) is not str or re.fullmatch(r"[0-9a-f]{64}", v["claim"]) is None)):
            _fail("invalid_record")
        original = RequestIntent.from_private(json.dumps(v["intent"], separators=(",", ":")).encode())
        if v["binding"] != _binding(original) or v["claim"] is not None and v["state"] != "unknown":
            _fail("invalid_record")
        if (v["state"] == "prepared") != (v["attempts"] == 0) or v["revision"] < v["attempts"]:
            _fail("invalid_record")
        return OperationRecord(v["revision"], v["state"], v["attempts"], bytes(data))
    except OperationStoreError:
        raise
    except Exception:
        _fail("invalid_record")


def _encode(v: dict) -> bytes:
    data = json.dumps(v, separators=(",", ":")).encode()
    _record(data)
    return data


class _Store:
    def _transaction(self, identifier: str, change: Callable[[bytes | None], bytes | None]) -> bytes | None:
        raise NotImplementedError

    @_safe
    def create(self, identifier: str, intent: RequestIntent) -> OperationRecord:
        """Persist before sending. Identical create returns the existing record."""
        _identifier(identifier)
        original = _original(intent)
        binding = _binding(intent)
        def create(old: bytes | None) -> bytes:
            if old is not None:
                if _binding(_record(old).original()) != binding:
                    _fail("conflict")
                return old
            return _encode(dict(version=1, intent=json.loads(original), binding=binding,
                revision=0, state="prepared", claim=None, attempts=0))
        data = self._transaction(identifier, create)
        assert data is not None
        return _record(data)

    @_safe
    def read(self, identifier: str) -> OperationRecord | None:
        data = self._transaction(_identifier(identifier), lambda old: old)
        return None if data is None else _record(data)

    def _change(self, identifier: str, revision: int, change: Callable[[dict], None]) -> OperationRecord:
        _identifier(identifier)
        if type(revision) is not int or not 0 <= revision < MAX_REVISION:
            _fail("invalid_input")
        def update(old: bytes | None) -> bytes:
            if old is None:
                _fail("not_found")
            row = _record(old)
            if row.revision != revision:
                _fail("conflict")
            v = json.loads(old)
            change(v)
            v["revision"] += 1
            return _encode(v)
        data = self._transaction(identifier, update)
        assert data is not None
        return _record(data)

    @_safe
    def begin(self, identifier: str, revision: int, original: RequestIntent, *, replay: bool = False) -> OperationRecord:
        """Commit unknown before one explicit caller send. Never sends/retries."""
        if type(revision) is not int or revision >= MAX_REVISION - 1:
            _fail("capacity")
        binding = _binding(original)
        if type(replay) is not bool:
            _fail("invalid_input")
        def begin(v: dict) -> None:
            if v["binding"] != binding:
                _fail("conflict")
            if v["claim"] is not None:
                _fail("claim_active")
            if v["state"] != ("unknown" if replay else "prepared"):
                _fail("invalid_transition")
            age = datetime.now(timezone.utc) - instant(original.created_at)
            if age < timedelta(0) or age >= timedelta(hours=24):
                _fail("intent_window")
            if v["attempts"] >= MAX_ATTEMPTS:
                _fail("capacity")
            v.update(state="unknown", claim=secrets.token_hex(32), attempts=v["attempts"] + 1)
        return self._change(identifier, revision, begin)

    @_safe
    def settle(self, identifier: str, revision: int, token: str, state: Literal["confirmed", "rejected", "unknown"]) -> OperationRecord:
        """Call only after the SDK's validated result or explicit error outcome."""
        if state not in ("confirmed", "rejected", "unknown") or type(token) is not str or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            _fail("invalid_input")
        def settle(v: dict) -> None:
            if v["state"] != "unknown" or v["claim"] != token:
                _fail("conflict")
            # Later rejection cannot establish what an earlier unknown send did.
            v.update(state="unknown" if state == "rejected" and v["attempts"] > 1 else state, claim=None)
        return self._change(identifier, revision, settle)

    @_safe
    def release_interrupted(self, identifier: str, revision: int, *, acknowledge_workers_joined: bool) -> OperationRecord:
        """Owner assertion only. Does not prove death, outcome or replay safety."""
        if acknowledge_workers_joined is not True:
            _fail("invalid_input")
        def release(v: dict) -> None:
            if v["state"] != "unknown" or v["claim"] is None:
                _fail("invalid_transition")
            v["claim"] = None
        return self._change(identifier, revision, release)


class _Directory:
    def __init__(self, path: str | os.PathLike[str]):
        self.fd = -1
        try:
            if sys.platform != "linux":
                _fail("invalid_storage")
            value = os.path.abspath(os.fspath(path))
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in Path(value).parts[1:]:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = nxt
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    _fail("invalid_storage")
                self.fd, fd = fd, -1
            finally:
                if fd >= 0:
                    os.close(fd)
        except OperationStoreError:
            raise
        except Exception:
            _fail("invalid_storage")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def read(self, leaf: str, maximum: int = MAX_BYTES) -> bytes | None:
        fd = -1
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_nlink != 1 or not 0 <= info.st_size <= maximum):
                _fail("invalid_storage")
            result = bytearray()
            while len(result) <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > maximum:
                _fail("invalid_storage")
            return bytes(result)
        except FileNotFoundError:
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    def write(self, leaf: str, data: bytes) -> None:
        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    _fail("storage_failed")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)


class FileOperationStore(_Store):
    """Bounded Linux local filesystem store. No automatic crash-lock reclaim."""
    @_safe
    def __init__(self, directory: str | os.PathLike[str]):
        self._directory = _Directory(directory)

    def __repr__(self) -> str:
        return "<AvailKit FileOperationStore (private)>"

    @_safe
    def close(self) -> None:
        self._directory.close()

    def _transaction(self, identifier: str, change: Callable[[bytes | None], bytes | None]) -> bytes | None:
        d = self._directory
        locked = False
        dirty = False
        try:
            try:
                os.mkdir(".lock", 0o700, dir_fd=d.fd)
            except FileExistsError:
                _fail("busy")
            locked = True
            os.fsync(d.fd)
            old = d.read(identifier + ".json")
            if old is not None:
                _record(old)
            new = change(old)
            if new != old:
                if old is None and len(os.listdir(d.fd)) - 1 >= MAX_RECORDS:
                    _fail("capacity")
                assert new is not None
                dirty = True
                d.write(".pending", new)
                os.rename(".pending", identifier + ".json", src_dir_fd=d.fd, dst_dir_fd=d.fd)
                os.fsync(d.fd)
                dirty = False
            return new
        except OperationStoreError:
            raise
        except Exception:
            _fail("storage_failed")
        finally:
            # An incomplete write leaves the lock AND snapshot for inspection.
            if locked and not dirty:
                try:
                    os.rmdir(".lock", dir_fd=d.fd)
                    os.fsync(d.fd)
                except OSError:
                    pass

    @_safe
    def recover_lock(self, *, acknowledge_workers_joined: bool) -> None:
        """Require all writers/starters/repairers quiescent until removal returns."""
        if acknowledge_workers_joined is not True:
            _fail("invalid_input")
        try:
            # Never inspect/delete a possibly incomplete private snapshot here.
            try:
                os.stat(".pending", dir_fd=self._directory.fd, follow_symlinks=False)
                _fail("repair_required")
            except FileNotFoundError:
                pass
            info = os.stat(".lock", dir_fd=self._directory.fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                _fail("invalid_storage")
            os.rmdir(".lock", dir_fd=self._directory.fd)
            os.fsync(self._directory.fd)
        except OperationStoreError:
            raise
        except Exception:
            _fail("storage_failed")


class SQLiteOperationStore(_Store):
    """Local stdlib SQLite adapter; never use an untrusted/shared directory."""
    @_safe
    def __init__(self, directory: str | os.PathLike[str]):
        import sqlite3
        self._directory = _Directory(directory)
        self._db = None
        try:
            d = self._directory
            try:
                d.write("operations.sqlite", b"")
                os.fsync(d.fd)
            except FileExistsError:
                pass
            d.read("operations.sqlite", 16 * 1024 * 1024)
            for suffix in ("-journal", "-wal", "-shm"):
                d.read("operations.sqlite" + suffix, 16 * 1024 * 1024)
            self._db = sqlite3.connect(f"/proc/self/fd/{d.fd}/operations.sqlite", timeout=1, isolation_level=None)
            app = self._db.execute("PRAGMA application_id").fetchone()[0]
            if app not in (0, 1096174385) or app == 0 and self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] != 0:
                _fail("invalid_storage")
            if self._db.execute("PRAGMA page_size").fetchone()[0] != 4096:
                _fail("invalid_storage")
            self._db.execute("PRAGMA busy_timeout=1000")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA max_page_count=4096")
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("BEGIN IMMEDIATE")
            app = self._db.execute("PRAGMA application_id").fetchone()[0]
            if app == 0:
                if self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] != 0:
                    _fail("invalid_storage")
                self._db.execute("CREATE TABLE operations (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data BLOB NOT NULL CHECK(length(data)<=65536))")
                self._db.execute("PRAGMA application_id=1096174385")
                self._db.execute("PRAGMA user_version=1")
            elif app != 1096174385 or self._db.execute("PRAGMA user_version").fetchone()[0] != 1:
                _fail("invalid_storage")
            self._db.execute("COMMIT")
            d.read("operations.sqlite", 16 * 1024 * 1024)
            os.fsync(d.fd)
        except Exception:
            self.close()
            _fail("invalid_storage")

    def __repr__(self) -> str:
        return "<AvailKit SQLiteOperationStore (private)>"

    @_safe
    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._directory.close()

    def _transaction(self, identifier: str, change: Callable[[bytes | None], bytes | None]) -> bytes | None:
        import sqlite3
        db = self._db
        if db is None:
            _fail("invalid_storage")
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,data FROM operations WHERE id=?", (identifier,)).fetchone()
            old = bytes(row[1]) if row else None
            if old is not None and _record(old).revision != row[0]:
                _fail("invalid_record")
            new = change(old)
            if new != old:
                assert new is not None
                record = _record(new)
                if old is None:
                    if db.execute("SELECT count(*) FROM operations").fetchone()[0] >= MAX_RECORDS:
                        _fail("capacity")
                    db.execute("INSERT INTO operations(id,revision,data) VALUES(?,?,?)", (identifier, record.revision, new))
                elif db.execute("UPDATE operations SET revision=?,data=? WHERE id=? AND revision=?", (record.revision, new, identifier, row[0])).rowcount != 1:
                    _fail("conflict")
            db.execute("COMMIT")
            return new
        except OperationStoreError:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            code = "busy" if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "storage_failed"
            if db.in_transaction:
                db.execute("ROLLBACK")
            _fail(code)
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            _fail("storage_failed")
