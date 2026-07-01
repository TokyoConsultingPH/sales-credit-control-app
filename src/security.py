"""Password hashing helpers (stdlib only — PBKDF2-HMAC-SHA256)."""
from __future__ import annotations

import hashlib
import hmac
import os

_ITERATIONS = 200_000


def hash_password(password: str, salt: str | bytes | None = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for a password."""
    if salt is None:
        salt = os.urandom(16)
    elif isinstance(salt, str):
        salt = bytes.fromhex(salt)
    h = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, _ITERATIONS)
    return salt.hex(), h.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        _, h = hash_password(password, salt_hex)
        return hmac.compare_digest(h, str(hash_hex))
    except Exception:
        return False
