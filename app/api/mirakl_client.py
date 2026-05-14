"""
Async Mirakl Marketplace API client for Douglas seller integration.

Endpoints used:
  GET  /api/orders          (OR11 – list orders)
  PUT  /api/orders/{id}/accept   (OR21/OR01 – accept order lines)
  PUT  /api/orders/{id}/ship     (OR24 – ship order with tracking)
  POST /api/offers/imports  (OF01 – update offer inventory)
  GET  /api/offers/imports/{id}  (OF02 – check import status)

Auth: API key in Authorization header.
Rate limits: poll orders max once/min; prefer every 5 min.
429 responses include Retry-After header — tenacity handles this.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.api.schemas import (
    BatchResult,
    MiraklAddress,
    MiraklOrder,
    MiraklOrderLine,
    OfferUpdate,
)
from app.config import Settings
from app.core.logging import logger


# Raised only on HTTP 429 so tenacity can target it specifically
class RateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


class MiraklClient:
    """
    Async context-manager client for Mirakl.

    Usage:
        async with MiraklClient(settings) as client:
            orders = await client.get_orders(status="WAITING_ACCEPTANCE")
    """

    def __init__(self, settings: Settings,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 shop_id: Optional[int] = None) -> None:
        self._settings = settings
        # SaaS overrides — when supplied, used in __aenter__ instead of
        # the legacy single-tenant settings. Pre-existing single-tenant
        # call sites (`MiraklClient(settings)`) keep working.
        self._override_base_url = base_url
        self._override_api_key = api_key
        self._override_shop_id = shop_id
        self._client: Optional[httpx.AsyncClient] = None

    @classmethod
    def from_connection(cls, settings: Settings, conn) -> "MiraklClient":
        """Build a client bound to a specific MiraklConnection row.
        `conn` is an app.tenancy.models.MiraklConnection — kept duck-typed
        to avoid a circular import."""
        from app.tenancy.crypto import decrypt
        return cls(
            settings,
            base_url=conn.base_url,
            api_key=decrypt(conn.api_key_enc),
            shop_id=conn.shop_id,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MiraklClient":
        base = self._override_base_url or self._settings.mirakl_base_url
        key = self._override_api_key or self._settings.mirakl_api_key
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        return self

    @property
    def shop_id(self) -> int:
        return (self._override_shop_id
                if self._override_shop_id is not None
                else self._settings.mirakl_shop_id)

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("MiraklClient must be used as async context manager")
        return self._client

    def _check_rate_limit(self, response: httpx.Response) -> None:
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise RateLimitError(retry_after=retry_after)

    def _raise_for_status(self, response: httpx.Response) -> None:
        self._check_rate_limit(response)
        response.raise_for_status()

    # ------------------------------------------------------------------
    # Public interface  (contract: these exact signatures must be kept)
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def get_orders(
        self,
        status: str = "WAITING_ACCEPTANCE",
        limit: int = 50,
        offset: int = 0,
    ) -> List[MiraklOrder]:
        """
        OR11 – List orders filtered by state.
        Mirakl paginates via offset/max parameters.
        Returns parsed MiraklOrder list.
        """
        params: Dict[str, Any] = {
            "order_state_codes": status,
            "max": limit,
            "start": offset,
        }
        if self.shop_id:
            params["shop_id"] = self.shop_id

        logger.info("mirakl.get_orders", status=status, limit=limit, offset=offset)
        resp = await self._http.get("/api/orders", params=params)
        self._raise_for_status(resp)

        data = resp.json()
        return [self._parse_order(o) for o in data.get("orders", [])]

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def accept_order(self, order_id: str) -> bool:
        """
        OR21 – Accept all order lines in an order.
        Returns True on success (204), False on failure.
        """
        if self._settings.dry_run:
            logger.info("mirakl.accept_order.dry_run", order_id=order_id)
            return True

        # Fetch the order first to get line IDs
        params: Dict[str, Any] = {}
        if self.shop_id:
            params["shop_id"] = self.shop_id

        detail_resp = await self._http.get(f"/api/orders/{order_id}", params=params)
        self._raise_for_status(detail_resp)
        order_data = detail_resp.json()

        order_lines = [
            {"id": line["order_line_id"], "accepted": True}
            for line in order_data.get("order_lines", [])
        ]

        payload = {"order_lines": order_lines}
        resp = await self._http.put(
            f"/api/orders/{order_id}/accept",
            json=payload,
            params=params,
        )
        self._raise_for_status(resp)
        logger.info("mirakl.accept_order.ok", order_id=order_id)
        return resp.status_code in (200, 204)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def ship_order(
        self,
        order_id: str,
        tracking_number: str,
        carrier: str,
    ) -> bool:
        """
        OR24 – Mark order as shipped with tracking info.
        Carrier codes are normalized to Douglas format before sending.
        """
        from app.api.schemas import CARRIER_MAP

        if self._settings.dry_run:
            logger.info(
                "mirakl.ship_order.dry_run",
                order_id=order_id,
                tracking=tracking_number,
                carrier=carrier,
            )
            return True

        normalized_carrier = CARRIER_MAP.get(carrier.upper(), carrier.lower())
        params: Dict[str, Any] = {}
        if self.shop_id:
            params["shop_id"] = self.shop_id

        payload = {
            "carrier_code": normalized_carrier,
            "tracking_number": tracking_number,
        }

        resp = await self._http.put(
            f"/api/orders/{order_id}/ship",
            json=payload,
            params=params,
        )
        self._raise_for_status(resp)
        logger.info(
            "mirakl.ship_order.ok",
            order_id=order_id,
            tracking=tracking_number,
            carrier=normalized_carrier,
        )
        return resp.status_code in (200, 204)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def update_offers(self, offers: List[OfferUpdate]) -> BatchResult:
        """
        OF01 + OF02 – Submit inventory update CSV/JSON to Mirakl and poll
        until the import completes or fails.

        Decision: We use the async import flow (OF01 → poll OF02) because
        Douglas volumes can be large and synchronous endpoints aren't offered.
        """
        if self._settings.dry_run:
            logger.info("mirakl.update_offers.dry_run", count=len(offers))
            return BatchResult(success_count=len(offers), error_count=0)

        # Build CSV payload (Mirakl OF01 expects CSV format for offer imports)
        csv_lines = ["sku;quantity"]
        for offer in offers:
            csv_lines.append(f"{offer.sku};{offer.quantity}")
        csv_body = "\n".join(csv_lines)

        params: Dict[str, Any] = {}
        if self.shop_id:
            params["shop_id"] = self.shop_id

        import_resp = await self._http.post(
            "/api/offers/imports",
            content=csv_body.encode(),
            headers={"Content-Type": "text/csv"},
            params=params,
        )
        self._raise_for_status(import_resp)
        import_id = import_resp.json().get("import_id")

        # Poll OF02 until complete (max 10 polls, 30s apart)
        for attempt in range(10):
            await asyncio.sleep(30)
            status_resp = await self._http.get(
                f"/api/offers/imports/{import_id}",
                params=params,
            )
            self._raise_for_status(status_resp)
            status_data = status_resp.json()
            state = status_data.get("import_status", "PENDING")

            if state == "COMPLETE":
                lines_ok = status_data.get("lines_read", 0)
                lines_err = status_data.get("lines_in_error", 0)
                logger.info(
                    "mirakl.update_offers.complete",
                    import_id=import_id,
                    ok=lines_ok,
                    errors=lines_err,
                )
                return BatchResult(success_count=lines_ok, error_count=lines_err)

            if state in ("FAILED", "CANCELED"):
                logger.error("mirakl.update_offers.failed", state=state)
                return BatchResult(success_count=0, error_count=len(offers))

        logger.warning("mirakl.update_offers.timeout", import_id=import_id)
        return BatchResult(success_count=0, error_count=len(offers))

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_address(raw: Dict[str, Any]) -> MiraklAddress:
        return MiraklAddress(
            firstname=raw.get("firstname", ""),
            lastname=raw.get("lastname", ""),
            company=raw.get("company"),
            street1=raw.get("street1", ""),
            street2=raw.get("street2"),
            zip_code=raw.get("zip_code", ""),
            city=raw.get("city", ""),
            country_iso_code=raw.get("country_iso_code", "DE"),
            phone=raw.get("phone"),
        )

    @classmethod
    def _parse_order(cls, raw: Dict[str, Any]) -> MiraklOrder:
        customer = raw.get("customer", {})
        shipping_addr = cls._parse_address(raw.get("shipping_address", {}))
        billing_addr = cls._parse_address(raw.get("billing_address", {}))

        lines = [
            MiraklOrderLine(
                order_line_id=line["order_line_id"],
                offer_id=str(line.get("offer_id", "")),
                offer_sku=line.get("offer_sku", ""),
                quantity=int(line.get("quantity", 1)),
                price=Decimal(str(line.get("price", "0"))),
                shipping_price=Decimal(str(line.get("shipping_price", "0"))),
                title=line.get("product_title", ""),
            )
            for line in raw.get("order_lines", [])
        ]

        return MiraklOrder(
            order_id=raw["order_id"],
            commercial_id=raw.get("commercial_id", ""),
            customer_id=customer.get("customer_id", ""),
            customer_email=customer.get("email", "noreply@example.com"),
            shipping_address=shipping_addr,
            billing_address=billing_addr,
            order_lines=lines,
            total_price=Decimal(str(raw.get("total_price", "0"))),
            currency_iso_code=raw.get("currency_iso_code", "EUR"),
            shipping_carrier=raw.get("shipping_company"),
            shipping_tracking=raw.get("shipping_tracking"),
            raw_data=raw,
        )
