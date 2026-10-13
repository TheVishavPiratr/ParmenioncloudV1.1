"""Short-lived signed URLs for files that must open without a login header
(<img> and <video> tags cannot send one).

Each purpose ("media", "gallery", ...) gets its own key derived from
JWT_SECRET, so a link for one kind of file never works for another, and a
link can never pass as a JWT.
"""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from api.core.config import settings


def _key(purpose: str) -> bytes:
    label = f"visionpulse-link:{purpose}".encode()
    return hmac.new(settings.jwt_secret.encode(), label, hashlib.sha256).digest()


def sign(purpose: str, resource_id: uuid.UUID, expires: int) -> str:
    message = f"{resource_id}:{expires}".encode()
    return hmac.new(_key(purpose), message, hashlib.sha256).hexdigest()


def create(
    purpose: str, path: str, resource_id: uuid.UUID, ttl: timedelta
) -> tuple[str, datetime]:
    """Returns (url, expires_at). `path` is the file route without a query."""
    expires_at = datetime.now(UTC).replace(microsecond=0) + ttl
    expires = int(expires_at.timestamp())
    signature = sign(purpose, resource_id, expires)
    return f"{path}?exp={expires}&sig={signature}", expires_at


def verify(purpose: str, resource_id: uuid.UUID, expires: int, signature: str) -> bool:
    """Whether the signature is genuine and the link has not expired."""
    expected = sign(purpose, resource_id, expires)
    return hmac.compare_digest(signature, expected) and (
        expires > datetime.now(UTC).timestamp()
    )
