"""Initial schema: sku_mapping, order_sync, inventory_log

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── sku_mapping ──────────────────────────────────────────────────────────
    op.create_table(
        "sku_mapping",
        sa.Column("mirakl_sku", sa.String(255), primary_key=True),
        sa.Column("plenty_variant_id", sa.BigInteger(), nullable=False),
        sa.Column("plenty_sku", sa.String(255), nullable=True),
        sa.Column("ean", sa.String(20), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sku_mapping_ean", "sku_mapping", ["ean"])
    op.create_index("ix_sku_mapping_variant", "sku_mapping", ["plenty_variant_id"])

    # ── order_sync ───────────────────────────────────────────────────────────
    op.create_table(
        "order_sync",
        sa.Column("mirakl_order_id", sa.String(100), primary_key=True),
        sa.Column("plenty_order_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="NEW",
        ),
        sa.Column("customer_email", sa.String(255), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_order_sync_plenty_id", "order_sync", ["plenty_order_id"])
    op.create_index("ix_order_sync_status", "order_sync", ["status"])

    # ── inventory_log ────────────────────────────────────────────────────────
    op.create_table(
        "inventory_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("mirakl_sku", sa.String(255), nullable=False),
        sa.Column("quantity_sent", sa.Integer(), nullable=False),
        sa.Column("mirakl_response", JSONB(), nullable=True),
    )
    op.create_index("ix_inventory_log_variant", "inventory_log", ["variant_id"])
    op.create_index("ix_inventory_log_timestamp", "inventory_log", ["timestamp"])


def downgrade() -> None:
    op.drop_table("inventory_log")
    op.drop_table("order_sync")
    op.drop_table("sku_mapping")
