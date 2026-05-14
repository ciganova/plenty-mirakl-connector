"""Fernet helpers for at-rest encryption of per-tenant connection secrets."""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(blob: bytes) -> str:
    if not blob:
        return ""
    try:
        return _fernet().decrypt(blob).decode()
    except Exception:
        # Migration 002 stores legacy values as plain bytes (the operator
        # can rotate via saas_admin.py mirakl-conn add). Don't crash old
        # default-tenant connections on first boot.
        try:
            return blob.decode()
        except UnicodeDecodeError:
            return ""
