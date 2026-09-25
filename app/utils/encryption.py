"""
Reusable Fernet encryption helper.

Historically, Fernet was only used by ``app.auth.google_token_manager``
for Google OAuth tokens. With the introduction of BYO email senders
(SMTP / SES credentials) and Slack incoming webhooks, we need the same
primitive in a non-Google context. Rather than import from
``GoogleTokenManager`` (which would tangle unrelated modules), this
file exposes a tiny wrapper around a process-wide ``Fernet`` instance
keyed off ``settings.TOKEN_ENCRYPTION_KEY``.

Usage::

    from app.utils.encryption import encrypt_str, decrypt_str, encrypt_json, decrypt_json

    ciphertext = encrypt_str("s3cret")
    plaintext  = decrypt_str(ciphertext)

    blob = encrypt_json({"host": "smtp.gmail.com", "password": "..."})
    cfg  = decrypt_json(blob)

Design notes:
  * The Fernet instance is built lazily on first call so importing this
    module has no side effects and doesn't require settings to be loaded.
  * We intentionally do NOT provide a ``decrypt_or_none`` helper — if a
    ciphertext fails to decrypt, something is wrong (bad key, corrupted
    DB row, key rotation in progress) and callers should see the raw
    ``InvalidToken`` error so it shows up in logs.
  * Rotation: to rotate the key, bump ``TOKEN_ENCRYPTION_KEY``, run a
    one-off migration that decrypts with the old key and re-encrypts
    with the new one, then deploy. This file does not implement rotation.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet

from app.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Build the Fernet instance once per process.

    Cached for performance: Fernet key derivation is expensive.
    Called on first encryption/decryption and cached for the lifetime of the process.

    Raises:
        RuntimeError: If TOKEN_ENCRYPTION_KEY is not configured
    """
    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not configured. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    # Fernet expects bytes; convert from str if needed
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    return Fernet(key_bytes)


def encrypt_str(plaintext: str) -> str:
    """Encrypt a string, return urlsafe-b64 ciphertext as str."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_str(ciphertext: str) -> str:
    """Decrypt a urlsafe-b64 ciphertext, return the plaintext string.

    Raises ``cryptography.fernet.InvalidToken`` on tamper / wrong key.
    """
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def encrypt_json(value: Any) -> str:
    """Serialise ``value`` to JSON and Fernet-encrypt it."""
    return encrypt_str(json.dumps(value, separators=(",", ":"), sort_keys=True))


def decrypt_json(ciphertext: str) -> Any:
    """Decrypt a JSON blob previously written by ``encrypt_json``."""
    return json.loads(decrypt_str(ciphertext))
