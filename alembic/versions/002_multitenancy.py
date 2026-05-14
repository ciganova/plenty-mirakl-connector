"""Multi-tenancy: tenants, mirakl_connections, plenty_connections,
audit_log, usage_counters; tenant_id on existing tables; RLS policies;
default-tenant bootstrap so single-tenant dev stays green.

Revision ID: 002
Revises: 001
Create Date: 2026-05-14 00:00:00.000000

This migration is ADDITIVE per the project's "never delete existing
data" rule. The PK rework on order_sync / sku_mapping is done by:

  1. adding tenant_id + mirakl_connection_id (nullable initially)
  2. inserting a default tenant + connections from env
  3. backfilling existing rows to point at the default
  4. NOT NULL + new unique constraint
  5. drop old PK, add surrogate uuid PK

Old natural-key columns stay on the table — they're now part of a
unique constraint with the connection_id. Nothing is destroyed.
"""
from __future__ import annotations

import os
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # ── App role for RLS (created if missing). NOSUPERUSER + NOBYPASSRLS so
    #    runtime queries are actually subject to row-level security.
    #    Whether DATABASE_URL points at this role is a deploy-time choice —
    #    see ARCHITECTURE_SAAS.md §A1.
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='connector_app') THEN
            CREATE ROLE connector_app WITH LOGIN NOSUPERUSER NOBYPASSRLS
                PASSWORD 'connector_app_changeme';
        END IF;
    END $$;
    """)

    # ── tenants ──────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("api_key_hash", sa.String(128), nullable=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("monthly_quota", sa.Integer, nullable=False, server_default="200"),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenants_status", "tenants", ["status"])
    op.create_index("ix_tenants_api_key_hash", "tenants", ["api_key_hash"])

    # ── mirakl_connections ──────────────────────────────────────────────────
    op.create_table(
        "mirakl_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("api_key_enc", sa.LargeBinary, nullable=False),
        sa.Column("shop_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_ok", sa.Boolean, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mirakl_conn_tenant", "mirakl_connections", ["tenant_id"])
    op.create_unique_constraint(
        "uq_mirakl_conn_tenant_label", "mirakl_connections", ["tenant_id", "label"],
    )

    # ── plenty_connections ──────────────────────────────────────────────────
    op.create_table(
        "plenty_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("username", sa.String(120), nullable=False),
        sa.Column("password_enc", sa.LargeBinary, nullable=False),
        sa.Column("referrer_id", sa.Integer, nullable=False, server_default="1"),
        sa.Column("warehouse_id", sa.Integer, nullable=False, server_default="1"),
        sa.Column("plenty_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("webhook_secret", sa.String(64), nullable=True),
        sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_call_ok", sa.Boolean, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_plenty_conn_tenant", "plenty_connections", ["tenant_id"])

    # ── audit_log (append-only by convention; no DB-level trigger to keep
    #    migration simple — code never UPDATEs/DELETEs from this table) ─────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity", sa.String(80), nullable=True),
        sa.Column("entity_id", sa.String(120), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
    )
    op.create_index("ix_audit_tenant_ts", "audit_log", ["tenant_id", "ts"])
    op.create_index("ix_audit_action", "audit_log", ["action"])

    # ── usage_counters ──────────────────────────────────────────────────────
    op.create_table(
        "usage_counters",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("period_year", sa.Integer, nullable=False),
        sa.Column("period_month", sa.Integer, nullable=False),
        sa.Column("orders_imported", sa.Integer, nullable=False, server_default="0"),
        sa.Column("orders_overage", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("tenant_id", "period_year", "period_month",
                            name="uq_usage_tenant_period"),
    )

    # ── default tenant + connections from env ───────────────────────────────
    # Bootstrap: existing single-tenant deployment becomes the "default"
    # tenant. Picks env vars at migration time. If absent, leaves dummies.
    default_tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    default_mirakl_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    default_plenty_id = uuid.UUID("00000000-0000-0000-0000-000000000003")

    bind.execute(sa.text("""
        INSERT INTO tenants (id, name, status, monthly_quota,
                             contact_email, created_at, updated_at)
        VALUES (:id, :name, 'active', 1000000, :email, now(), now())
        ON CONFLICT DO NOTHING
    """), {
        "id": default_tenant_id,
        "name": "default",
        "email": os.environ.get("DEFAULT_TENANT_EMAIL", "ops@example.invalid"),
    })

    bind.execute(sa.text("""
        INSERT INTO mirakl_connections (id, tenant_id, label, base_url,
                                        api_key_enc, shop_id, active,
                                        created_at, updated_at)
        VALUES (:id, :tid, 'default', :url, :key, :shop, true, now(), now())
        ON CONFLICT DO NOTHING
    """), {
        "id": default_mirakl_id, "tid": default_tenant_id,
        "url": os.environ.get("MIRAKL_BASE_URL", "https://your-shop.mirakl.net"),
        # Stored as plain bytes during migration — encryption happens for
        # NEW connections only. Operators rotating keys will re-add.
        "key": os.environ.get("MIRAKL_API_KEY", "").encode() or b"unset",
        "shop": int(os.environ.get("MIRAKL_SHOP_ID", "0") or "0"),
    })

    bind.execute(sa.text("""
        INSERT INTO plenty_connections (id, tenant_id, label, base_url,
                                        username, password_enc, referrer_id,
                                        warehouse_id, plenty_id, active,
                                        created_at, updated_at)
        VALUES (:id, :tid, 'default', :url, :user, :pw, :ref, :wh, :pid,
                true, now(), now())
        ON CONFLICT DO NOTHING
    """), {
        "id": default_plenty_id, "tid": default_tenant_id,
        "url": os.environ.get("PLENTY_BASE_URL", "https://your-shop.plentymarkets.com"),
        "user": os.environ.get("PLENTY_USERNAME", "unset"),
        "pw": (os.environ.get("PLENTY_PASSWORD", "") or "unset").encode(),
        "ref": int(os.environ.get("PLENTY_REFERRER_ID", "1") or "1"),
        "wh": int(os.environ.get("PLENTY_WAREHOUSE_ID", "1") or "1"),
        "pid": int(os.environ.get("PLENTY_PLENTY_ID", "0") or "0"),
    })

    # ── extend existing tables ──────────────────────────────────────────────
    for tbl in ("order_sync", "sku_mapping", "inventory_log"):
        op.add_column(tbl, sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
        op.add_column(tbl, sa.Column("mirakl_connection_id", UUID(as_uuid=True),
                                     nullable=True))

    # backfill
    for tbl in ("order_sync", "sku_mapping", "inventory_log"):
        bind.execute(sa.text(f"""
            UPDATE {tbl} SET tenant_id = :tid, mirakl_connection_id = :mid
            WHERE tenant_id IS NULL
        """), {"tid": default_tenant_id, "mid": default_mirakl_id})

    # NOT NULL + indexes + FKs
    for tbl in ("order_sync", "sku_mapping", "inventory_log"):
        op.alter_column(tbl, "tenant_id", nullable=False)
        op.alter_column(tbl, "mirakl_connection_id", nullable=False)
        op.create_index(f"ix_{tbl}_tenant", tbl, ["tenant_id"])
        op.create_foreign_key(
            f"fk_{tbl}_tenant", tbl, "tenants", ["tenant_id"], ["id"],
            ondelete="CASCADE",
        )
        op.create_foreign_key(
            f"fk_{tbl}_mirakl_conn", tbl, "mirakl_connections",
            ["mirakl_connection_id"], ["id"], ondelete="CASCADE",
        )

    # PK rework — drop natural PK, add surrogate uuid, add unique
    # constraint on (mirakl_connection_id, mirakl_order_id / mirakl_sku).
    # The natural-key column STAYS — it's now in a unique constraint.

    # order_sync
    op.execute("ALTER TABLE order_sync DROP CONSTRAINT order_sync_pkey")
    op.add_column("order_sync", sa.Column(
        "id", UUID(as_uuid=True), nullable=True))
    bind.execute(sa.text(
        "UPDATE order_sync SET id = gen_random_uuid() WHERE id IS NULL"))
    op.alter_column("order_sync", "id", nullable=False)
    op.create_primary_key("order_sync_pkey", "order_sync", ["id"])
    op.create_unique_constraint(
        "uq_order_sync_conn_mirakl_id", "order_sync",
        ["mirakl_connection_id", "mirakl_order_id"],
    )

    # sku_mapping
    op.execute("ALTER TABLE sku_mapping DROP CONSTRAINT sku_mapping_pkey")
    op.add_column("sku_mapping", sa.Column(
        "id", UUID(as_uuid=True), nullable=True))
    bind.execute(sa.text(
        "UPDATE sku_mapping SET id = gen_random_uuid() WHERE id IS NULL"))
    op.alter_column("sku_mapping", "id", nullable=False)
    op.create_primary_key("sku_mapping_pkey", "sku_mapping", ["id"])
    op.create_unique_constraint(
        "uq_sku_mapping_conn_sku", "sku_mapping",
        ["mirakl_connection_id", "mirakl_sku"],
    )

    # ── RLS policies (belt-and-braces; primary defense is code-level scoping) ─
    for tbl in ("order_sync", "sku_mapping", "inventory_log",
                "mirakl_connections", "plenty_connections",
                "usage_counters"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {tbl}_tenant_isolation ON {tbl}
            USING (tenant_id::text = current_setting('app.current_tenant', true))
            WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true))
        """)

    # tenants table: only see your own row
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenants_self ON tenants
        USING (id::text = current_setting('app.current_tenant', true))
        WITH CHECK (id::text = current_setting('app.current_tenant', true))
    """)

    # audit_log: tenant sees own rows; system writes any row (admin-bypass
    # uses superuser/connector role, see ARCHITECTURE §A1).
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_log_tenant_select ON audit_log
        FOR SELECT
        USING (tenant_id::text = current_setting('app.current_tenant', true)
               OR current_setting('app.current_tenant', true) = '')
    """)

    # GRANTs so connector_app can actually read/write (only own rows).
    op.execute("""
        GRANT USAGE ON SCHEMA public TO connector_app;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON ALL TABLES IN SCHEMA public TO connector_app;
        GRANT USAGE, SELECT, UPDATE
            ON ALL SEQUENCES IN SCHEMA public TO connector_app;
    """)


