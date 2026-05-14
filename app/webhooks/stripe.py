"""Stripe webhook receiver. CRITICAL: must read raw body bytes BEFORE
any parsing — signature verification needs the unparsed payload exactly
as Stripe sent it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.webhook import handle_stripe_event, verify_stripe_signature
from app.core.logging import logger
from app.models.database import get_db


router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/stripe-webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
):
    raw = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing Stripe-Signature")
    event = verify_stripe_signature(raw, stripe_signature)
    if event is None:
        logger.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=400, detail="invalid signature")
    await handle_stripe_event(db, event)
    return {"received": True}
