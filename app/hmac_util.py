from __future__ import annotations

import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: str) -> bool:
    expected = sign(body, secret)
    provided = signature.strip()
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)
