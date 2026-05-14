"""FastAPI dependencies for tenant-scoped + admin-only routes."""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import verify_api_key
from app.config import Settings, get_settings
from app.models.database import get_db
from app.tenancy.context import set_current_tenant
from app.tenancy.models import Tenant


ALLOWED_STATUSES = {"active", "trial"}


async def current_tenant_dep(
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    """Resolve the X-Api-Key header to a Tenant. 401 if missing/invalid,
    402 if subscription inactive."""
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="missing X-Api-Key")
    # Linear scan over active tenants. Fine up to ~10k tenants; index on
    # api_key_hash limits the rows. For larger fleets, switch to a
    # prefix-table lookup.
    res = await db.execute(select(Tenant).where(Tenant.api_key_hash.isnot(None)))
    for t in res.scalars().all():
        if verify_api_key(x_api_key, t.api_key_hash or ""):
            if t.status not in ALLOWED_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=f"subscription inactive: {t.status}",
                )
            set_current_tenant(t.id)
            return t
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="invalid X-Api-Key")


async def admin_only_dep(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> bool:
    expected = settings.admin_api_key
    if not expected or expected == "admin-changeme-set-via-env":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="admin api key not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="admin only")
    return True
