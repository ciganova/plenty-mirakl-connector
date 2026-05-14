"""Operator panel — Jinja+HTMX, dark + dense, auto-refresh.

Two surfaces:
  /panel?key=<api_key>      — tenant view (per-tenant)
  /panel/admin?key=<admin>  — admin view (all tenants)
  /panel/health             — JSON health (no auth)

We accept the key via query param `?key=` for embeds / Bookmarks.
The session cookie is set after the first hit so subsequent HTMX polls
don't need it in the URL.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import verify_api_key
from app.billing.quota import quota_status
from app.config import Settings, get_settings
from app.models.database import get_db
from app.models.tables import OrderSync
from app.tenancy.models import (
    AuditLog,
    MiraklConnection,
    PlentyConnection,
    Tenant,
)


router = APIRouter(prefix="/panel", tags=["panel"])

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# auth helpers — session cookie OR ?key= query param
# ---------------------------------------------------------------------------

async def _resolve_tenant(
    db: AsyncSession,
    key: Optional[str],
    cookie: Optional[str],
) -> Tenant:
    candidate = key or cookie
    if not candidate:
        raise HTTPException(401, "missing key")
    res = await db.execute(select(Tenant).where(Tenant.api_key_hash.isnot(None)))
    for t in res.scalars().all():
        if verify_api_key(candidate, t.api_key_hash or ""):
            return t
    raise HTTPException(401, "invalid key")


def _check_admin(key: Optional[str], cookie: Optional[str], settings: Settings) -> None:
    candidate = key or cookie
    if not candidate or not secrets.compare_digest(candidate, settings.admin_api_key):
        raise HTTPException(403, "admin only")


# ---------------------------------------------------------------------------
# Tenant view
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def panel_root(
    request: Request,
    response: Response,
    key: Optional[str] = None,
    pmc_session: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    tenant = await _resolve_tenant(db, key, pmc_session)
    if key:
        # First load — set cookie so future HTMX polls don't need ?key=
        response = HTMLResponse(content="", status_code=200)
        response.set_cookie("pmc_session", key, httponly=True, samesite="lax",
                            max_age=86400 * 7)
    ctx = await _tenant_context(db, tenant, settings)
    rendered = templates.TemplateResponse(
        request, "tenant.html", ctx
    )
    if key:
        rendered.set_cookie("pmc_session", key, httponly=True, samesite="lax",
                            max_age=86400 * 7)
    return rendered


@router.get("/data", response_class=HTMLResponse)
async def panel_data(
    request: Request,
    pmc_session: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """HTMX poll target — returns the inner table fragment."""
    tenant = await _resolve_tenant(db, None, pmc_session)
    ctx = await _tenant_context(db, tenant, settings)
    return templates.TemplateResponse(request, "tenant_fragment.html", ctx)


# ---------------------------------------------------------------------------
# Admin view
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def panel_admin(
    request: Request,
    key: Optional[str] = None,
    pmc_admin: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _check_admin(key, pmc_admin, settings)
    ctx = await _admin_context(db, settings)
    rendered = templates.TemplateResponse(request, "admin.html", ctx)
    if key:
        rendered.set_cookie("pmc_admin", key, httponly=True, samesite="lax",
                            max_age=86400)
    return rendered


@router.get("/admin/data", response_class=HTMLResponse)
async def panel_admin_data(
    request: Request,
    pmc_admin: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    _check_admin(None, pmc_admin, settings)
    ctx = await _admin_context(db, settings)
    return templates.TemplateResponse(request, "admin_fragment.html", ctx)


# ---------------------------------------------------------------------------
# Public health (no auth — used by Traefik)
# ---------------------------------------------------------------------------

@router.get("/health")
async def panel_health(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(func.count()).select_from(Tenant))
    return {"ok": True, "tenants": int(res.scalar() or 0)}


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

async def _tenant_context(db, tenant: Tenant, settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    today = now - timedelta(days=1)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)

    async def _count(since):
        r = await db.execute(
            select(func.count()).select_from(OrderSync).where(
                OrderSync.tenant_id == tenant.id,
                OrderSync.created_at >= since,
            )
        )
        return int(r.scalar() or 0)

    orders_today = await _count(today)
    orders_week = await _count(week)
    orders_month = await _count(month)

    err_res = await db.execute(
        select(func.count()).select_from(OrderSync).where(
            OrderSync.tenant_id == tenant.id,
            OrderSync.status == "ERROR",
        )
    )
    error_count = int(err_res.scalar() or 0)

    used, quota, frac = await quota_status(db, tenant)

    mres = await db.execute(
        select(MiraklConnection).where(MiraklConnection.tenant_id == tenant.id)
    )
    mirakl_conns = list(mres.scalars().all())
    pres = await db.execute(
        select(PlentyConnection).where(PlentyConnection.tenant_id == tenant.id)
    )
    plenty_conns = list(pres.scalars().all())

    audit_res = await db.execute(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id)
        .order_by(desc(AuditLog.ts)).limit(50)
    )
    audit_rows = list(audit_res.scalars().all())

    return {
        "tenant": tenant,
        "orders_today": orders_today,
        "orders_week": orders_week,
        "orders_month": orders_month,
        "error_count": error_count,
        "quota_used": used,
        "quota_total": quota,
        "quota_pct": int(frac * 100),
        "mirakl_conns": mirakl_conns,
        "plenty_conns": plenty_conns,
        "audit_rows": audit_rows,
        "refresh_seconds": settings.panel_refresh_seconds,
        "now": now,
    }


async def _admin_context(db, settings: Settings) -> dict:
    res = await db.execute(select(Tenant).order_by(desc(Tenant.created_at)))
    tenants = list(res.scalars().all())

    rows = []
    total_mrr = 0
    month_orders = 0
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    for t in tenants:
        used, quota, frac = await quota_status(db, t)
        err_res = await db.execute(
            select(func.count()).select_from(OrderSync).where(
                OrderSync.tenant_id == t.id, OrderSync.status == "ERROR",
            )
        )
        errs = int(err_res.scalar() or 0)
        ord_res = await db.execute(
            select(func.count()).select_from(OrderSync).where(
                OrderSync.tenant_id == t.id, OrderSync.created_at >= month_start,
            )
        )
        omc = int(ord_res.scalar() or 0)
        month_orders += omc
        # MRR: €29 if active and has subscription, 0 else.
        if t.status in ("active", "trial") and t.stripe_subscription_id:
            total_mrr += 29
        rows.append({
            "tenant": t, "used": used, "quota": quota, "pct": int(frac * 100),
            "errors": errs, "orders_month": omc,
        })

    return {
        "rows": rows,
        "total_mrr": total_mrr,
        "month_orders": month_orders,
        "tenant_count": len(tenants),
        "active_count": sum(1 for t in tenants if t.status == "active"),
        "refresh_seconds": settings.panel_refresh_seconds,
        "now": now,
    }
