"""Explicit private retention of replay-capable booking intents."""

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AvailKitError
from ._validation import fail, instant, intent_key


def read_private(path: str | os.PathLike[str], limit: int, code: str) -> bytes:
    data: bytes | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()
                    or not 0 < info.st_size <= limit):
                raise ValueError
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError
        finally:
            os.close(fd)
    except Exception:
        data = None
    if data is None:
        fail(code)
    return data


@dataclass(frozen=True, repr=False)
class RequestIntent:
    """Immutable original booking request; repr deliberately hides its contents.

    The scope includes an irreversible credential fingerprint, never the API key.
    Public intents include private capability tokens. Treat every intent as secret.
    Saving/loading never sends. Only ``client.execute(intent)`` sends once.
    """

    operation: str
    path: str
    body: bytes
    idempotency_key: str
    scope: str
    headers: tuple[tuple[str, str], ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def __post_init__(self) -> None:
        if (any(type(v) is not str for v in (self.operation, self.path, self.scope))
                or type(self.body) is not bytes or len(self.body) > 16384
                or type(self.headers) is not tuple
                or any(type(h) is not tuple or len(h) != 2 or any(type(s) is not str for s in h) for h in self.headers)):
            fail("invalid_intent")
        intent_key(self.idempotency_key)
        valid_time = False
        try:
            instant(self.created_at)
            valid_time = True
        except (ValueError, TypeError, OverflowError):
            pass
        if not valid_time:
            fail("invalid_intent_timestamp")

    def __repr__(self) -> str:
        return "<AvailKit RequestIntent (private)>"

    def save(self, path: str | os.PathLike[str]) -> None:
        """Exclusively create/fsync a0600 file inside an owned0700 directory.

        Existing files are never replaced. Persist before the first send and keep
        the file after unknown outcomes. The directory must already exist.
        """
        success = False
        try:
            parent = Path(path).parent
            info = parent.stat()
            if (info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode)
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()):
                raise ValueError
            encoded = self.export_private()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            if os.name == "posix":
                directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            success = True
        except Exception:
            pass
        if not success:
            # A file may exist after a persistence failure: retain for inspection.
            fail("intent_persistence_failed")

    def export_private(self) -> bytes:
        """Export the original versioned private bytes; never log this value."""
        encoded: bytes | None = None
        try:
            encoded = json.dumps({"version": 1, "operation": self.operation,
                "path": self.path, "body": self.body.decode("utf-8"),
                "idempotencyKey": self.idempotency_key, "scope": self.scope,
                "headers": list(self.headers), "createdAt": self.created_at}, separators=(",", ":")).encode()
        except Exception:
            pass
        if encoded is None or len(encoded) > 65536:
            fail("invalid_intent")
        return encoded

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "RequestIntent":
        return cls.from_private(read_private(path, 131072, "invalid_intent_file"))

    @classmethod
    def from_private(cls, data: bytes) -> "RequestIntent":
        """Import a bounded private record without sending or changing body bytes."""
        if type(data) is not bytes or not 0 < len(data) <= 131072:
            fail("invalid_intent_file")
        intent: RequestIntent | None = None
        try:
            v = json.loads(data.decode("utf-8"))
            keys = {"version", "operation", "path", "body", "idempotencyKey", "scope", "headers", "createdAt"}
            if (type(v) is not dict or v.keys() != keys or type(v["version"]) is not int or v["version"] != 1
                    or any(type(v[k]) is not str for k in ("operation", "path", "body", "scope", "createdAt"))
                    or type(v["headers"]) is not list
                    or any(type(h) is not list or len(h) != 2 or any(type(s) is not str for s in h) for h in v["headers"])):
                raise ValueError
            intent_key(v["idempotencyKey"])
            intent = cls(v["operation"], v["path"], v["body"].encode("utf-8"),
                         v["idempotencyKey"], v["scope"], tuple(tuple(h) for h in v["headers"]), v["createdAt"])
        except Exception:
            pass
        if intent is None:
            fail("invalid_intent_file")
        return intent
