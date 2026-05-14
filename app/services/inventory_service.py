"""
Inventory synchronization service.

Reads stock levels from PlentyONE and pushes them to Mirakl via OF01.
Only syncs active SKUs in the sku_mapping table.
No price sync (per spec: non-goal).
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.api.schemas import BatchResult, OfferUpdate
from app.config import Settings
from app.core.logging import logger
from app.models.tables import InventoryLog, SKUMapping


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_DEFAULT_MIRAKL_CONN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class InventoryService:
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

    async def sync_stock(self) -> BatchResult:
        """
        Full inventory sync cycle:
        1. Load all active SKU mappings
        2. For each, fetch stock from PlentyONE
        3. Push to Mirakl in one batch (OF01)
        4. Log results
        """
        result = await self._db.execute(
            select(SKUMapping).where(
                SKUMapping.tenant_id == self._tenant_id,
                SKUMapping.mirakl_connection_id == self._mirakl_connection_id,
                SKUMapping.is_active == True,  # noqa: E712
            )
        )
        mappings = list(result.scalars().all())

        if not mappings:
            logger.info("inventory_service.no_active_skus")
            return BatchResult(success_count=0, error_count=0)

        offers: list[OfferUpdate] = []
        for mapping in mappings:
            stock = await self._get_plenty_stock(mapping.plenty_variant_id)
            if stock is not None:
                offers.append(OfferUpdate(sku=mapping.mirakl_sku, quantity=stock))

        if not offers:
            logger.info("inventory_service.no_stock_data")
            return BatchResult(success_count=0, error_count=0)

        batch_result = await self._mirakl.update_offers(offers)

        # Log each push to inventory_log table
        for offer in offers:
            log_entry = InventoryLog(
                tenant_id=self._tenant_id,
                mirakl_connection_id=self._mirakl_connection_id,
                variant_id=next(
                    m.plenty_variant_id for m in mappings if m.mirakl_sku == offer.sku
                ),
                mirakl_sku=offer.sku,
                quantity_sent=offer.quantity,
                mirakl_response={"success_count": batch_result.success_count},
            )
            self._db.add(log_entry)

        await self._db.flush()
        logger.info(
            "inventory_service.sync_complete",
            pushed=len(offers),
            ok=batch_result.success_count,
            errors=batch_result.error_count,
        )
        return batch_result

    async def _get_plenty_stock(self, variant_id: int) -> int | None:
        """
        GET /rest/stockmanagement/warehouses/stocks — fetch current stock.
        Returns None if request fails (skip that SKU, don't abort the batch).
        """
        try:
            resp = await self._plenty._http.get(
                "/rest/stockmanagement/warehouses/stocks",
                params={
                    "variationId": variant_id,
                    "warehouseId": self._settings.plenty_warehouse_id,
                },
                headers=self._plenty._auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", [])
            if entries:
                return int(entries[0].get("netStock", 0))
            return 0
        except Exception as exc:
            logger.warning("inventory_service.stock_fetch_failed", variant_id=variant_id, error=str(exc))
            return None
