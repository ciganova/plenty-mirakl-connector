#!/usr/bin/env python3
"""
CSV import script for sku_mapping table.

CSV format (header required):
  mirakl_sku,plenty_variant_id,plenty_sku,ean

Usage:
  python scripts/import_sku_mapping.py --file mapping.csv [--dry-run]

Behavior:
  - UPSERT: existing SKUs are updated, new ones inserted
  - Validates that plenty_variant_id is a positive integer
  - Reports counts at the end
"""
import argparse
import asyncio
import csv
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.models.database import AsyncSessionLocal
from app.models.tables import SKUMapping


async def import_mappings(csv_path: str, dry_run: bool = False) -> None:
    records = []
    errors = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # Line 2 = first data row
            try:
                mirakl_sku = row["mirakl_sku"].strip()
                plenty_variant_id = int(row["plenty_variant_id"].strip())
                if not mirakl_sku or plenty_variant_id <= 0:
                    raise ValueError("mirakl_sku empty or plenty_variant_id not positive")

                records.append({
                    "mirakl_sku": mirakl_sku,
                    "plenty_variant_id": plenty_variant_id,
                    "plenty_sku": row.get("plenty_sku", "").strip() or None,
                    "ean": row.get("ean", "").strip() or None,
                    "is_active": True,
                })
            except (KeyError, ValueError) as exc:
                errors.append(f"Line {i}: {exc}")

    if errors:
        print(f"Validation errors ({len(errors)}):")
        for err in errors:
            print(f"  {err}")
        if not dry_run:
            sys.exit(1)

    print(f"Records to import: {len(records)}")

    if dry_run:
        print("[DRY RUN] No changes written.")
        return

    async with AsyncSessionLocal() as session:
        stmt = insert(SKUMapping).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["mirakl_sku"],
            set_={
                "plenty_variant_id": stmt.excluded.plenty_variant_id,
                "plenty_sku": stmt.excluded.plenty_sku,
                "ean": stmt.excluded.ean,
                "is_active": stmt.excluded.is_active,
            },
        )
        await session.execute(stmt)
        await session.commit()

    print(f"Done. Upserted {len(records)} SKU mappings.")


def main():
    parser = argparse.ArgumentParser(description="Import SKU mappings from CSV")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, no DB writes")
    args = parser.parse_args()

    asyncio.run(import_mappings(args.file, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
