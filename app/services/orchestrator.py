"""Multi-tenant fan-out for Mirakl ↔ Plenty sync tasks.

Each Celery beat task calls one orchestrator method. The orchestrator
queries every (tenant, mirakl_conn, plenty_conn) tuple and runs the
existing OrderService / InventoryService against per-connection clients.

Tenant filter: only `status IN ('active','trial')`. Quota-block check is
inside the loop so each tenant fails closed independently.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import select

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.audit.log import audit
from app.billing.quota import check_and_block_if_exceeded, increment_usage
from app.config import Settings
from app.core.logging import logger
from app.models.database import db_session
from app.services.inventory_service import InventoryService
from app.services.order_service import OrderService
from app.tenancy.context import tenant_scope
from app.tenancy.models import MiraklConnection, PlentyConnection, Tenant


ALLOWED_TENANT_STATUSES = ("active", "trial")


async def _active_pairs(db) -> List[tuple[Tenant, MiraklConnection, PlentyConnection]]:
    """Return one row per (tenant, mirakl_conn, plenty_conn). Each tenant
    can have N Mirakl connections but is assumed to have ONE active Plenty
    connection (see ARCHITECTURE_SAAS.md §A6)."""
    res = await db.execute(
        select(Tenant).where(Tenant.status.in_(ALLOWED_TENANT_STATUSES))
    )
    tenants = list(res.scalars().all())

    pairs: list = []
    for t in tenants:
        mres = await db.execute(
            select(MiraklConnection).where(
                MiraklConnection.tenant_id == t.id,
                MiraklConnection.active.is_(True),
            )
        )
        miraklis = list(mres.scalars().all())
        pres = await db.execute(
            select(PlentyConnection).where(
                PlentyConnection.tenant_id == t.id,
                PlentyConnection.active.is_(True),
            )
        )
        plentis = list(pres.scalars().all())
        if not plentis:
            logger.warning("orchestrator.no_plenty_connection", tenant_id=str(t.id))
            continue
        plenty = plentis[0]
        for m in miraklis:
            pairs.append((t, m, plenty))
    return pairs


async def import_orders_for_all(settings: Settings) -> Dict[str, int]:
    """Run import_new_orders() for every active (tenant, mirakl_conn) pair.

    Quota-block: if a tenant is over quota, skipped + alerted (handled in
    check_and_block_if_exceeded). Counter is incremented inside
    OrderService._import_single_order — we tap into that via a callback
    pattern: post-loop we increment by `imported` count atomically.
    """
    totals: Dict[str, int] = {"imported": 0, "skipped": 0, "errors": 0,
                              "tenants_processed": 0, "tenants_blocked": 0}
    async with db_session() as db:
        pairs = await _active_pairs(db)
        for tenant, mirakl_conn, plenty_conn in pairs:
            async with tenant_scope(db, tenant.id):
                blocked = await check_and_block_if_exceeded(db, tenant)
                if blocked:
                    totals["tenants_blocked"] += 1
                    continue
                totals["tenants_processed"] += 1
                try:
                    async with MiraklClient.from_connection(settings, mirakl_conn) as mirakl:
                        async with PlentyOneClient.from_connection(settings, plenty_conn) as plenty:
                            svc = OrderService(db, mirakl, plenty, settings,
                                               tenant_id=tenant.id,
                                               mirakl_connection_id=mirakl_conn.id)
                            counts = await svc.import_new_orders()
                    for k, v in counts.items():
                        totals[k] = totals.get(k, 0) + v
                    if counts.get("imported", 0):
                        await increment_usage(db, tenant.id, counts["imported"])
                    await audit(db, actor="system", action="orchestrator.import",
                                tenant_id=tenant.id,
                                entity="mirakl_connection",
                                entity_id=str(mirakl_conn.id),
                                payload={"counts": counts})
                    mirakl_conn.last_poll_ok = True
                    mirakl_conn.consecutive_failures = 0
                except Exception as exc:
                    mirakl_conn.last_poll_ok = False
                    mirakl_conn.consecutive_failures += 1
                    logger.error("orchestrator.import.failed",
                                 tenant_id=str(tenant.id),
                                 connection=mirakl_conn.label,
                                 error=str(exc))
                    db.add(mirakl_conn)
    logger.info("orchestrator.import.done", **totals)
    return totals


async def confirm_orders_for_all(settings: Settings) -> Dict[str, int]:
    totals: Dict[str, int] = {"confirmed": 0, "errors": 0}
    async with db_session() as db:
        pairs = await _active_pairs(db)
        for tenant, mirakl_conn, plenty_conn in pairs:
            async with tenant_scope(db, tenant.id):
                try:
                    async with MiraklClient.from_connection(settings, mirakl_conn) as mirakl:
                        async with PlentyOneClient.from_connection(settings, plenty_conn) as plenty:
                            svc = OrderService(db, mirakl, plenty, settings,
                                               tenant_id=tenant.id,
                                               mirakl_connection_id=mirakl_conn.id)
                            counts = await svc.confirm_orders()
                    for k, v in counts.items():
                        totals[k] = totals.get(k, 0) + v
                except Exception as exc:
                    logger.error("orchestrator.confirm.failed",
                                 tenant_id=str(tenant.id), error=str(exc))
    return totals


async def ship_pending_for_all(settings: Settings) -> Dict[str, int]:
    """Fallback poller for tenants without a configured Plenty webhook."""
    totals: Dict[str, int] = {"shipped": 0, "pending": 0, "errors": 0}
    async with db_session() as db:
        pairs = await _active_pairs(db)
        for tenant, mirakl_conn, plenty_conn in pairs:
            async with tenant_scope(db, tenant.id):
                try:
                    async with MiraklClient.from_connection(settings, mirakl_conn) as mirakl:
                        async with PlentyOneClient.from_connection(settings, plenty_conn) as plenty:
                            svc = OrderService(db, mirakl, plenty, settings,
                                               tenant_id=tenant.id,
                                               mirakl_connection_id=mirakl_conn.id)
                            counts = await svc.ship_orders()
                    for k, v in counts.items():
                        totals[k] = totals.get(k, 0) + v
                except Exception as exc:
                    logger.error("orchestrator.ship.failed",
                                 tenant_id=str(tenant.id), error=str(exc))
    return totals


async def sync_inventory_for_all(settings: Settings) -> Dict[str, Any]:
    totals = {"ok": 0, "errors": 0, "tenants": 0}
    async with db_session() as db:
        pairs = await _active_pairs(db)
        for tenant, mirakl_conn, plenty_conn in pairs:
            async with tenant_scope(db, tenant.id):
                try:
                    async with MiraklClient.from_connection(settings, mirakl_conn) as mirakl:
                        async with PlentyOneClient.from_connection(settings, plenty_conn) as plenty:
                            svc = InventoryService(db, mirakl, plenty, settings,
                                                   tenant_id=tenant.id,
                                                   mirakl_connection_id=mirakl_conn.id)
                            res = await svc.sync_stock()
                    totals["ok"] += res.success_count
                    totals["errors"] += res.error_count
                    totals["tenants"] += 1
                except Exception as exc:
                    logger.error("orchestrator.inventory.failed",
                                 tenant_id=str(tenant.id), error=str(exc))
    return totals
