"""
Integration tests — require real PostgreSQL but mock the external HTTP APIs.

Run with: pytest tests/integration/ -v -m integration
Requires: a reachable Postgres at TEST_DATABASE_URL (default postgres:secret).

These exercise the multi-tenant schema; the legacy single-tenant happy-paths
that lived here previously now run against the seeded `default` tenant.
"""
import pytest
import respx

pytestmark = pytest.mark.integration
from decimal import Decimal
from httpx import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.api.schemas import MiraklAddress, MiraklOrder, MiraklOrderLine
from app.config import Settings
from app.models.database import Base
from app.models.tables import OrderSync, SKUMapping
from app.services.order_service import OrderService


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def db_url():
    """
    Provide a test DB URL.
    In CI, use testcontainers. Locally, set TEST_DATABASE_URL env var.
    Falls back to in-memory SQLite (incompatible with JSONB — skip on SQLite).
    """
    import os
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://connector:secret@localhost:5432/connector_test",
    )


@pytest.fixture
async def db_engine(db_url):
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    SessionLocal = async_sessionmaker(db_engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
        await session.rollback()


@pytest.fixture
def settings():
    return Settings(
        mirakl_base_url="https://test.mirakl.net",
        mirakl_api_key="test-key",
        mirakl_shop_id=0,
        plenty_base_url="https://test.plenty.com",
        plenty_username="user",
        plenty_password="pass",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost/0",
        dry_run=False,
        plenty_referrer_id=1,
        plenty_warehouse_id=1,
        plenty_plenty_id=1000,
    )


def make_order(order_id: str = "MRK-INT-001") -> MiraklOrder:
    addr = MiraklAddress(
        firstname="Anna",
        lastname="Schmidt",
        street1="Berliner Str. 42",
        zip_code="10115",
        city="Berlin",
    )
    return MiraklOrder(
        order_id=order_id,
        customer_email="anna@example.com",
        shipping_address=addr,
        billing_address=addr,
        order_lines=[
            MiraklOrderLine(
                order_line_id="L1",
                offer_id="O1",
                offer_sku="SKU-BEAUTY-001",
                quantity=1,
                price=Decimal("39.99"),
            )
        ],
        total_price=Decimal("39.99"),
        raw_data={"order_id": order_id},
    )


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_e2e_order_import_confirm_ship(db_session, settings):
    """
    Full E2E flow:
    1. Mirakl returns 1 NEW order
    2. Connector creates it in PlentyONE
    3. Status becomes IMPORTED, then CONFIRMED, then SHIPPED
    """
    order = make_order()

    # Insert SKU mapping
    sku_map = SKUMapping(mirakl_sku="SKU-BEAUTY-001", plenty_variant_id=777, is_active=True)
    db_session.add(sku_map)
    await db_session.flush()

    # ── Mock Mirakl ──────────────────────────────────────────────────────────
    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(200, json={
            "orders": [{
                "order_id": "MRK-INT-001",
                "commercial_id": "C001",
                "total_price": "39.99",
                "currency_iso_code": "EUR",
                "customer": {"customer_id": "CX", "email": "anna@example.com"},
                "shipping_address": {
                    "firstname": "Anna", "lastname": "Schmidt",
                    "street1": "Berliner Str. 42", "zip_code": "10115",
                    "city": "Berlin", "country_iso_code": "DE",
                },
                "billing_address": {
                    "firstname": "Anna", "lastname": "Schmidt",
                    "street1": "Berliner Str. 42", "zip_code": "10115",
                    "city": "Berlin", "country_iso_code": "DE",
                },
                "order_lines": [{
                    "order_line_id": "L1", "offer_id": "O1",
                    "offer_sku": "SKU-BEAUTY-001", "quantity": 1,
                    "price": "39.99", "shipping_price": "0",
                    "product_title": "Test Perfume",
                }],
            }],
            "total_count": 1,
        })
    )

    # ── Mock PlentyONE login ─────────────────────────────────────────────────
    respx.post("https://test.plenty.com/rest/login").mock(
        return_value=Response(200, json={"access_token": "test-token", "expires_in": 86400})
    )
    # Mock order creation
    respx.post("https://test.plenty.com/rest/orders").mock(
        return_value=Response(200, json={"id": 88888})
    )
    # Mock accept order
    respx.get("https://test.mirakl.net/api/orders/MRK-INT-001").mock(
        return_value=Response(200, json={"order_lines": [{"order_line_id": "L1"}]})
    )
    respx.put("https://test.mirakl.net/api/orders/MRK-INT-001/accept").mock(
        return_value=Response(204)
    )
    # Mock tracking
    respx.get("https://test.plenty.com/rest/orders/88888/shipping/packages").mock(
        return_value=Response(200, json=[{
            "id": 1, "packageNumber": "1Z999AA10123456784",
            "shippingServiceProvider": {"name": "DHL"},
        }])
    )
    respx.put("https://test.mirakl.net/api/orders/MRK-INT-001/ship").mock(
        return_value=Response(204)
    )

    async with MiraklClient(settings) as mirakl:
        async with PlentyOneClient(settings) as plenty:
            svc = OrderService(db_session, mirakl, plenty, settings)

            # Step 1: Import
            counts = await svc.import_new_orders()
            assert counts["imported"] == 1

            record = await db_session.get(OrderSync, "MRK-INT-001")
            assert record is not None
            assert record.status == "IMPORTED"
            assert record.plenty_order_id == 88888

            # Step 2: Confirm
            await svc.confirm_orders()
            await db_session.refresh(record)
            assert record.status == "CONFIRMED"

            # Step 3: Ship
            await svc.ship_orders()
            await db_session.refresh(record)
            assert record.status == "SHIPPED"


