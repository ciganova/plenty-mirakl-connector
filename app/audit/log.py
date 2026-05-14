"""Audit logging — writes to both structlog AND audit_log table.

The DB row is the durable record (visible in /panel). The structlog line
is for ops tooling (jq pipelines, log aggregation).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.tenancy.models import AuditLog


async def audit(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    tenant_id: Optional[uuid.UUID | str] = None,
    entity: Optional[str] = None,
    entity_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    row = AuditLog(
        tenant_id=uuid.UUID(str(tenant_id)) if tenant_id else None,
        actor=actor,
        action=action,
        entity=entity,
        entity_id=entity_id,
        payload=payload,
    )
    db.add(row)
    # caller flushes; we don't await flush here so this stays cheap.
    logger.info("audit", actor=actor, action=action,
                tenant_id=str(tenant_id) if tenant_id else None,
                entity=entity, entity_id=entity_id)
