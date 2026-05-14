"""
Unit tests for MiraklClient — all HTTP calls are mocked via respx.
"""
import pytest
import respx
from httpx import Response

from app.api.mirakl_client import MiraklClient, RateLimitError
from app.api.schemas import OfferUpdate
from app.config import Settings


@pytest.fixture
def settings():
    return Settings(
        mirakl_base_url="https://test.mirakl.net",
        mirakl_api_key="test-key",
        mirakl_shop_id=0,
        plenty_base_url="https://test.plentymarkets.com",
        plenty_username="u",
        plenty_password="p",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost:6379/0",
        dry_run=False,
    )


SAMPLE_ORDER = {
    "order_id": "MRK-001",
    "commercial_id": "COMM-001",
    "total_price": "49.99",
    "currency_iso_code": "EUR",
    "customer": {"customer_id": "C1", "email": "test@example.com"},
    "shipping_address": {
        "firstname": "Max",
        "lastname": "Mustermann",
        "street1": "Musterstraße 1",
        "zip_code": "10115",
        "city": "Berlin",
        "country_iso_code": "DE",
    },
    "billing_address": {
        "firstname": "Max",
        "lastname": "Mustermann",
        "street1": "Musterstraße 1",
        "zip_code": "10115",
        "city": "Berlin",
        "country_iso_code": "DE",
    },
    "order_lines": [
        {
            "order_line_id": "LINE-001",
            "offer_id": "OFF-001",
            "offer_sku": "SKU-ABC",
            "quantity": 2,
            "price": "24.99",
            "shipping_price": "0",
            "product_title": "Test Product",
        }
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_get_orders_success(settings):
    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(200, json={"orders": [SAMPLE_ORDER], "total_count": 1})
    )

    async with MiraklClient(settings) as client:
        orders = await client.get_orders()

    assert len(orders) == 1
    assert orders[0].order_id == "MRK-001"
    assert orders[0].customer_email == "test@example.com"
    assert len(orders[0].order_lines) == 1
    assert orders[0].order_lines[0].offer_sku == "SKU-ABC"


@pytest.mark.asyncio
@respx.mock
async def test_get_orders_empty(settings):
    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(200, json={"orders": [], "total_count": 0})
    )

    async with MiraklClient(settings) as client:
        orders = await client.get_orders()

    assert orders == []


@pytest.mark.asyncio
@respx.mock
async def test_accept_order_success(settings):
    # Mock order detail fetch
    respx.get("https://test.mirakl.net/api/orders/MRK-001").mock(
        return_value=Response(200, json={"order_lines": [{"order_line_id": "LINE-001"}]})
    )
    # Mock accept call
    respx.put("https://test.mirakl.net/api/orders/MRK-001/accept").mock(
        return_value=Response(204)
    )

    async with MiraklClient(settings) as client:
        result = await client.accept_order("MRK-001")

    assert result is True


@pytest.mark.asyncio
@respx.mock
async def test_ship_order_success(settings):
    respx.put("https://test.mirakl.net/api/orders/MRK-001/ship").mock(
        return_value=Response(204)
    )

    async with MiraklClient(settings) as client:
        result = await client.ship_order("MRK-001", "1Z999AA10123456784", "DHL")

    assert result is True


@pytest.mark.asyncio
@respx.mock
async def test_ship_order_carrier_normalization(settings):
    """DHL should be normalized to dhl_germany for Douglas."""
    captured_requests = []

    def capture(request, route):
        captured_requests.append(request)
        return Response(204)

    respx.put("https://test.mirakl.net/api/orders/MRK-001/ship").mock(side_effect=capture)

    async with MiraklClient(settings) as client:
        await client.ship_order("MRK-001", "TRACK123", "DHL")

    assert len(captured_requests) == 1
    import json
    body = json.loads(captured_requests[0].content)
    assert body["carrier_code"] == "dhl_germany"


@pytest.mark.asyncio
@respx.mock
async def test_dry_run_skip_accept(settings):
    settings.dry_run = True

    async with MiraklClient(settings) as client:
        result = await client.accept_order("MRK-001")

    assert result is True
    # No HTTP calls should have been made
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_raises(settings):
    respx.get("https://test.mirakl.net/api/orders").mock(
        return_value=Response(429, headers={"Retry-After": "60"})
    )

    async with MiraklClient(settings) as client:
        # tenacity will retry 3x and reraise
        with pytest.raises(RateLimitError):
            await client.get_orders()