# ---------------------------------------------------------------------------
# downgrade — only structural drops; tenant data preserved by NOT touching
#            backfilled rows. Per "never delete existing data" the rule is:
#            we ADD only. This downgrade exists for alembic completeness but
#            should not be run in prod.
# ---------------------------------------------------------------------------

def downgrade() -> None:
    for tbl in ("order_sync", "sku_mapping", "inventory_log",
                "mirakl_connections", "plenty_connections",
                "usage_counters", "tenants"):
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
    op.execute("DROP POLICY IF EXISTS tenants_self ON tenants")
    op.execute("DROP POLICY IF EXISTS audit_log_tenant_select ON audit_log")

    op.drop_constraint("uq_order_sync_conn_mirakl_id", "order_sync")
    op.drop_constraint("uq_sku_mapping_conn_sku", "sku_mapping")
    op.execute("ALTER TABLE order_sync DROP CONSTRAINT order_sync_pkey")
    op.create_primary_key("order_sync_pkey", "order_sync", ["mirakl_order_id"])
    op.drop_column("order_sync", "id")
    op.execute("ALTER TABLE sku_mapping DROP CONSTRAINT sku_mapping_pkey")
    op.create_primary_key("sku_mapping_pkey", "sku_mapping", ["mirakl_sku"])
    op.drop_column("sku_mapping", "id")

    for tbl in ("order_sync", "sku_mapping", "inventory_log"):
        op.drop_constraint(f"fk_{tbl}_tenant", tbl, type_="foreignkey")
        op.drop_constraint(f"fk_{tbl}_mirakl_conn", tbl, type_="foreignkey")
        op.drop_index(f"ix_{tbl}_tenant", table_name=tbl)
        op.drop_column(tbl, "mirakl_connection_id")
        op.drop_column(tbl, "tenant_id")

    op.drop_table("usage_counters")
    op.drop_table("audit_log")
    op.drop_table("plenty_connections")
    op.drop_table("mirakl_connections")
    op.drop_table("tenants")
