"""Plenty event-procedure webhook receiver.

Plenty sends a POST when an order's status changes (configured per tenant
in Plenty's event-procedure UI to fire on `Order.statusUpdated`).

We support two auth modes (see ARCHITECTURE_SAAS.md §A3):

  1. HMAC header `X-Plenty-Signature: sha256=<hex>` over raw body, secret
     = plenty_connections.webhook_secret. Preferred.
  2. Shared secret in query string `?secret=<value>`. Fallback when the
     specific Plenty event-procedure can't add custom headers.

On status 7 (Versendet) WITH a packageNumber, we trigger Mirakl OR23 ship.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.audit.log import audit
from app.config import Settings, get_settings
from app.core.logging import logger
from app.models.database import get_db
from app.models.tables import OrderSync
from app.tenancy.context import tenant_scope
from app.tenancy.models import MiraklConnection, PlentyConnection, Tenant


router = APIRouter(prefix="/webhooks/plenty", tags=["webhooks"])


def _verify(raw: bytes, secret: str, header_sig: str | None,
            query_secret: str | None) -> bool:
    if header_sig:
        if header_sig.startswith("sha256="):
            header_sig = header_sig.split("=", 1)[1]
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header_sig)
    if query_secret:
        return hmac.compare_digest(secret, query_secret)
    return False


@router.post("/{tenant_id}/order-status")
async def plenty_order_status(
    tenant_id: str,
    request: Request,
    secret: str | None = None,  # query
    x_plenty_signature: str | None = Header(default=None, alias="X-Plenty-Signature"),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    raw = await request.body()
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(404, "unknown tenant")

    tenant = await db.get(Tenant, tid)
    if tenant is None:
        raise HTTPException(404, "unknown tenant")

    res = await db.execute(
        select(PlentyConnection).where(
            PlentyConnection.tenant_id == tid,
            PlentyConnection.active.is_(True),
        )
    )
    plenty_conn = res.scalar_one_or_none()
    if plenty_conn is None or not plenty_conn.webhook_secret:
        raise HTTPException(403, "no webhook configured")

    if not _verify(raw, plenty_conn.webhook_secret, x_plenty_signature, secret):
        logger.warning("plenty.webhook.invalid_sig", tenant_id=tenant_id)
        raise HTTPException(401, "invalid signature")

    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    plenty_order_id = body.get("orderId") or body.get("id")
    new_status = body.get("statusId") or body.get("status")
    package_number = body.get("packageNumber") or body.get("trackingNumber")
    carrier = body.get("carrier") or body.get("shippingProvider") or "DHL"

    if not plenty_order_id or new_status is None:
        return {"ok": True, "ignored": "missing orderId/statusId"}

    # Status 7 = Versendet. Floats arrive sometimes (5.1, 7.0).
    try:
        st = float(new_status)
    except (TypeError, ValueError):
        return {"ok": True, "ignored": f"unparseable status {new_status!r}"}

    if int(st) != 7:
        return {"ok": True, "ignored": f"status {st} not 7"}

    if not package_number:
        return {"ok": True, "ignored": "no tracking yet"}

    # Find the order_sync row for this tenant + plenty_order_id
    async with tenant_scope(db, tid):
        res2 = await db.execute(
            select(OrderSync, MiraklConnection)
            .join(MiraklConnection,
                  MiraklConnection.id == OrderSync.mirakl_connection_id)
            .where(
                OrderSync.tenant_id == tid,
                OrderSync.plenty_order_id == int(plenty_order_id),
            )
        )
        row = res2.first()
        if row is None:
            return {"ok": True, "ignored": f"no order_sync for plenty_id={plenty_order_id}"}

        order_sync, mirakl_conn = row

        async with MiraklClient.from_connection(settings, mirakl_conn) as mirakl:
            ok = await mirakl.ship_order(
                order_sync.mirakl_order_id, str(package_number), str(carrier)
            )

        if ok:
            order_sync.status = "SHIPPED"
            db.add(order_sync)
            await audit(db, actor="plenty.webhook", action="mirakl.ship",
                        tenant_id=tid, entity="order",
                        entity_id=order_sync.mirakl_order_id,
                        payload={"tracking": package_number, "carrier": carrier})
            await db.flush()
            return {"ok": True, "shipped": order_sync.mirakl_order_id}

    return {"ok": False, "error": "mirakl ship failed"}
