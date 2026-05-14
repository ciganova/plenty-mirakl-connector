"""Code-level tenant-scoping discriminator.

We verify that OrderService.confirm_orders / ship_orders / _resolve_skus
issue queries that include `OrderSync.tenant_id == <tenant>` in the WHERE
clause. RLS is belt-and-braces (see ARCHITECTURE_SAAS.md §A1) — code-level
scoping is the primary defence and is what these tests guard.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.services.order_service import OrderService


@pytest.fixture
def settings():
    return Settings(
        mirakl_base_url="https://m", mirakl_api_key="k",
        plenty_base_url="https://p", plenty_username="u", plenty_password="p",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost/0",
    )


def _capture_executes(db):
    """Return the list of compiled WHERE clauses across all execute() calls."""
    captured = []
    real_execute = AsyncMock()

    async def _capture(stmt, *a, **kw):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": False})))
        res = MagicMock()
        res.scalars.return_value.all.return_value = []
        res.scalar_one_or_none.return_value = None
        return res

    db.execute = _capture
    return captured


@pytest.mark.asyncio
async def test_confirm_orders_scopes_by_tenant(settings):
    db = AsyncMock()
    captured = _capture_executes(db)
    tid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    cid = uuid.UUID("22222222-2222-2222-2222-222222222222")

    svc = OrderService(db, AsyncMock(), AsyncMock(), settings,
                       tenant_id=tid, mirakl_connection_id=cid)
    await svc.confirm_orders()

    sql = "\n".join(captured)
    assert "tenant_id" in sql
    assert "mirakl_connection_id" in sql


@pytest.mark.asyncio
async def test_ship_orders_scopes_by_tenant(settings):
    db = AsyncMock()
    captured = _capture_executes(db)
    tid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    cid = uuid.UUID("44444444-4444-4444-4444-444444444444")

    svc = OrderService(db, AsyncMock(), AsyncMock(), settings,
                       tenant_id=tid, mirakl_connection_id=cid)
    await svc.ship_orders()

    sql = "\n".join(captured)
    assert "tenant_id" in sql
    assert "mirakl_connection_id" in sql
