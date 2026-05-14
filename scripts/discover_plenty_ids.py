#!/usr/bin/env python3
"""Walk a Plenty mandant and dump the lookup IDs needed for onboarding.

Usage:
    python scripts/discover_plenty_ids.py --conn-id <plenty_conn_uuid>
    python scripts/discover_plenty_ids.py --base-url ... --user ... --password ...

Prints (in order, prefixed with section header):
  * referrers      — id, name (marketplace channels)
  * warehouses     — id, name
  * order-statuses — id, name
  * payment-methods — id, name

Output is human-readable AND a JSON tail at the end so the operator can
pipe into jq to seed `mirakl-conn add`/`plenty-conn add` decisions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any, Dict

from app.api.plenty_client import PlentyOneClient
from app.config import get_settings
from app.models.database import db_session
from app.tenancy.models import PlentyConnection


async def _fetch(client, path, params=None):
    await client._ensure_token()
    r = await client._http.get(path, params=params or {}, headers=client._auth_headers())
    r.raise_for_status()
    return r.json()


async def discover(client) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        out["referrers"] = await _fetch(client, "/rest/orders/referrers")
    except Exception as exc:
        out["referrers"] = {"error": str(exc)}
    try:
        out["warehouses"] = await _fetch(client, "/rest/stockmanagement/warehouses")
    except Exception as exc:
        out["warehouses"] = {"error": str(exc)}
    try:
        out["order_statuses"] = await _fetch(client, "/rest/orders/statuses")
    except Exception as exc:
        out["order_statuses"] = {"error": str(exc)}
    try:
        out["payment_methods"] = await _fetch(client, "/rest/payments/methods")
    except Exception as exc:
        out["payment_methods"] = {"error": str(exc)}
    return out


def _print(section: str, data):
    print(f"\n=== {section} ===")
    if isinstance(data, dict) and "error" in data:
        print(f"  ERROR: {data['error']}")
        return
    rows = data if isinstance(data, list) else (data.get("entries") or [])
    for row in rows:
        rid = row.get("id") or row.get("statusId") or row.get("referrerId")
        name = row.get("name") or row.get("backendName") or row.get("statusName") or ""
        print(f"  {rid!s:>10s}  {name}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn-id")
    ap.add_argument("--base-url"); ap.add_argument("--user"); ap.add_argument("--password")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    if args.conn_id:
        async with db_session() as db:
            c = await db.get(PlentyConnection, uuid.UUID(args.conn_id))
            if not c:
                sys.exit("unknown plenty connection")
            client = PlentyOneClient.from_connection(settings, c)
    elif args.base_url and args.user and args.password:
        client = PlentyOneClient(settings, base_url=args.base_url,
                                 username=args.user, password=args.password)
    else:
        sys.exit("provide --conn-id OR --base-url + --user + --password")

    async with client:
        data = await discover(client)

    if not args.json_only:
        _print("REFERRERS (marketplace channels)", data["referrers"])
        _print("WAREHOUSES", data["warehouses"])
        _print("ORDER STATUSES", data["order_statuses"])
        _print("PAYMENT METHODS", data["payment_methods"])

    print("\n--- JSON ---")
    print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
