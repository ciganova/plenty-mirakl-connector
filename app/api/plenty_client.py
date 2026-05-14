"""
Async PlentyONE REST API v2 client.

Endpoints used:
  POST /rest/login                                  – Obtain Bearer token
  POST /rest/orders                                 – Create order
  GET  /rest/orders/{id}/shipping/packages          – Get tracking info
  GET  /rest/items/variations?barcode={ean}         – EAN lookup (SKU fallback)

Auth: Username + Password → Bearer token, expires in 24h.
We auto-refresh the token before expiry.

Rate limits: PlentyONE limits concurrent sessions to 3.
We use a single shared client with connection pooling.
"""
from __future__ import annotations

import asyncio
import time
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
    MiraklOrder,
    PlentyAddress,
    PlentyAmount,
    PlentyOrderItem,
    TrackingInfo,
)
from app.config import Settings
from app.core.logging import logger

# PlentyONE address relation type IDs
ADDR_TYPE_BILLING = 1
ADDR_TYPE_DELIVERY = 2


class PlentyOneClient:
    """
    Async context-manager client for PlentyONE REST API v2.

    Usage:
        async with PlentyOneClient(settings) as client:
            order_id = await client.create_order(order_data)
    """

    def __init__(self, settings: Settings,
                 base_url: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 referrer_id: Optional[int] = None,
                 warehouse_id: Optional[int] = None,
                 plenty_id: Optional[int] = None) -> None:
        self._settings = settings
        self._override_base_url = base_url
        self._override_username = username
        self._override_password = password
        self._override_referrer = referrer_id
        self._override_warehouse = warehouse_id
        self._override_plenty_id = plenty_id
        self._client: Optional[httpx.AsyncClient] = None
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_connection(cls, settings: Settings, conn) -> "PlentyOneClient":
        """Build a client bound to a specific PlentyConnection row."""
        from app.tenancy.crypto import decrypt
        return cls(
            settings,
            base_url=conn.base_url,
            username=conn.username,
            password=decrypt(conn.password_enc),
            referrer_id=conn.referrer_id,
            warehouse_id=conn.warehouse_id,
            plenty_id=conn.plenty_id,
        )

    @property
    def base_url(self) -> str:
        return self._override_base_url or self._settings.plenty_base_url

    @property
    def username(self) -> str:
        return self._override_username or self._settings.plenty_username

    @property
    def password(self) -> str:
        return self._override_password or self._settings.plenty_password

    @property
    def referrer_id(self) -> int:
        return (self._override_referrer
                if self._override_referrer is not None
                else self._settings.plenty_referrer_id)

    @property
    def warehouse_id(self) -> int:
        return (self._override_warehouse
                if self._override_warehouse is not None
                else self._settings.plenty_warehouse_id)

    @property
    def plenty_id(self) -> int:
        return (self._override_plenty_id
                if self._override_plenty_id is not None
                else self._settings.plenty_plenty_id)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PlentyOneClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        await self._ensure_token()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> None:
        """Obtain or refresh Bearer token if needed (expires 5 min before actual expiry)."""
        async with self._token_lock:
            if self._access_token and time.time() < (self._token_expires_at - 300):
                return
            await self._login()

    async def _login(self) -> None:
        if self._client is None:
            raise RuntimeError("PlentyOneClient must be used as async context manager")

        logger.info("plenty.login")
        resp = await self._client.post(
            "/rest/login",
            json={
                "username": self.username,
                "password": self.password,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 86400)
        self._token_expires_at = time.time() + expires_in
        logger.info("plenty.login.ok", expires_in=expires_in)

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("PlentyOneClient must be used as async context manager")
        return self._client

    # ------------------------------------------------------------------
    # Public interface (contract: these exact signatures must be kept)
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def create_order(self, mirakl_order: MiraklOrder, line_variant_map: Dict[str, int]) -> int:
        """
        POST /rest/orders – Create a sales order in PlentyONE.
        Returns the new PlentyONE order ID.

        Order type 1 = Sales order (documentation confirmed; spec said 3 but
        that is Returns — see DECISIONS.md for rationale).
        """
        await self._ensure_token()

        if self._settings.dry_run:
            logger.info("plenty.create_order.dry_run", mirakl_order_id=mirakl_order.order_id)
            return -1

        billing_addr = self._build_plenty_address(mirakl_order.billing_address)
        shipping_addr = self._build_plenty_address(mirakl_order.shipping_address)

        items = []
        for line in mirakl_order.order_lines:
            variant_id = line_variant_map.get(line.offer_sku, 0)
            items.append({
                "typeId": 1,  # Variation
                "quantity": line.quantity,
                "orderItemName": line.title or line.offer_sku,
                "itemVariationId": variant_id,
                "countryVatId": 1,
                "vatRate": 19.0,
                "amounts": [{
                    "isSystemCurrency": True,
                    "currency": mirakl_order.currency_iso_code,
                    "exchangeRate": 1.0,
                    "priceOriginalGross": float(line.price),
                }],
            })

        payload = {
            "typeId": 1,           # Sales order
            "statusId": 5.0,       # Release for dispatch (Douglas pays immediately)
            "referrerId": self.referrer_id,
            "plentyId": self.plenty_id,
            "relations": [
                {
                    "referenceType": "warehouse",
                    "referenceId": self.warehouse_id,
                    "relation": "sender",
                },
            ],
            "addresses": [
                {**billing_addr, "typeId": ADDR_TYPE_BILLING},
                {**shipping_addr, "typeId": ADDR_TYPE_DELIVERY},
            ],
            "items": items,
            "properties": [
                # Store external Mirakl order ID for traceability
                {"typeId": 7, "value": mirakl_order.order_id},  # 7 = external order ID
            ],
        }

        resp = await self._http.post("/rest/orders", json=payload, headers=self._auth_headers())
        resp.raise_for_status()
        data = resp.json()
        plenty_order_id = data["id"]
        logger.info(
            "plenty.create_order.ok",
            mirakl_order_id=mirakl_order.order_id,
            plenty_order_id=plenty_order_id,
        )
        return plenty_order_id

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def get_tracking(self, order_id: int) -> Optional[TrackingInfo]:
        """
        GET /rest/orders/{id}/shipping/packages – Retrieve tracking info.
        Returns None if no packages/tracking exists yet.
        """
        await self._ensure_token()

        resp = await self._http.get(
            f"/rest/orders/{order_id}/shipping/packages",
            headers=self._auth_headers(),
        )

        if resp.status_code == 404:
            return None

        resp.raise_for_status()
        packages = resp.json()

        if not packages:
            return None

        # Take first package (most orders have one package)
        pkg = packages[0] if isinstance(packages, list) else packages
        tracking_number = pkg.get("packageNumber") or pkg.get("tracking_number")
        if not tracking_number:
            return None

        return TrackingInfo(
            package_id=pkg.get("id", 0),
            order_id=order_id,
            tracking_number=tracking_number,
            carrier_name=pkg.get("shippingServiceProvider", {}).get("name")
            if isinstance(pkg.get("shippingServiceProvider"), dict)
            else None,
        )

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def find_customer(self, email: str) -> Optional[int]:
        """
        GET /rest/accounts/contacts – Search for existing contact by email.
        Returns contact ID or None.
        """
        await self._ensure_token()

        resp = await self._http.get(
            "/rest/accounts/contacts",
            params={"email": email},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("entries", [])
        if entries:
            return entries[0]["id"]
        return None

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True,
    )
    async def create_customer(self, mirakl_order: MiraklOrder) -> int:
        """
        POST /rest/accounts/contacts – Create a new contact in PlentyONE.
        Returns the new contact ID.
        """
        await self._ensure_token()

        if self._settings.dry_run:
            logger.info("plenty.create_customer.dry_run", email=mirakl_order.customer_email)
            return -1

        addr = mirakl_order.shipping_address
        payload = {
            "email": mirakl_order.customer_email,
            "firstName": addr.firstname,
            "lastName": addr.lastname,
            "typeId": 1,  # Customer
        }

        resp = await self._http.post(
            "/rest/accounts/contacts",
            json=payload,
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data["id"]

    async def find_variant_by_ean(self, ean: str) -> Optional[int]:
        """
        EAN fallback lookup: search PlentyONE variations by barcode.
        Returns variation ID or None.
        """
        await self._ensure_token()

        resp = await self._http.get(
            "/rest/items/variations",
            params={"barcode": ean},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("entries", [])
        if entries:
            return entries[0]["id"]
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_plenty_address(addr: Any) -> Dict[str, Any]:
        """Convert MiraklAddress to PlentyONE address dict."""
        # Split street/house number heuristically (German format: "Musterstr. 12")
        street_parts = addr.street1.rsplit(" ", 1) if addr.street1 else ["", ""]
        street = street_parts[0] if len(street_parts) > 1 else addr.street1
        house_no = street_parts[1] if len(street_parts) > 1 else ""

        return {
            "name1": addr.company or "",
            "name2": addr.firstname,
            "name3": addr.lastname,
            "address1": street,
            "address2": house_no + (" " + addr.street2 if addr.street2 else ""),
            "postalCode": addr.zip_code,
            "town": addr.city,
            "countryId": 1,  # Germany – hardcoded for Douglas DE; extend for AT/CH
        }
