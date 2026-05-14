"""Per-tenant monthly order quota.

* increment on each successful Plenty order create (atomic UPSERT)
* block at 100% (orchestrator skips tenant; API returns 402)
* alert at 80% and 100%
* monthly reset on day 1 via Celery beat
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Tuple

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.tenancy.models import Tenant, UsageCounter


def _ym(now: datetime | None = None) -> tuple[int, int]:
    n = now or datetime.now(timezone.utc)
    return n.year, n.month


async def increment_usage(db: AsyncSession, tenant_id: uuid.UUID, n: int = 1) -> int:
    """Atomic UPSERT + increment. Returns new orders_imported value."""
    year, month = _ym()
    # Postgres UPSERT
    await db.execute(text("""
        INSERT INTO usage_counters (tenant_id, period_year, period_month, orders_imported)
        VALUES (:t, :y, :m, :n)
        ON CONFLICT (tenant_id, period_year, period_month)
        DO UPDATE SET orders_imported = usage_counters.orders_imported + :n
    """), {"t": tenant_id, "y": year, "m": month, "n": n})
    res = await db.execute(text("""
        SELECT orders_imported FROM usage_counters
        WHERE tenant_id=:t AND period_year=:y AND period_month=:m
    """), {"t": tenant_id, "y": year, "m": month})
    return int(res.scalar() or 0)


async def quota_status(db: AsyncSession, tenant: Tenant) -> Tuple[int, int, float]:
    """Returns (used, quota, fraction). fraction = used/quota (capped 1.0)."""
    year, month = _ym()
    res = await db.execute(text("""
        SELECT orders_imported FROM usage_counters
        WHERE tenant_id=:t AND period_year=:y AND period_month=:m
    """), {"t": tenant.id, "y": year, "m": month})
    used = int(res.scalar() or 0)
    quota = int(tenant.monthly_quota or 0) or 1
    frac = min(1.0, used / quota) if quota else 0.0
    return used, quota, frac


async def check_and_block_if_exceeded(db: AsyncSession, tenant: Tenant) -> bool:
    """Returns True if tenant is over quota and should be SKIPPED.

    Side-effect: at 80% and 100% emits an alert log line. Wiring to BillionMail
    happens in `_alert` (currently structlog only — see ARCHITECTURE_SAAS.md §9).
    """
    used, quota, frac = await quota_status(db, tenant)
    if frac >= 1.0:
        await _alert(tenant, "quota_exceeded", used, quota)
        return True
    if frac >= 0.8:
        await _alert(tenant, "quota_warning_80pct", used, quota)
    return False


async def _alert(tenant: Tenant, kind: str, used: int, quota: int) -> None:
    # TODO: POST to billionmail_api_url. For now structlog only — operator
    #       sees it in /var/log/connector/app.json.
    logger.warning(
        "quota.alert",
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        alert_kind=kind,
        used=used,
        quota=quota,
        contact_email=tenant.contact_email,
    )


async def monthly_reset(db: AsyncSession) -> int:
    """Called by Celery beat on day 1 at 00:05 UTC. Inserts a fresh
    counter row for every active tenant and reverts `quota_exceeded` →
    `active`. Returns number of tenants reset."""
    year, month = _ym()
    res = await db.execute(select(Tenant).where(Tenant.status.in_(
        ("active", "trial", "quota_exceeded"))))
    tenants = list(res.scalars().all())
    for t in tenants:
        await db.execute(text("""
            INSERT INTO usage_counters (tenant_id, period_year, period_month, orders_imported)
            VALUES (:t, :y, :m, 0)
            ON CONFLICT (tenant_id, period_year, period_month) DO NOTHING
        """), {"t": t.id, "y": year, "m": month})
        if t.status == "quota_exceeded":
            t.status = "active"
            db.add(t)
    await db.flush()
    logger.info("quota.monthly_reset", tenants=len(tenants))
    return len(tenants)
