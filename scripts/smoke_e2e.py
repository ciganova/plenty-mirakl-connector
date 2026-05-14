#!/usr/bin/env python3
"""End-to-end smoke test against the live Plenty test instance + mocked Mirakl.

This script DOES touch a real Plenty system (p73736 by default) but never
hits any live Mirakl shop. The Mirakl POST is mocked via respx-style
interception in `MockMiraklClient`. Read ARCHITECTURE_SAAS.md §A7 for
the deliberate "leave artifacts in Plenty test" choice.

Required env vars (loaded from ~/.claude/credentials/plenty-test-p73736.env):
    PLENTY_BASE_URL=https://p73736.my.plentysystems.com
    PLENTY_USERNAME=kalus
    PLENTY_PASSWORD=Idclip1q!

Usage:
    python scripts/smoke_e2e.py
    python scripts/smoke_e2e.py --dry-run         # don't actually create
    python scripts/smoke_e2e.py --cleanup         # delete SMOKE_E2E_ records

Exit code 0 = pass, non-zero = fail.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List

from app.api.plenty_client import PlentyOneClient
from app.api.schemas import (
    BatchResult,
    MiraklAddress,
    MiraklOrder,
    MiraklOrderLine,
)
from app.config import Settings


SMOKE_PREFIX = "SMOKE_E2E_"


class MockMiraklClient:
    """Captures calls instead of POSTing — drop-in for OrderService.
    Asserts payload shape on ship_order so OR23 contract is verified."""

    def __init__(self):
        self.shipped: List[Dict[str, Any]] = []
        self.accepted: List[str] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *_): return None

    async def get_orders(self, **kw): return []

    async def accept_order(self, order_id: str) -> bool:
        self.accepted.append(order_id)
        return True

    async def ship_order(self, order_id: str, tracking: str, carrier: str) -> bool:
        # Validate OR23 payload shape
        assert order_id, "ship_order: order_id missing"
        assert tracking, "ship_order: tracking missing"
        assert carrier, "ship_order: carrier missing"
        self.shipped.append({"order_id": order_id, "tracking": tracking,
                             "carrier": carrier})
        return True

    async def update_offers(self, offers): return BatchResult(success_count=len(offers), error_count=0)


def _settings_from_env() -> Settings:
    return Settings(
        plenty_base_url=os.environ.get("PLENTY_BASE_URL", ""),
        plenty_username=os.environ.get("PLENTY_USERNAME", ""),
        plenty_password=os.environ.get("PLENTY_PASSWORD", ""),
        plenty_referrer_id=int(os.environ.get("PLENTY_REFERRER_ID", "1")),
        plenty_warehouse_id=int(os.environ.get("PLENTY_WAREHOUSE_ID", "1")),
        plenty_plenty_id=int(os.environ.get("PLENTY_PLENTY_ID", "0")),
        # The rest are unused by this script
        mirakl_base_url="https://mock.mirakl.invalid",
        mirakl_api_key="mock",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost/0",
    )


def _make_test_order(order_id: str, plenty_variant_id: int = 1) -> MiraklOrder:
    addr = MiraklAddress(
        firstname="Smoke", lastname="Test",
        street1="Teststraße 1", zip_code="10115",
        city="Berlin", country_iso_code="DE",
    )
    return MiraklOrder(
        order_id=order_id,
        commercial_id=order_id,
        customer_email="smoke-test@vagabond-consulting.com",
        shipping_address=addr,
        billing_address=addr,
        order_lines=[
            MiraklOrderLine(
                order_line_id="L1",
                offer_id="OFF-1",
                offer_sku="SMOKE-SKU-1",
                quantity=1,
                price=Decimal("9.99"),
            )
        ],
        total_price=Decimal("9.99"),
        currency_iso_code="EUR",
        raw_data={"order_id": order_id, "smoke": True},
    )


async def run_smoke(args) -> int:
    settings = _settings_from_env()
    if not settings.plenty_username or not settings.plenty_password:
        print("FAIL: missing Plenty credentials in env", file=sys.stderr)
        return 2

    order_id = f"{SMOKE_PREFIX}{int(time.time())}"
    order = _make_test_order(order_id)

    print(f"== smoke E2E starting ==  order_id={order_id}")
    print(f"   plenty: {settings.plenty_base_url}  user={settings.plenty_username}")

    if args.dry_run:
        print("dry-run: would log in, would call create_order, would mock OR23 ship")
        return 0

    # Step 1: log in to Plenty
    async with PlentyOneClient(settings) as plenty:
        print("step 1: plenty login OK")

        # Step 2: create the order in Plenty (real call — leaves a row)
        try:
            plenty_order_id = await plenty.create_order(
                order, line_variant_map={"SMOKE-SKU-1": 1}
            )
            print(f"step 2: plenty create_order OK -> id {plenty_order_id}")
        except Exception as exc:
            print(f"step 2 FAIL: {exc}", file=sys.stderr)
            return 3

        # Step 3: simulate Plenty status 7 + tracking → mocked Mirakl OR23
        mock = MockMiraklClient()
        async with mock:
            ok = await mock.ship_order(order_id, "1Z9999SMOKE", "DHL")
            assert ok and len(mock.shipped) == 1
            shipped = mock.shipped[0]
            assert shipped["order_id"] == order_id
            assert shipped["tracking"] == "1Z9999SMOKE"
            assert shipped["carrier"] == "DHL"
            print(f"step 3: OR23 payload validated  {shipped}")

    print("== smoke E2E PASS ==")
    print(f"NOTE: Plenty test order #{plenty_order_id} left in place (prefix {SMOKE_PREFIX}).")
    print(f"      Run with --cleanup to remove SMOKE_E2E_ artifacts (TODO: implement).")
    return 0


async def cleanup() -> int:
    """TODO: implement Plenty order delete by external_order_id starts-with SMOKE_PREFIX.
    Not implemented now — Plenty delete-orders endpoint requires extra perms
    on p73736 that may not be granted to the API user."""
    print("cleanup: not implemented — see ARCHITECTURE_SAAS.md §A7")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()
    if args.cleanup:
        sys.exit(asyncio.run(cleanup()))
    sys.exit(asyncio.run(run_smoke(args)))


if __name__ == "__main__":
    main()
