"""Stripe webhook signature verification + event router.

CRITICAL: signature verification needs the RAW request body bytes, not the
parsed JSON. The route handler MUST pass `await request.body()` to
`verify_stripe_signature`, NOT a re-serialised dict.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import logger
from app.tenancy.models import Tenant


# Tolerance window for Stripe replay protection (Stripe uses 5min default).
DEFAULT_TOLERANCE = 300


def verify_stripe_signature(
    payload: bytes,
    sig_header: str,
    secret: str | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
) -> Optional[Dict[str, Any]]:
    """Re-implements stripe.Webhook.construct_event without the SDK so the
    test suite has zero outbound network capability. Returns the parsed
    event dict on success, None on failure.

    Header format: `t=<ts>,v1=<sig>,v0=<old-sig>` (we check v1 only).
    """
    sec = (secret if secret is not None else get_settings().stripe_webhook_secret)
    if not sec or not sig_header:
        return None

    parts = {k: v for k, v in (
        item.split("=", 1) for item in sig_header.split(",") if "=" in item
    )}
    ts = parts.get("t")
    v1 = parts.get("v1")
    if not ts or not v1:
        return None
    try:
        ts_int = int(ts)
    except ValueError:
        return None
    if abs(time.time() - ts_int) > tolerance:
        return None

    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(sec.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        return None
    try:
        return json.loads(payload.decode())
    except json.JSONDecodeError:
        return None


async def handle_stripe_event(db: AsyncSession, event: Dict[str, Any]) -> None:
    """Dispatch a verified Stripe event to the right handler."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    logger.info("stripe.event", type=etype, id=event.get("id"))

    if etype == "checkout.session.completed":
        await _on_checkout_completed(db, obj)
    elif etype in ("customer.subscription.created",
                   "customer.subscription.updated"):
        await _on_subscription_change(db, obj)
    elif etype == "customer.subscription.deleted":
        await _on_subscription_deleted(db, obj)
    elif etype == "invoice.payment_succeeded":
        await _on_payment_succeeded(db, obj)
    elif etype == "invoice.payment_failed":
        await _on_payment_failed(db, obj)
    else:
        logger.info("stripe.event.ignored", type=etype)


async def _tenant_by_customer(db: AsyncSession, cus: str | None) -> Tenant | None:
    if not cus:
        return None
    res = await db.execute(select(Tenant).where(Tenant.stripe_customer_id == cus))
    return res.scalar_one_or_none()


async def _tenant_by_metadata(db: AsyncSession, meta: dict) -> Tenant | None:
    tid = (meta or {}).get("tenant_id")
    if not tid:
        return None
    return await db.get(Tenant, tid)


def _ts_to_dt(ts: int | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


async def _on_checkout_completed(db: AsyncSession, sess: dict) -> None:
    """First successful subscription checkout. The Stripe Checkout Session
    metadata MUST carry `tenant_id` (or `tenant_name` for fresh signups).
    For fresh signups the panel signup endpoint creates the Tenant row
    BEFORE redirecting to Stripe and stuffs tenant_id into metadata.
    """
    meta = sess.get("metadata") or {}
    tenant = await _tenant_by_metadata(db, meta)
    if tenant is None:
        logger.warning("stripe.checkout.no_tenant_metadata", session_id=sess.get("id"))
        return
    tenant.stripe_customer_id = sess.get("customer") or tenant.stripe_customer_id
    tenant.stripe_subscription_id = sess.get("subscription") or tenant.stripe_subscription_id
    tenant.status = "active"
    db.add(tenant)
    await db.flush()


async def _on_subscription_change(db: AsyncSession, sub: dict) -> None:
    tenant = (await _tenant_by_metadata(db, sub.get("metadata") or {})
              or await _tenant_by_customer(db, sub.get("customer")))
    if tenant is None:
        return
    tenant.stripe_subscription_id = sub.get("id") or tenant.stripe_subscription_id
    tenant.current_period_end = _ts_to_dt(sub.get("current_period_end"))
    status = sub.get("status") or "active"
    if status in ("active", "trialing"):
        tenant.status = "active"
    elif status == "past_due":
        tenant.status = "past_due"
    elif status in ("canceled", "incomplete_expired"):
        tenant.status = "canceled"
    db.add(tenant)
    await db.flush()


async def _on_subscription_deleted(db: AsyncSession, sub: dict) -> None:
    tenant = await _tenant_by_customer(db, sub.get("customer"))
    if tenant is None:
        return
    tenant.status = "canceled"
    db.add(tenant)
    await db.flush()


async def _on_payment_succeeded(db: AsyncSession, inv: dict) -> None:
    tenant = await _tenant_by_customer(db, inv.get("customer"))
    if tenant is None:
        return
    if tenant.status in ("past_due", "suspended"):
        tenant.status = "active"
    db.add(tenant)
    await db.flush()


async def _on_payment_failed(db: AsyncSession, inv: dict) -> None:
    tenant = await _tenant_by_customer(db, inv.get("customer"))
    if tenant is None:
        return
    tenant.status = "past_due"
    db.add(tenant)
    await db.flush()
    logger.warning("stripe.payment_failed", tenant_id=str(tenant.id),
                   contact=tenant.contact_email)
