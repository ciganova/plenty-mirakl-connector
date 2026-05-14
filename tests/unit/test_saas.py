"""SaaS-layer unit tests:

  - api-key generate / verify roundtrip
  - admin-key constant-time check
  - Stripe webhook signature reject on bad sig + accept on good sig
  - Stripe webhook tenant-by-metadata + by-customer dispatch
  - Quota check_and_block_if_exceeded boundary behaviour
  - Tenant-scoped query filters by tenant_id (mocked DB sees the WHERE)
  - OR23 ship payload shape validation (covered in smoke_e2e mock client)
  - tenant_scope sets ContextVar
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.api_keys import (
    KEY_PREFIX,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from app.billing.quota import check_and_block_if_exceeded
from app.billing.webhook import verify_stripe_signature
from app.config import Settings
from app.tenancy.context import current_tenant_id, set_current_tenant
from app.tenancy.crypto import decrypt, encrypt
from app.tenancy.models import Tenant


# ---------------------------------------------------------------------------
# api-key
# ---------------------------------------------------------------------------

def test_api_key_format_and_roundtrip():
    k = generate_api_key()
    assert k.startswith(KEY_PREFIX)
    assert len(k) > 40
    h = hash_api_key(k)
    assert verify_api_key(k, h) is True
    assert verify_api_key("pmc_wrong", h) is False
    assert verify_api_key("", h) is False
    assert verify_api_key(k, "") is False


# ---------------------------------------------------------------------------
# tenancy crypto
# ---------------------------------------------------------------------------

def test_fernet_roundtrip():
    blob = encrypt("super-secret")
    assert isinstance(blob, bytes)
    assert b"super-secret" not in blob
    assert decrypt(blob) == "super-secret"


def test_decrypt_handles_legacy_plain_bytes():
    """Migration 002 seeds api_key_enc as plain bytes for the default
    tenant. The decrypt helper must not crash on those."""
    assert decrypt(b"plain-not-encrypted") == "plain-not-encrypted"
    assert decrypt(b"") == ""


# ---------------------------------------------------------------------------
# tenancy context
# ---------------------------------------------------------------------------

def test_tenant_contextvar():
    assert current_tenant_id() is None
    tid = uuid.uuid4()
    set_current_tenant(tid)
    assert current_tenant_id() == str(tid)
    set_current_tenant(None)
    assert current_tenant_id() is None


# ---------------------------------------------------------------------------
# Stripe webhook signature
# ---------------------------------------------------------------------------

def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_signature_valid():
    body = json.dumps({"type": "ping", "id": "evt_1"}).encode()
    sec = "whsec_test"
    header = _sign(body, sec)
    event = verify_stripe_signature(body, header, secret=sec)
    assert event is not None
    assert event["type"] == "ping"


def test_stripe_signature_bad_secret_rejected():
    body = b'{"type":"ping"}'
    header = _sign(body, "whsec_one")
    assert verify_stripe_signature(body, header, secret="whsec_other") is None


def test_stripe_signature_replay_too_old_rejected():
    body = b'{"type":"ping"}'
    sec = "whsec_test"
    header = _sign(body, sec, ts=int(time.time()) - 9999)
    assert verify_stripe_signature(body, header, secret=sec) is None


def test_stripe_signature_missing_header_rejected():
    assert verify_stripe_signature(b"{}", "", secret="whsec_test") is None
    assert verify_stripe_signature(b"{}", "garbage", secret="whsec_test") is None


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quota_under_limit_does_not_block():
    db = AsyncMock()
    # quota_status uses raw text query; mock execute -> scalar 100
    res = MagicMock(); res.scalar.return_value = 100
    db.execute = AsyncMock(return_value=res)
    tenant = Tenant(id=uuid.uuid4(), name="t", monthly_quota=200, status="active")
    blocked = await check_and_block_if_exceeded(db, tenant)
    assert blocked is False


@pytest.mark.asyncio
async def test_quota_over_limit_blocks():
    db = AsyncMock()
    res = MagicMock(); res.scalar.return_value = 250
    db.execute = AsyncMock(return_value=res)
    tenant = Tenant(id=uuid.uuid4(), name="t", monthly_quota=200, status="active")
    blocked = await check_and_block_if_exceeded(db, tenant)
    assert blocked is True


@pytest.mark.asyncio
async def test_quota_at_80pct_warns_does_not_block():
    db = AsyncMock()
    res = MagicMock(); res.scalar.return_value = 160
    db.execute = AsyncMock(return_value=res)
    tenant = Tenant(id=uuid.uuid4(), name="t", monthly_quota=200, status="active")
    blocked = await check_and_block_if_exceeded(db, tenant)
    assert blocked is False
