"""
Unified Pydantic v2 schemas — the single source of truth for all
data shapes flowing between Mirakl, PlentyONE, and the DB.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Shared value types
# ---------------------------------------------------------------------------

OrderStatus = Literal["NEW", "IMPORTED", "CONFIRMED", "SHIPPED", "ERROR"]

# Douglas-specific carrier code mapping (Mirakl → Douglas seller portal codes)
CARRIER_MAP: Dict[str, str] = {
    "DHL": "dhl_germany",
    "DPD": "dpd_de",
    "GLS": "gls_de",
    "HERMES": "hermes_de",
    "HERMES_DE": "hermes_de",
    # fallback: pass through as-is
}


# ---------------------------------------------------------------------------
# Mirakl data shapes  (deserialized from Mirakl API responses)
# ---------------------------------------------------------------------------

class MiraklAddress(BaseModel):
    firstname: str = ""
    lastname: str = ""
    company: Optional[str] = None
    street1: str = ""
    street2: Optional[str] = None
    zip_code: str = ""
    city: str = ""
    country_iso_code: str = "DE"
    phone: Optional[str] = None


class MiraklOrderLine(BaseModel):
    order_line_id: str
    offer_id: str
    offer_sku: str
    quantity: int
    price: Decimal
    shipping_price: Decimal = Decimal("0")
    title: str = ""


class MiraklOrder(BaseModel):
    order_id: str
    commercial_id: str = ""
    customer_id: str = ""
    customer_email: EmailStr
    shipping_address: MiraklAddress
    billing_address: MiraklAddress
    order_lines: List[MiraklOrderLine]
    total_price: Decimal
    currency_iso_code: str = "EUR"
    shipping_carrier: Optional[str] = None
    shipping_tracking: Optional[str] = None
    raw_data: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# PlentyONE data shapes  (sent to / received from PlentyONE API)
# ---------------------------------------------------------------------------

class PlentyAmount(BaseModel):
    is_system_currency: bool = True
    currency: str = "EUR"
    exchange_rate: float = 1.0
    price_original_gross: Decimal


class PlentyOrderItem(BaseModel):
    type_id: int = 1           # 1 = Variation (standard product)
    quantity: int
    order_item_name: str
    item_variation_id: int
    vat_rate: float = 19.0
    amounts: List[PlentyAmount]


class PlentyAddress(BaseModel):
    name1: str = ""            # Company
    name2: str = ""            # First name
    name3: str = ""            # Last name
    address1: str = ""         # Street
    address2: str = ""         # House number / additional
    postal_code: str = ""
    town: str = ""
    country_id: int = 1        # 1 = Germany


class TrackingInfo(BaseModel):
    package_id: int
    order_id: int
    tracking_number: Optional[str] = None
    carrier_name: Optional[str] = None


# ---------------------------------------------------------------------------
# SKU Mapping
# ---------------------------------------------------------------------------

class SKUMappingRecord(BaseModel):
    mirakl_sku: str
    plenty_variant_id: int
    plenty_sku: Optional[str] = None
    ean: Optional[str] = None
    is_active: bool = True


# ---------------------------------------------------------------------------
# Offer/Inventory update  (Mirakl OF01 payload item)
# ---------------------------------------------------------------------------

class OfferUpdate(BaseModel):
    sku: str
    quantity: int
    # Douglas does not use price sync (non-goal per spec)


class BatchResult(BaseModel):
    success_count: int
    error_count: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
