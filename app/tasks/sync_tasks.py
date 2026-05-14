"""Celery task definitions — multi-tenant.

Each task delegates to app.services.orchestrator which fans out across
every active (tenant, mirakl_conn, plenty_conn) tuple. Single-tenant
behavior is preserved via the `default` tenant + connections seeded by
migration 002 from the legacy env vars.
"""
from __future__ import annotations

import asyncio

from app.billing.quota import monthly_reset
from app.config import get_settings
from app.core.logging import logger
from app.models.database import db_session
from app.services import orchestrator
from app.tasks.celery_app import celery_app


def _run(coro):
    """Run an async coroutine from a synchronous Celery task."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@celery_app.task(name="app.tasks.sync_tasks.task_import_orders", bind=True, max_retries=0)
def task_import_orders(self):
    settings = get_settings()
    try:
        totals = _run(orchestrator.import_orders_for_all(settings))
        logger.info("task.import_orders.done", **totals)
    except Exception as exc:
        logger.error("task.import_orders.failed", error=str(exc))


@celery_app.task(name="app.tasks.sync_tasks.task_confirm_orders", bind=True, max_retries=0)
def task_confirm_orders(self):
    settings = get_settings()
    try:
        totals = _run(orchestrator.confirm_orders_for_all(settings))
        logger.info("task.confirm_orders.done", **totals)
    except Exception as exc:
        logger.error("task.confirm_orders.failed", error=str(exc))


@celery_app.task(name="app.tasks.sync_tasks.task_sync_tracking", bind=True, max_retries=0)
def task_sync_tracking(self):
    settings = get_settings()
    try:
        totals = _run(orchestrator.ship_pending_for_all(settings))
        logger.info("task.sync_tracking.done", **totals)
    except Exception as exc:
        logger.error("task.sync_tracking.failed", error=str(exc))


@celery_app.task(name="app.tasks.sync_tasks.task_sync_inventory", bind=True, max_retries=0)
def task_sync_inventory(self):
    settings = get_settings()
    try:
        totals = _run(orchestrator.sync_inventory_for_all(settings))
        logger.info("task.sync_inventory.done", **totals)
    except Exception as exc:
        logger.error("task.sync_inventory.failed", error=str(exc))


@celery_app.task(name="app.tasks.sync_tasks.task_monthly_reset", bind=True, max_retries=0)
def task_monthly_reset(self):
    """Run on day 1 at 00:05 UTC — reset usage counters + revert
    quota_exceeded → active."""
    async def _inner():
        async with db_session() as db:
            return await monthly_reset(db)
    try:
        n = _run(_inner())
        logger.info("task.monthly_reset.done", tenants=n)
    except Exception as exc:
        logger.error("task.monthly_reset.failed", error=str(exc))
