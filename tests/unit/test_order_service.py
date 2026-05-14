"""Unit tests for OrderService — multi-tenant aware.

These tests use AsyncMock-mocked DB. The OrderService default-tenant
fallback (when no tenant_id is passed) makes them work against the
seeded `default` tenant, identical behavior to single-tenant code.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.schemas import MiraklAddress, MiraklOrder, MiraklOrderLine, TrackingInfo
from app.config import Settings
from app.models.tables import OrderSync, SKUMapping
from app.services.order_service import (
    OrderService,
    _DEFAULT_MIRAKL_CONN_ID,
    _DEFAULT_TENANT_ID,
)


@pytest.fixture
def settings():
    return Settings(
        mirakl_base_url="https://test.mirakl.net",
        mirakl_api_key="key",
        plenty_base_url="https://test.plenty.com",
        plenty_username="u",
        plenty_password="p",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost/0",
    )


@pytest.fixture
def sample_order():
    addr = MiraklAddress(firstname="Max", lastname="Mustermann",
                         street1="Musterstr. 1", zip_code="10115", city="Berlin")
    return MiraklOrder(
        order_id="MRK-001", customer_email="max@example.com",
        shipping_address=addr, billing_address=addr,
        order_lines=[MiraklOrderLine(order_line_id="L1", offer_id="OFF-1",
                                     offer_sku="SKU-A", quantity=1,
                                     price=Decimal("29.99"))],
        total_price=Decimal("29.99"), raw_data={"order_id": "MRK-001"},
    )


def _scalar_one_or_none_returning(value):
    """Build a fake Result whose .scalar_one_or_none() returns `value`."""
    res = MagicMock()
    res.scalar_one_or_none.return_value = value
    return res


def _scalars_all_returning(rows):
    res = MagicMock()
    res.scalars.return_value.all.return_value = rows
    return res


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    # By default execute returns no rows
    db.execute = AsyncMock(return_value=_scalar_one_or_none_returning(None))
    return db


@pytest.fixture
def mock_mirakl():
    client = AsyncMock()
    client.get_orders = AsyncMock(return_value=[])
    client.accept_order = AsyncMock(return_value=True)
    client.ship_order = AsyncMock(return_value=True)
    return client


@pytest.fixture
def mock_plenty():
    client = AsyncMock()
    client.create_order = AsyncMock(return_value=9999)
    client.get_tracking = AsyncMock(return_value=None)
    client.find_variant_by_ean = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_import_new_order_success(settings, sample_order, mock_db,
                                        mock_mirakl, mock_plenty):
    mock_mirakl.get_orders.return_value = [sample_order]
    sku_record = SKUMapping(
        tenant_id=_DEFAULT_TENANT_ID,
        mirakl_connection_id=_DEFAULT_MIRAKL_CONN_ID,
        mirakl_sku="SKU-A", plenty_variant_id=42, is_active=True,
    )
    # Three execute calls inside _import_single_order:
    #   1. _find_order  -> None
    #   2. _resolve_skus -> sku_record
    # And the inner sequence is called per order.
    mock_db.execute.side_effect = [
        _scalar_one_or_none_returning(None),       # _find_order
        _scalar_one_or_none_returning(sku_record), # _resolve_skus
    ]

    svc = OrderService(mock_db, mock_mirakl, mock_plenty, settings)
    counts = await svc.import_new_orders()
    assert counts["imported"] == 1
    assert counts["errors"] == 0
    mock_plenty.create_order.assert_called_once()
    mock_db.add.assert_called()


@pytest.mark.asyncio
async def test_import_idempotent(settings, sample_order, mock_db,
                                 mock_mirakl, mock_plenty):
    mock_mirakl.get_orders.return_value = [sample_order]
    existing = OrderSync(
        tenant_id=_DEFAULT_TENANT_ID,
        mirakl_connection_id=_DEFAULT_MIRAKL_CONN_ID,
        mirakl_order_id="MRK-001", status="CONFIRMED",
    )
    mock_db.execute.side_effect = [_scalar_one_or_none_returning(existing)]

    svc = OrderService(mock_db, mock_mirakl, mock_plenty, settings)
    counts = await svc.import_new_orders()
    assert counts["skipped"] == 1
    mock_plenty.create_order.assert_not_called()


@pytest.mark.asyncio
async def test_sku_not_found_quarantine(settings, sample_order, mock_db,
                                        mock_mirakl, mock_plenty):
    mock_mirakl.get_orders.return_value = [sample_order]
    # _find_order -> None, _resolve_skus -> None, _mark_error _find_order -> None
    mock_db.execute.side_effect = [
        _scalar_one_or_none_returning(None),
        _scalar_one_or_none_returning(None),
        _scalar_one_or_none_returning(None),
    ]
    mock_plenty.find_variant_by_ean.return_value = None

    svc = OrderService(mock_db, mock_mirakl, mock_plenty, settings)
    counts = await svc.import_new_orders()
    assert counts["errors"] == 1
    mock_db.add.assert_called()
    mock_plenty.create_order.assert_not_called()


@pytest.mark.asyncio
async def test_ship_orders_with_tracking(settings, mock_db, mock_mirakl, mock_plenty):
    confirmed_record = OrderSync(
        tenant_id=_DEFAULT_TENANT_ID,
        mirakl_connection_id=_DEFAULT_MIRAKL_CONN_ID,
        mirakl_order_id="MRK-001", plenty_order_id=9999, status="CONFIRMED",
    )
    mock_db.execute = AsyncMock(return_value=_scalars_all_returning([confirmed_record]))
    mock_plenty.get_tracking.return_value = TrackingInfo(
        package_id=1, order_id=9999, tracking_number="1Z999AA10123456784",
        carrier_name="DHL",
    )
    mock_mirakl.ship_order.return_value = True

    svc = OrderService(mock_db, mock_mirakl, mock_plenty, settings)
    counts = await svc.ship_orders()
    assert counts["shipped"] == 1
    mock_mirakl.ship_order.assert_called_once_with(
        "MRK-001", "1Z999AA10123456784", "DHL"
    )
    assert confirmed_record.status == "SHIPPED"


@pytest.mark.asyncio
async def test_ship_orders_no_tracking_yet(settings, mock_db, mock_mirakl, mock_plenty):
    confirmed_record = OrderSync(
        tenant_id=_DEFAULT_TENANT_ID,
        mirakl_connection_id=_DEFAULT_MIRAKL_CONN_ID,
        mirakl_order_id="MRK-001", plenty_order_id=9999, status="CONFIRMED",
    )
    mock_db.execute = AsyncMock(return_value=_scalars_all_returning([confirmed_record]))
    mock_plenty.get_tracking.return_value = None

    svc = OrderService(mock_db, mock_mirakl, mock_plenty, settings)
    counts = await svc.ship_orders()
    assert counts["pending"] == 1
    assert counts["shipped"] == 0
    mock_mirakl.ship_order.assert_not_called()