@pytest.mark.asyncio
@respx.mock
async def test_idempotency_duplicate_import(db_session, settings):
    """Importing same order twice results in only one DB record."""
    sku_map = SKUMapping(mirakl_sku="SKU-BEAUTY-001", plenty_variant_id=777, is_active=True)
    db_session.add(sku_map)
    await db_session.flush()

    order_json = {
        "order_id": "MRK-DUP-001",
        "commercial_id": "C002",
        "total_price": "19.99",
        "currency_iso_code": "EUR",
        "customer": {"customer_id": "CY", "email": "dup@example.com"},
        "shipping_address": {
            "firstname": "Test", "lastname": "User",
            "street1": "Str. 1", "zip_code": "10000",
            "city": "Hamburg", "country_iso_code": "DE",
        },
        "billing_address": {
            "firstname": "Test", "lastname": "User",
            "street1": "Str. 1", "zip_code": "10000",
            "city": "Hamburg", "country_iso_code": "DE",
        },
        "order_lines": [{
            "order_line_id": "L2", "offer_id": "O2",
            "offer_sku": "SKU-BEAUTY-001", "quantity": 1,
            "price": "19.99", "shipping_price": "0",
            "product_title": "Dup Product",
        }],
    }

    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(200, json={"orders": [order_json], "total_count": 1})
    )
    respx.post("https://test.plenty.com/rest/login").mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 86400})
    )
    respx.post("https://test.plenty.com/rest/orders").mock(
        return_value=Response(200, json={"id": 77777})
    )

    async with MiraklClient(settings) as mirakl:
        async with PlentyOneClient(settings) as plenty:
            svc = OrderService(db_session, mirakl, plenty, settings)

            counts1 = await svc.import_new_orders()
            assert counts1["imported"] == 1

            # Second import call — Mirakl still returns same order (e.g., accept not called yet)
            respx.get("https://test.mirakl.net/api/orders").mock(
                return_value=Response(200, json={"orders": [order_json], "total_count": 1})
            )
            counts2 = await svc.import_new_orders()
            assert counts2["skipped"] == 1

    # Verify only one DB record exists
    from sqlalchemy import select, func
    result = await db_session.execute(
        select(func.count(OrderSync.mirakl_order_id)).where(
            OrderSync.mirakl_order_id == "MRK-DUP-001"
        )
    )
    count = result.scalar()
    assert count == 1


@pytest.mark.asyncio
@respx.mock
async def test_sku_not_found_no_crash(db_session, settings):
    """Order with unmapped SKU ends in ERROR state without raising exception."""
    order_json = {
        "order_id": "MRK-NOSKU-001",
        "commercial_id": "C003",
        "total_price": "9.99",
        "currency_iso_code": "EUR",
        "customer": {"customer_id": "CZ", "email": "nosku@example.com"},
        "shipping_address": {
            "firstname": "SKU", "lastname": "Test",
            "street1": "Str. 2", "zip_code": "20000",
            "city": "München", "country_iso_code": "DE",
        },
        "billing_address": {
            "firstname": "SKU", "lastname": "Test",
            "street1": "Str. 2", "zip_code": "20000",
            "city": "München", "country_iso_code": "DE",
        },
        "order_lines": [{
            "order_line_id": "L3", "offer_id": "O3",
            "offer_sku": "SKU-UNKNOWN-XYZ",  # Not in mapping table
            "quantity": 1, "price": "9.99", "shipping_price": "0",
            "product_title": "Unknown Product",
        }],
    }

    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(200, json={"orders": [order_json], "total_count": 1})
    )
    respx.post("https://test.plenty.com/rest/login").mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 86400})
    )
    respx.get("https://test.plenty.com/rest/items/variations").mock(
        return_value=Response(200, json={"entries": []})
    )

    async with MiraklClient(settings) as mirakl:
        async with PlentyOneClient(settings) as plenty:
            svc = OrderService(db_session, mirakl, plenty, settings)
            counts = await svc.import_new_orders()

    assert counts["errors"] == 1
    record = await db_session.get(OrderSync, "MRK-NOSKU-001")
    assert record is not None
    assert record.status == "ERROR"
    assert "SKU_NOT_FOUND" in record.error_message
