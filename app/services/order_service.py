"""
Order orchestration service — the core business logic layer.

Implements the full state machine:
  Mirakl NEW → IMPORTED (PlentyONE) → CONFIRMED (Mirakl accept) → SHIPPED (Mirakl ship)

SKU Resolution strategy (per spec):
  1. Exact match via sku_mapping table
  2. EAN fallback via PlentyONE variation search
  3. Quarantine: status=ERROR, flag SKU_NOT_FOUND

All methods are idempotent: re-running on already-processed orders is safe.
"""
from __future__ import annotations

import json
import uuid
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.api.schemas import MiraklOrder
from app.config import Settings
from app.core.logging import logger
from app.models.tables import OrderSync, SKUMapping


# Default-tenant UUIDs from migration 002. Used when OrderService is
# constructed without explicit tenant args (the legacy single-tenant
# path — keeps existing tests passing without a fixture refactor).
_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_DEFAULT_MIRAKL_CONN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class OrderService:
    def __init__(
        self,
        db: AsyncSession,
        mirakl: MiraklClient,
        plenty: PlentyOneClient,
        settings: Settings,
        tenant_id: Optional[uuid.UUID] = None,
        mirakl_connection_id: Optional[uuid.UUID] = None,
    ) -> None:
        self._db = db
        self._mirakl = mirakl
        self._plenty = plenty
        self._settings = settings
        self._tenant_id = tenant_id or _DEFAULT_TENANT_ID
        self._mirakl_connection_id = mirakl_connection_id or _DEFAULT_MIRAKL_CONN_ID

    # ------------------------------------------------------------------
    # Step 1: Import new orders from Mirakl into PlentyONE
    # ------------------------------------------------------------------

    async def import_new_orders(self) -> Dict[str, int]:
        """
        Poll Mirakl for WAITING_ACCEPTANCE orders and create them in PlentyONE.
        Returns counts: {"imported": N, "skipped": N, "errors": N}
        """
        counts = {"imported": 0, "skipped": 0, "errors": 0}
        orders = await self._mirakl.get_orders(status="WAITING_ACCEPTANCE")

        for order in orders:
            result = await self._import_single_order(order)
            counts[result] += 1

        logger.info("order_service.import_complete", **counts)
        return counts

    async def _find_order(self, mirakl_order_id: str) -> Optional[OrderSync]:
        """Lookup by (mirakl_connection_id, mirakl_order_id) — the new
        unique constraint since migration 002."""
        res = await self._db.execute(
            select(OrderSync).where(
                OrderSync.tenant_id == self._tenant_id,
                OrderSync.mirakl_connection_id == self._mirakl_connection_id,
                OrderSync.mirakl_order_id == mirakl_order_id,
            )
        )
        return res.scalar_one_or_none()

    def _new_order_record(self, mirakl_order_id: str) -> OrderSync:
        return OrderSync(
            tenant_id=self._tenant_id,
            mirakl_connection_id=self._mirakl_connection_id,
            mirakl_order_id=mirakl_order_id,
        )

    async def _import_single_order(self, order: MiraklOrder) -> str:
        """
        Process one order. Returns "imported", "skipped", or "errors".
        Idempotent: if the order already exists in DB, skip it.
        """
        # Idempotency check
        existing = await self._find_order(order.order_id)
        if existing and existing.status not in ("NEW", "ERROR"):
            logger.debug("order_service.skip_existing", order_id=order.order_id, status=existing.status)
            return "skipped"

        # Resolve SKUs → PlentyONE variant IDs
        variant_map, missing_skus = await self._resolve_skus(order)

        if missing_skus:
            await self._mark_error(
                order,
                f"SKU_NOT_FOUND: {', '.join(missing_skus)}",
            )
            logger.error(
                "order_service.sku_not_found",
                order_id=order.order_id,
                missing=missing_skus,
            )
            return "errors"

        # Create order in PlentyONE
        try:
            plenty_order_id = await self._plenty.create_order(order, variant_map)
        except Exception as exc:
            await self._mark_error(order, f"PLENTY_CREATE_FAILED: {exc}")
            logger.error("order_service.plenty_create_failed", order_id=order.order_id, error=str(exc))
            return "errors"

        # Upsert DB record
        sync_record = existing or self._new_order_record(order.order_id)
        sync_record.plenty_order_id = plenty_order_id
        sync_record.status = "IMPORTED"
        sync_record.customer_email = order.customer_email
        sync_record.raw_json = order.raw_data
        sync_record.error_message = None
        sync_record.error_count = 0
        self._db.add(sync_record)
        await self._db.flush()

        logger.info(
            "order_service.imported",
            order_id=order.order_id,
            plenty_id=plenty_order_id,
        )
        return "imported"

    # ------------------------------------------------------------------
    # Step 2: Confirm (accept) imported orders in Mirakl
    # ------------------------------------------------------------------

    async def confirm_orders(self) -> Dict[str, int]:
        """
        For all IMPORTED orders, call Mirakl accept_order → set CONFIRMED.
        """
        counts = {"confirmed": 0, "errors": 0}

        result = await self._db.execute(
            select(OrderSync).where(
                OrderSync.tenant_id == self._tenant_id,
                OrderSync.mirakl_connection_id == self._mirakl_connection_id,
                OrderSync.status == "IMPORTED",
            )
        )
        records: List[OrderSync] = list(result.scalars().all())

        for record in records:
            try:
                ok = await self._mirakl.accept_order(record.mirakl_order_id)
                if ok:
                    record.status = "CONFIRMED"
                    self._db.add(record)
                    counts["confirmed"] += 1
                    logger.info("order_service.confirmed", order_id=record.mirakl_order_id)
                else:
                    counts["errors"] += 1
            except Exception as exc:
                await self._increment_error(record, f"ACCEPT_FAILED: {exc}")
                counts["errors"] += 1
                logger.error("order_service.confirm_failed", order_id=record.mirakl_order_id, error=str(exc))

        await self._db.flush()
        logger.info("order_service.confirm_complete", **counts)
        return counts

    # ------------------------------------------------------------------
    # Step 3: Ship orders — sync tracking from PlentyONE to Mirakl
    # ------------------------------------------------------------------

    async def ship_orders(self) -> Dict[str, int]:
        """
        For all CONFIRMED orders with a PlentyONE ID, check if tracking
        is available → call Mirakl ship_order → set SHIPPED.
        """
        counts = {"shipped": 0, "pending": 0, "errors": 0}

        result = await self._db.execute(
            select(OrderSync).where(
                OrderSync.tenant_id == self._tenant_id,
                OrderSync.mirakl_connection_id == self._mirakl_connection_id,
                OrderSync.status == "CONFIRMED",
                OrderSync.plenty_order_id.isnot(None),
            )
        )
        records: List[OrderSync] = list(result.scalars().all())

        for record in records:
            try:
                tracking = await self._plenty.get_tracking(record.plenty_order_id)
                if not tracking or not tracking.tracking_number:
                    counts["pending"] += 1
                    continue

                carrier = tracking.carrier_name or "DHL"  # Default carrier for Douglas
                ok = await self._mirakl.ship_order(
                    record.mirakl_order_id,
                    tracking.tracking_number,
                    carrier,
                )
                if ok:
                    record.status = "SHIPPED"
                    self._db.add(record)
                    counts["shipped"] += 1
                    logger.info(
                        "order_service.shipped",
                        order_id=record.mirakl_order_id,
                        tracking=tracking.tracking_number,
                    )
                else:
                    counts["errors"] += 1

            except Exception as exc:
                await self._increment_error(record, f"SHIP_FAILED: {exc}")
                counts["errors"] += 1
                logger.error("order_service.ship_failed", order_id=record.mirakl_order_id, error=str(exc))

        await self._db.flush()
        logger.info("order_service.ship_complete", **counts)
        return counts

    # ------------------------------------------------------------------
    # SKU resolution
    # ------------------------------------------------------------------

    async def _resolve_skus(
        self, order: MiraklOrder
    ) -> tuple[Dict[str, int], List[str]]:
        """
        Returns (variant_map, missing_skus).
        variant_map: {offer_sku: plenty_variant_id}
        missing_skus: list of SKUs that could not be resolved
        """
        variant_map: Dict[str, int] = {}
        missing: List[str] = []

        for line in order.order_lines:
            sku = line.offer_sku

            # 1. Exact match in mapping table (per connection)
            res = await self._db.execute(
                select(SKUMapping).where(
                    SKUMapping.tenant_id == self._tenant_id,
                    SKUMapping.mirakl_connection_id == self._mirakl_connection_id,
                    SKUMapping.mirakl_sku == sku,
                )
            )
            record = res.scalar_one_or_none()
            if record and record.is_active:
                variant_map[sku] = record.plenty_variant_id
                continue

            # 2. EAN fallback (if mapping has EAN but SKU key was wrong)
            if record and record.ean:
                variant_id = await self._plenty.find_variant_by_ean(record.ean)
                if variant_id:
                    variant_map[sku] = variant_id
                    continue

            # 3. Quarantine
            missing.append(sku)

        return variant_map, missing

    # ------------------------------------------------------------------
    # Error helpers
    # ------------------------------------------------------------------

    async def _mark_error(self, order: MiraklOrder, message: str) -> None:
        existing = await self._find_order(order.order_id)
        record = existing or self._new_order_record(order.order_id)
        record.status = "ERROR"
        record.error_message = message
        record.error_count = (record.error_count or 0) + 1
        record.raw_json = order.raw_data
        record.customer_email = order.customer_email
        self._db.add(record)
        await self._db.flush()

    async def _increment_error(self, record: OrderSync, message: str) -> None:
        record.error_count = (record.error_count or 0) + 1
        record.error_message = message
        self._db.add(record)
        await self._db.flush()
