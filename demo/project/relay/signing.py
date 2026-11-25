"""HMAC signatures so receivers can check that an event came from us."""

import hashlib
import hmac

from relay import config


def sign(body: bytes, secret: str | None = None) -> str:
    """Hex HMAC-SHA256 of the request body."""
    key = (secret or config.SIGNING_SECRET).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str) -> bool:
    """Constant-time check against the current secret, then the previous one."""
    secrets = [config.SIGNING_SECRET, config.SIGNING_SECRET_PREVIOUS]
    return any(hmac.compare_digest(sign(body, s), signature) for s in secrets if s)
