"""Reversible encryption for camera passwords.

The agent needs the real password back to open the camera's stream, so it is
encrypted (Fernet: AES-128-CBC with an HMAC-SHA256 check), not hashed. The key
comes only from .env (CAMERA_CREDENTIALS_KEY); losing it makes every stored
camera password unreadable.
"""

from cryptography.fernet import Fernet

from api.core.config import settings

_fernet = Fernet(settings.camera_credentials_key)


def encrypt(password: str) -> str:
    return _fernet.encrypt(password.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()
