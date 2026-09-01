"""Fernet-based field-level encryption for sensitive model fields.

A connected Google account gives this app full read/write access to somebody's
calendar, so the refresh token must not sit in the database in plain text. The
Fernet key is derived deterministically from ``SECRET_KEY``, which means:

* nothing extra to configure, and
* rotating ``SECRET_KEY`` invalidates the stored secrets, so the salon simply
  reconnects the Google account afterwards.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _get_fernet() -> Fernet:
    """Derive a Fernet key from Django's SECRET_KEY (deterministic, stable)."""
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt_value(plaintext: str) -> str:
    """Return a Fernet-encrypted, base64-encoded string ("" stays "")."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a value written by :func:`encrypt_value`.

    Values stored before encryption was introduced (and values orphaned by a
    ``SECRET_KEY`` change) are returned as-is rather than raising, so a bad
    secret degrades to "reconnect the account" instead of a 500.
    """
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ciphertext
