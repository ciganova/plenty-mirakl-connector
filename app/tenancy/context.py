"""Async tenant context — ContextVar + asyncpg SET LOCAL helper for RLS.

Usage:
    async with tenant_scope(db, tenant_id):
        # all queries inside this block carry app.current_tenant=<uuid>
        ...

Outside this scope, RLS-enforced tables are invisible to non-superuser
roles. Inside, tenant_id-scoped queries are also enforced at the code
layer via filter_by(tenant_id=current_tenant_id()).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_tenant_var: ContextVar[Optional[str]] = ContextVar("current_tenant", default=None)


def current_tenant_id() -> Optional[str]:
    return _tenant_var.get()


def set_current_tenant(tenant_id: Optional[str | uuid.UUID]) -> None:
    if tenant_id is None:
        _tenant_var.set(None)
    else:
        _tenant_var.set(str(tenant_id))


@asynccontextmanager
async def tenant_scope(db: AsyncSession, tenant_id: str | uuid.UUID):
    """Set both the ContextVar (for code-level scoping) and the Postgres
    GUC `app.current_tenant` (for RLS) within a single transaction.

    Defense-in-depth: code MUST still filter every query by tenant_id —
    RLS only catches mistakes when the runtime DB role is non-superuser.
    """
    tid = str(tenant_id)
    token = _tenant_var.set(tid)
    try:
        # SET LOCAL only persists for the current transaction. asyncpg
        # parameterises with $1; SET LOCAL doesn't accept params, so
        # we use safe quoting (uuid → no escape concerns).
        # Validate uuid first to defang any injection.
        uuid.UUID(tid)
        await db.execute(text(f"SET LOCAL app.current_tenant = '{tid}'"))
        yield tid
    finally:
        _tenant_var.reset(token)
