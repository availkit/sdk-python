"""Direct HTTP, one request, no redirects, proxy env, cookies or retries."""

import http.client
import json
import queue
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .errors import AvailKitError
from ._validation import fail

# Bound resolver concurrency; a timed-out DNS worker can finish but cannot send.
_RESOLVERS = threading.BoundedSemaphore(8)


def origin(value: str) -> str:
    result: str | None = None
    try:
        u = urlsplit(value)
        if (u.scheme not in ("https", "http") or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path not in ("", "/")
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
                or u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1")):
            fail("invalid_base_url")
        _ = u.port
        result = f"{u.scheme}://{u.netloc}".rstrip("/")
    except (TypeError, ValueError, AttributeError):
        pass
    if result is None:
        fail("invalid_base_url")
    return result


class Transport:
    def __init__(self, base_url: str, timeout: float, max_response_bytes: int):
        self.base_url = origin(base_url)
        if (type(timeout) not in (float, int) or not 0 < timeout <= 120
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 8388608):
            fail("invalid_limits")
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self._tls = ssl.create_default_context()

    def request(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        deadline = time.monotonic() + self.timeout
        sock: socket.socket | None = None
        connection: http.client.HTTPConnection | None = None
        timer: threading.Timer | None = None
        expired = threading.Event()
        caught: AvailKitError | None = None
        result: tuple[int, dict[str, str], Any] | None = None
        try:
            u = urlsplit(self.base_url)
            host = u.hostname or ""
            port = u.port or (443 if u.scheme == "https" else 80)
            pending: queue.Queue[Any] = queue.Queue(maxsize=1)
            if not _RESOLVERS.acquire(blocking=False):
                raise AvailKitError("transport", "resolver_busy")

            def resolve() -> None:
                try:
                    pending.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
                except Exception:
                    pending.put(None)
                finally:
                    _RESOLVERS.release()

            threading.Thread(target=resolve, daemon=True, name="availkit-dns").start()
            addresses = pending.get(timeout=max(0.001, deadline - time.monotonic()))
            if not addresses:
                raise AvailKitError("transport", "request_failed")
            for family, kind, protocol, _, address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    sock = socket.socket(family, kind, protocol)
                    sock.settimeout(remaining)
                    sock.connect(address)
                    break
                except OSError:
                    if sock is not None:
                        sock.close()
                    sock = None
            if sock is None:
                raise AvailKitError("transport", "request_failed")

            def abort() -> None:
                expired.set()
                # Shutdown wakes reads even while HTTPResponse owns a file wrapper.
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            timer = threading.Timer(remaining, abort)
            timer.daemon = True
            timer.start()
            if u.scheme == "https":
                sock.settimeout(remaining)
                sock = self._tls.wrap_socket(sock, server_hostname=host)
            connection = http.client.HTTPConnection(host, port, timeout=remaining)
            connection.sock = sock
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError
            connection.request(method, path, body=body, headers={
                "Accept": "application/json", "Accept-Encoding": "identity",
                "Cache-Control": "no-store", "User-Agent": "availkit-python/0.2.0",
                **({"Content-Type": "application/json"} if body is not None else {}), **headers,
            })
            response = connection.getresponse()
            status = response.status
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            media = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if (300 <= status < 400 or media not in ("application/json", "application/problem+json")
                    or 200 <= status < 300 and media != "application/json"
                    or response_headers.get("content-encoding", "identity").lower() != "identity"):
                raise AvailKitError("protocol", "invalid_response", status)
            length = response_headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > self.max_response_bytes):
                raise AvailKitError("protocol", "response_too_large", status)
            chunks = bytearray()
            while True:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError
                data = response.read1(min(65536, self.max_response_bytes + 1 - len(chunks)))
                if not data:
                    break
                chunks.extend(data)
                if len(chunks) > self.max_response_bytes:
                    raise AvailKitError("protocol", "response_too_large", status)
            if length is not None and len(chunks) != int(length):
                raise AvailKitError("protocol", "incomplete_response", status)
            # Disallow non-standard JSON numbers and duplicate keys.
            def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                d: dict[str, Any] = {}
                for k, v in pairs:
                    if k in d:
                        raise ValueError
                    d[k] = v
                return d

            def invalid_constant(_: str) -> Any:
                raise ValueError

            try:
                decoded = json.loads(chunks.decode("utf-8"), object_pairs_hook=object_pairs, parse_constant=invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise AvailKitError("protocol", "invalid_json", status) from None
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError
            result = status, response_headers, decoded
        except AvailKitError as exc:
            caught = AvailKitError("transport", "timeout") if expired.is_set() else exc
        except (TimeoutError, queue.Empty):
            caught = AvailKitError("transport", "timeout")
        except Exception:
            caught = AvailKitError("transport", "timeout" if expired.is_set() else "request_failed")
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
            if sock is not None:
                sock.close()
        # Raise outside exception handlers so raw transport causes are not retained.
        if caught is not None:
            raise caught from None
        assert result is not None
        return result
