"""Verify the exact raw webhook body before JSON decoding or processing."""

import base64
import hashlib
import hmac
import re
import time

from .errors import AvailKitError


def verify_webhook(body: bytes, signature: str, signing_secret: str, *,
                   now: int | None = None, tolerance: int = 300) -> bool:
    """Check HMAC-SHA256 and absolute timestamp age in seconds.

    ReturnsFalse for untrusted data. Invalid local tolerance raises a redacted
    input error. Deduplicate the stable AvailKit-Delivery-ID transactionally only
    after verification; retries retain delivery identity and refresh signatures.
    """
    if type(tolerance) is not int or not 0 <= tolerance <= 3600 or now is not None and (type(now) is not int or now < 0):
        raise AvailKitError("input", "invalid_webhook_tolerance")
    if (type(body) is not bytes or len(body) > 16384 or type(signature) is not str
            or type(signing_secret) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", signing_secret)):
        return False
    match = re.fullmatch(r"t=(0|[1-9][0-9]{0,19}),v1=([0-9a-f]{64})", signature)
    if not match:
        return False
    stamp, claimed = match.groups()
    current = int(time.time()) if now is None else now
    if abs(current - int(stamp)) > tolerance:
        return False
    key = base64.urlsafe_b64decode(signing_secret + "=")
    expected = hmac.new(key, stamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, claimed)
