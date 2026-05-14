"""
SQLAlchemy ORM table definitions.

Single-tenant tables (sku_mapping, order_sync, inventory_log) gained
`tenant_id` + `mirakl_connection_id` in migration 002. Original natural-key
columns stay (now part of unique constraints with connection_id).

New SaaS tables (Tenant, MiraklConnection, PlentyConnection, AuditLog,
UsageCounter) live in app.tenancy.models — kept separate so the existing
single-tenant code path can import what it always imported.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Re-export new tenancy models so `from app.models.tables import Tenant` works
# for old import sites (and for alembic's autogenerate sweeps).
from app.tenancy.models import (  # noqa: F401
    AuditLog,
    MiraklConnection,
    PlentyConnection,
    Tenant,
    UsageCounter,
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class SKUMapping(Base):
    __tablename__ = "sku_mapping"

    # surrogate PK (migration 002)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mirakl_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mirakl_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    mirakl_sku: Mapped[str] = mapped_column(String(255), nullable=False)
    plenty_variant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plenty_sku: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ean: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_sku_mapping_ean", "ean"),
        Index("ix_sku_mapping_variant", "plenty_variant_id"),
        UniqueConstraint("mirakl_connection_id", "mirakl_sku",
                         name="uq_sku_mapping_conn_sku"),
    )


class OrderSync(Base):
    """
    State machine: NEW → IMPORTED → CONFIRMED → SHIPPED. ERROR is terminal.

    Surrogate uuid PK since migration 002. Natural key (mirakl_order_id) is
    now unique within (mirakl_connection_id, mirakl_order_id) — see
    ARCHITECTURE_SAAS.md §2.
    """
    __tablename__ = "order_sync"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mirakl_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mirakl_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    mirakl_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    plenty_order_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NEW", index=True
    )
    customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_order_sync_plenty_id", "plenty_order_id"),
        Index("ix_order_sync_status", "status"),
        UniqueConstraint("mirakl_connection_id", "mirakl_order_id",
                         name="uq_order_sync_conn_mirakl_id"),
    )


class InventoryLog(Base):
    __tablename__ = "inventory_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    mirakl_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mirakl_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    variant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mirakl_sku: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    mirakl_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_inventory_log_variant", "variant_id"),)
