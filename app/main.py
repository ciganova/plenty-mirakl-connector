"""
FastAPI application entry point.

Routes:
  GET  /health                    – Liveness + readiness (DB + Redis + API keys)
  GET  /healthz                   – simple liveness (deploy gate)
  GET  /status                    – Order sync counts by status (admin)
  POST /orders/{id}/retry         – Manually re-trigger a failed order (api-key)
  POST /webhooks/plenty/<tid>/order-status  – Plenty event-procedure callback
  POST /billing/stripe-webhook    – Stripe subscription events
  GET  /panel                     – Tenant operator panel
  GET  /panel/admin               – Cross-tenant admin panel
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import redis.asyncio as redis
import stripe
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_tenant_dep
from app.config import Settings, get_settings
from app.core.logging import configure_logging, logger
from app.models.database import engine, get_db
from app.models.tables import OrderSync
from app.panel.routes import router as panel_router
from app.tenancy.context import tenant_scope
from app.tenancy.models import Tenant
from app.webhooks.plenty import router as plenty_webhook_router
from app.webhooks.stripe import router as stripe_webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("connector.startup", dry_run=settings.dry_run)
    yield
    await engine.dispose()
    logger.info("connector.shutdown")


app = FastAPI(
    title="PlentyONE-Mirakl Connector SaaS",
    version="2.0.0",
    description="Multi-tenant connector between Mirakl marketplaces and PlentyONE ERP.",
    lifespan=lifespan,
)


app.include_router(panel_router)
app.include_router(plenty_webhook_router)
app.include_router(stripe_webhook_router)


# ---------------------------------------------------------------------------
# Landing page (serve web/ as static, with / -> index.html)
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if _WEB_DIR.is_dir():
    @app.get("/", include_in_schema=False)
    async def landing_root() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    @app.get("/imprint.html", include_in_schema=False)
    async def landing_imprint() -> FileResponse:
        return FileResponse(_WEB_DIR / "imprint.html")

    @app.get("/privacy.html", include_in_schema=False)
    async def landing_privacy() -> FileResponse:
        return FileResponse(_WEB_DIR / "privacy.html")

    @app.get("/terms.html", include_in_schema=False)
    async def landing_terms() -> FileResponse:
        return FileResponse(_WEB_DIR / "terms.html")

    @app.get("/style.css", include_in_schema=False)
    async def landing_css() -> FileResponse:
        return FileResponse(_WEB_DIR / "style.css")

    @app.get("/og.png", include_in_schema=False)
    async def landing_og() -> FileResponse:
        return FileResponse(_WEB_DIR / "og.png")


# ---------------------------------------------------------------------------
# Stripe Checkout — public trial sign-up entrypoint
# ---------------------------------------------------------------------------

@app.get("/billing/checkout", include_in_schema=False)
async def billing_checkout_redirect(
    plan: str = Query("starter"),
    settings: Settings = Depends(get_settings),
):
    """Public trial CTA — creates a Stripe Checkout Session and 303-redirects.

    Tenant is provisioned post-checkout via the webhook
    (`checkout.session.completed`) once we have customer details.
    """
    if not settings.stripe_secret_key or not settings.stripe_price_default:
        # Stripe not configured (e.g. local dev) — show a friendly fallback.
        return HTMLResponse(
            "<h1>Trial sign-up not configured yet</h1>"
            "<p>Mail us at <a href='mailto:contact@vagabond-consulting.com'>"
            "contact@vagabond-consulting.com</a> to get early access.</p>",
            status_code=503,
        )
    stripe.api_key = settings.stripe_secret_key
    base_url = os.environ.get("BASE_URL", f"https://{settings.traefik_domain}")
    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_default, "quantity": 1}],
            success_url=f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/?canceled=1",
            allow_promotion_codes=True,
            subscription_data={"trial_period_days": 14},
            metadata={"plan": plan},
        )
    except Exception as exc:
        logger.error("billing.checkout.create_failed", error=str(exc))
        raise HTTPException(502, detail=f"stripe: {exc}")
    return RedirectResponse(url=sess.url, status_code=303)


@app.post("/billing/checkout", include_in_schema=False)
async def billing_checkout_post(
    plan: str = Query("starter"),
    settings: Settings = Depends(get_settings),
):
    """POST variant — same behavior. Used by the API smoke test."""
    return await billing_checkout_redirect(plan=plan, settings=settings)


@app.get("/billing/success", include_in_schema=False)
async def billing_success(session_id: str = Query(None)) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
        <title>Welcome — PlentyMirakl Connector</title>
        <script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-zinc-950 text-zinc-100 font-mono p-10">
        <div class="max-w-xl mx-auto">
        <h1 class="text-3xl font-bold text-emerald-400 mb-4">Trial activated.</h1>
        <p class="mb-2">Session: <code class="text-zinc-400">{session_id or 'n/a'}</code></p>
        <p class="mb-6">Check your inbox — we'll send your tenant API key + login link in the next minutes.</p>
        <a href="/" class="text-emerald-400 underline">← back home</a>
        </div></body></html>"""
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
async def healthz(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Lightweight liveness — used by Traefik + deploy gate."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, detail=f"db: {exc}")
    return {"ok": True, "ts": time.time()}


@app.get("/health", tags=["ops"])
async def health(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Dict[str, Any]:
    checks: Dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    try:
        r = redis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    checks["stripe_configured"] = (
        "ok" if settings.stripe_webhook_secret else "missing"
    )
    checks["fernet_configured"] = (
        "ok" if settings.fernet_key else "missing"
    )

    all_ok = all(v == "ok" for v in checks.values()
                 if not v.endswith("missing"))
    http_status = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if all_ok else "degraded",
        "checks": checks,
        "dry_run": settings.dry_run,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Per-tenant status + retry
# ---------------------------------------------------------------------------

@app.get("/status", tags=["api"])
async def sync_status(
    tenant: Tenant = Depends(current_tenant_dep),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    async with tenant_scope(db, tenant.id):
        result = await db.execute(
            select(OrderSync.status,
                   func.count(OrderSync.id).label("count"))
            .where(OrderSync.tenant_id == tenant.id)
            .group_by(OrderSync.status)
        )
        rows = result.all()
    return {
        "tenant": tenant.name,
        "orders_by_status": {row.status: row.count for row in rows},
    }


@app.post("/orders/{mirakl_order_id}/retry", tags=["api"])
async def retry_order(
    mirakl_order_id: str,
    tenant: Tenant = Depends(current_tenant_dep),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, str]:
    async with tenant_scope(db, tenant.id):
        result = await db.execute(
            select(OrderSync).where(
                OrderSync.tenant_id == tenant.id,
                OrderSync.mirakl_order_id == mirakl_order_id,
            )
        )
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(404, "Order not found")
        if record.status != "ERROR":
            raise HTTPException(
                400, f"Order is in status '{record.status}', can only retry ERROR orders",
            )
        record.status = "NEW"
        record.error_count = 0
        record.error_message = None
        db.add(record)
    logger.info("api.retry", order_id=mirakl_order_id, tenant_id=str(tenant.id))
    return {"status": "queued", "order_id": mirakl_order_id}
