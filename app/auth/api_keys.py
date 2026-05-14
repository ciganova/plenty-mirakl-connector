"""Per-tenant API key issuance + verification.

Format: `pmc_` + 43 char urlsafe-base64 (32 random bytes).
Stored as bcrypt(key) in tenants.api_key_hash. Plaintext returned to caller
only once at creation time.
"""
from __future__ import annotations

import secrets

import bcrypt


KEY_PREFIX = "pmc_"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()


def verify_api_key(key: str, hashed: str) -> bool:
    if not key or not hashed:
        return False
    try:
        return bcrypt.checkpw(key.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False
