#!/usr/bin/env python3
"""SaaS admin CLI.

Usage:
    python scripts/saas_admin.py tenant create --name "Acme" --email a@b.com [--quota 200]
    python scripts/saas_admin.py tenant list
    python scripts/saas_admin.py tenant suspend --id <uuid>
    python scripts/saas_admin.py tenant activate --id <uuid>
    python scripts/saas_admin.py tenant set-quota --id <uuid> --quota 500

    python scripts/saas_admin.py mirakl-conn add --tenant <uuid> --label "Douglas DE" \\
        --base-url https://shop.mirakl.net --api-key <KEY> [--shop-id 0]
    python scripts/saas_admin.py mirakl-conn test --id <uuid>
    python scripts/saas_admin.py mirakl-conn list --tenant <uuid>
    python scripts/saas_admin.py mirakl-conn remove --id <uuid>

    python scripts/saas_admin.py plenty-conn add --tenant <uuid> --label "Acme Plenty" \\
        --base-url https://pNNNNN.my.plentysystems.com --user <U> --password <P> \\
        [--referrer-id 1] [--warehouse-id 1] [--plenty-id 1234] [--gen-webhook-secret]
    python scripts/saas_admin.py plenty-conn test --id <uuid>

    python scripts/saas_admin.py usage report [--tenant <uuid>] [--year 2026] [--month 5]

All commands use the running DATABASE_URL. No external API calls except
`*-conn test` which performs a live ping against the configured URL.
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.api.mirakl_client import MiraklClient
from app.api.plenty_client import PlentyOneClient
from app.auth.api_keys import generate_api_key, hash_api_key
from app.config import get_settings
from app.models.database import db_session
from app.tenancy.crypto import encrypt
from app.tenancy.models import (
    MiraklConnection,
    PlentyConnection,
    Tenant,
    UsageCounter,
)


# ---------------------------------------------------------------------------
# tenant
# ---------------------------------------------------------------------------

async def cmd_tenant_create(args):
    async with db_session() as db:
        api_key = generate_api_key()
        t = Tenant(
            name=args.name,
            contact_email=args.email,
            monthly_quota=args.quota,
            api_key_hash=hash_api_key(api_key),
            status="active",
        )
        db.add(t)
        await db.flush()
        print(f"tenant_id  = {t.id}")
        print(f"api_key    = {api_key}    # SHOWN ONCE — store it now")


async def cmd_tenant_list(args):
    async with db_session() as db:
        res = await db.execute(select(Tenant).order_by(Tenant.created_at))
        for t in res.scalars().all():
            print(f"{t.id} {t.status:14s} q={t.monthly_quota:>5d} {t.name}  <{t.contact_email or '-'}>")


async def cmd_tenant_set_status(args, status):
    async with db_session() as db:
        t = await db.get(Tenant, uuid.UUID(args.id))
        if not t:
            sys.exit(f"unknown tenant {args.id}")
        t.status = status
        db.add(t)
        print(f"{t.id} status -> {status}")


async def cmd_tenant_set_quota(args):
    async with db_session() as db:
        t = await db.get(Tenant, uuid.UUID(args.id))
        if not t:
            sys.exit(f"unknown tenant {args.id}")
        t.monthly_quota = args.quota
        db.add(t)
        print(f"{t.id} quota -> {args.quota}")


# ---------------------------------------------------------------------------
# mirakl-conn
# ---------------------------------------------------------------------------

async def cmd_mirakl_add(args):
    async with db_session() as db:
        c = MiraklConnection(
            tenant_id=uuid.UUID(args.tenant),
            label=args.label,
            base_url=args.base_url,
            api_key_enc=encrypt(args.api_key),
            shop_id=args.shop_id,
            active=True,
        )
        db.add(c)
        await db.flush()
        print(f"mirakl_connection_id = {c.id}")


async def cmd_mirakl_list(args):
    async with db_session() as db:
        res = await db.execute(
            select(MiraklConnection).where(MiraklConnection.tenant_id == uuid.UUID(args.tenant))
        )
        for c in res.scalars().all():
            print(f"{c.id} active={c.active} shop={c.shop_id} {c.label} -> {c.base_url}")


async def cmd_mirakl_test(args):
    async with db_session() as db:
        c = await db.get(MiraklConnection, uuid.UUID(args.id))
        if not c:
            sys.exit(f"unknown connection {args.id}")
        settings = get_settings()
        try:
            async with MiraklClient.from_connection(settings, c) as m:
                orders = await m.get_orders(limit=1)
            print(f"OK — {len(orders)} order(s) reachable")
        except Exception as exc:
            sys.exit(f"FAIL: {exc}")


async def cmd_mirakl_remove(args):
    async with db_session() as db:
        c = await db.get(MiraklConnection, uuid.UUID(args.id))
        if not c:
            sys.exit("unknown")
        # Per "never delete data" — soft-disable instead of DELETE.
        c.active = False
        db.add(c)
        print(f"{c.id} deactivated (soft delete)")


# ---------------------------------------------------------------------------
# plenty-conn
# ---------------------------------------------------------------------------

async def cmd_plenty_add(args):
    async with db_session() as db:
        webhook = secrets.token_urlsafe(32) if args.gen_webhook_secret else None
        c = PlentyConnection(
            tenant_id=uuid.UUID(args.tenant),
            label=args.label,
            base_url=args.base_url,
            username=args.user,
            password_enc=encrypt(args.password),
            referrer_id=args.referrer_id,
            warehouse_id=args.warehouse_id,
            plenty_id=args.plenty_id,
            webhook_secret=webhook,
            active=True,
        )
        db.add(c)
        await db.flush()
        print(f"plenty_connection_id = {c.id}")
        if webhook:
            print(f"webhook_secret       = {webhook}")
            print(f"webhook_url          = https://<your-host>/webhooks/plenty/{args.tenant}/order-status")


async def cmd_plenty_test(args):
    async with db_session() as db:
        c = await db.get(PlentyConnection, uuid.UUID(args.id))
        if not c:
            sys.exit("unknown")
        settings = get_settings()
        try:
            async with PlentyOneClient.from_connection(settings, c) as p:
                # Trigger login round-trip (already done in __aenter__)
                pass
            print("OK — login succeeded")
        except Exception as exc:
            sys.exit(f"FAIL: {exc}")


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

async def cmd_usage_report(args):
    now = datetime.now(timezone.utc)
    year, month = args.year or now.year, args.month or now.month
    async with db_session() as db:
        if args.tenant:
            tres = await db.execute(select(Tenant).where(Tenant.id == uuid.UUID(args.tenant)))
        else:
            tres = await db.execute(select(Tenant))
        for t in tres.scalars().all():
            res = await db.execute(text("""
                SELECT orders_imported, orders_overage FROM usage_counters
                WHERE tenant_id=:t AND period_year=:y AND period_month=:m
            """), {"t": t.id, "y": year, "m": month})
            row = res.first()
            used = row[0] if row else 0
            over = row[1] if row else 0
            print(f"{t.id} {t.name:30s} {year}-{month:02d}  used={used:>5d}/{t.monthly_quota:<5d} overage={over}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="saas_admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tenant").add_subparsers(dest="sub", required=True)
    tc = t.add_parser("create"); tc.add_argument("--name", required=True); tc.add_argument("--email", required=True); tc.add_argument("--quota", type=int, default=200)
    t.add_parser("list")
    ts = t.add_parser("suspend"); ts.add_argument("--id", required=True)
    ta = t.add_parser("activate"); ta.add_argument("--id", required=True)
    tq = t.add_parser("set-quota"); tq.add_argument("--id", required=True); tq.add_argument("--quota", type=int, required=True)

    m = sub.add_parser("mirakl-conn").add_subparsers(dest="sub", required=True)
    ma = m.add_parser("add")
    ma.add_argument("--tenant", required=True); ma.add_argument("--label", required=True)
    ma.add_argument("--base-url", required=True); ma.add_argument("--api-key", required=True)
    ma.add_argument("--shop-id", type=int, default=0)
    ml = m.add_parser("list"); ml.add_argument("--tenant", required=True)
    mt = m.add_parser("test"); mt.add_argument("--id", required=True)
    mr = m.add_parser("remove"); mr.add_argument("--id", required=True)

    pc = sub.add_parser("plenty-conn").add_subparsers(dest="sub", required=True)
    pa = pc.add_parser("add")
    pa.add_argument("--tenant", required=True); pa.add_argument("--label", required=True)
    pa.add_argument("--base-url", required=True); pa.add_argument("--user", required=True)
    pa.add_argument("--password", required=True); pa.add_argument("--referrer-id", type=int, default=1)
    pa.add_argument("--warehouse-id", type=int, default=1); pa.add_argument("--plenty-id", type=int, default=0)
    pa.add_argument("--gen-webhook-secret", action="store_true")
    pt = pc.add_parser("test"); pt.add_argument("--id", required=True)

    u = sub.add_parser("usage").add_subparsers(dest="sub", required=True)
    ur = u.add_parser("report")
    ur.add_argument("--tenant", required=False); ur.add_argument("--year", type=int)
    ur.add_argument("--month", type=int)

    return p


def main():
    args = build_parser().parse_args()
    cmd = args.cmd

    if cmd == "tenant":
        if args.sub == "create": asyncio.run(cmd_tenant_create(args))
        elif args.sub == "list": asyncio.run(cmd_tenant_list(args))
        elif args.sub == "suspend": asyncio.run(cmd_tenant_set_status(args, "suspended"))
        elif args.sub == "activate": asyncio.run(cmd_tenant_set_status(args, "active"))
        elif args.sub == "set-quota": asyncio.run(cmd_tenant_set_quota(args))
    elif cmd == "mirakl-conn":
        if args.sub == "add": asyncio.run(cmd_mirakl_add(args))
        elif args.sub == "list": asyncio.run(cmd_mirakl_list(args))
        elif args.sub == "test": asyncio.run(cmd_mirakl_test(args))
        elif args.sub == "remove": asyncio.run(cmd_mirakl_remove(args))
    elif cmd == "plenty-conn":
        if args.sub == "add": asyncio.run(cmd_plenty_add(args))
        elif args.sub == "test": asyncio.run(cmd_plenty_test(args))
    elif cmd == "usage":
        asyncio.run(cmd_usage_report(args))


if __name__ == "__main__":
    main()
