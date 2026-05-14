# PlentyONE ↔ Mirakl Connector — SaaS Architecture

This document describes the multi-tenant SaaS evolution of the original
single-tenant Douglas-only connector. It is the canonical design reference;
when in doubt about scope or behavior, this file wins.

Authored 2026-05-14. Continues the assumption stack from `DECISIONS.md`
(decisions #12+ live there; #1–11 stay untouched).

---

## 1. What changed

| Aspect | Before | After |
|---|---|---|
| Tenancy | hard-wired single tenant via env vars | N tenants, each with N Mirakl shops + 1 Plenty mandant |
| Auth | none (private internal API) | per-tenant `X-Api-Key` (bcrypt-hashed); admin via `ADMIN_API_KEY` env |
| Billing | n/a | Stripe subscription €29/mo, includes 200 orders/mo, then €0.15/extra |
| Persistence | one DB, one config | one DB, RLS + scoped queries by `tenant_id` |
| Panel | no UI | `/panel` (Jinja+HTMX), tenant view + admin overview |
| Webhooks | n/a | Plenty status webhook in, Stripe webhook in |
| Audit | structlog only | structlog + `audit_log` table for every mutation |

---

## 2. Data model (additions)

### New tables

```
tenants
  id              uuid pk
  name            text
  status          text   ('active','past_due','suspended','trial','canceled')
  api_key_hash    text   -- bcrypt of the merchant's X-Api-Key
  contact_email   text
  monthly_quota   int    -- default 200
  stripe_customer_id     text
  stripe_subscription_id text
  current_period_end     timestamptz
  created_at      timestamptz
  updated_at      timestamptz

mirakl_connections
  id              uuid pk
  tenant_id       uuid fk -> tenants(id) on delete cascade
  label           text   -- "Douglas DE", "Shop-Apotheke"
  base_url        text
  api_key_enc     bytea  -- Fernet-encrypted Mirakl seller API key
  shop_id         int    -- 0 if single-shop
  active          bool
  last_poll_at    timestamptz
  last_poll_ok    bool
  consecutive_failures int default 0
  created_at, updated_at

plenty_connections
  id              uuid pk
  tenant_id       uuid fk -> tenants(id) on delete cascade
  label           text
  base_url        text
  username        text
  password_enc    bytea  -- Fernet
  referrer_id     int
  warehouse_id    int
  plenty_id       int
  active          bool
  webhook_secret  text   -- shared secret for incoming /webhooks/plenty/<tenant>/...
  last_call_at    timestamptz
  last_call_ok    bool
  consecutive_failures int default 0
  created_at, updated_at

audit_log
  id              bigserial pk
  tenant_id       uuid     -- nullable for platform-level events
  ts              timestamptz default now()
  actor           text     -- "tenant:<uuid>", "admin", "system", "stripe"
  action          text     -- "tenant.create", "order.import", "mirakl.ship", ...
  entity          text     -- "order", "mirakl_connection", ...
  entity_id       text
  payload         jsonb    -- {"diff": {...}, "context": {...}}

usage_counters
  id              bigserial pk
  tenant_id       uuid     -- not null
  period_year     int
  period_month    int
  orders_imported int default 0
  orders_overage  int default 0
  -- (orders_imported - monthly_quota, capped at 0)
  unique (tenant_id, period_year, period_month)
```

### Tenant_id added to existing tables

`order_sync`, `sku_mapping`, `inventory_log` all gain:

* `tenant_id uuid not null`
* `mirakl_connection_id uuid not null` (so the same Mirakl SKU/order ID can
  exist in different shops without collision)

### Primary-key rework (schema decision #12)

The current schema uses natural keys (`mirakl_order_id`, `mirakl_sku`) as
primary keys. With multi-tenancy this would create cross-shop collisions
("12345" on Douglas vs "12345" on Shop-Apotheke). The migration:

* `order_sync`: drops PK on `mirakl_order_id`, adds surrogate `id uuid pk`,
  unique constraint `(mirakl_connection_id, mirakl_order_id)`. The orchestrator
  layer always queries by `(connection_id, mirakl_order_id)`.
* `sku_mapping`: same pattern, unique on `(mirakl_connection_id, mirakl_sku)`.
* `inventory_log`: stays append-only, gains `tenant_id`, `mirakl_connection_id`.

**Backward compat**: a `default` tenant + one Mirakl/Plenty connection are
inserted by the migration using values from existing env vars, so the
single-tenant dev deployment keeps running with no manual ops.

### Row-Level Security

Every tenant-scoped table has:

```sql
ALTER TABLE foo ENABLE ROW LEVEL SECURITY;
ALTER TABLE foo FORCE ROW LEVEL SECURITY;
CREATE POLICY foo_tenant_isolation ON foo
  USING (tenant_id::text = current_setting('app.current_tenant', true));
```

**Assumption A1** (open, see §10): RLS is belt-and-braces only. Code-level
scoping (every query filters by `tenant_id` via the `tenant_ctx` dependency)
is the primary defense, because the local Postgres bootstrap user is a
superuser → BYPASSRLS. Wismo SaaS hit this exact trap (CLAUDE.md §RLS) and
the fix requires a separate `connector_app` non-superuser role at deploy
time. The migration creates the role; whether it's used is a runtime
config (`DATABASE_URL` username) the operator chooses. Tests in this
deliverable assert code-level scoping, not RLS.

---

## 3. Sequence diagrams

### Inbound: Mirakl → Plenty

```
celery beat (every 60s)
   │
   └─> orchestrator.import_orders_for_all()
         │
         for each (tenant, mirakl_conn, plenty_conn) in active connections:
           │
           ├─ check tenant.status; skip if suspended/past_due
           ├─ check usage quota; skip + alert if 100% used
           │
           └─ OrderService(db, MiraklClient(conn), PlentyOneClient(conn))
                .import_new_orders()
                  │
                  for each Mirakl WAITING_ACCEPTANCE order:
                    ├─ resolve SKUs (mapping table → EAN fallback → ERROR)
                    ├─ create Plenty sales order (typeId=1, statusId=5)
                    ├─ store order_sync row (status=IMPORTED)
                    ├─ increment usage_counters
                    └─ audit_log.append("order.import", ...)

celery beat (every 60s, +30s offset)
   │
   └─> orchestrator.confirm_orders_for_all()
         │
         for each connection:
           OrderService.confirm_orders()
             for each IMPORTED order: Mirakl OR21 accept → CONFIRMED
```

### Outbound: Plenty → Mirakl (status 7 = Versendet)

```
Plenty event-procedure → POST /webhooks/plenty/<tenant_id>/order-status
   │  (HMAC-signed with plenty_connections.webhook_secret)
   │
   ├─ verify HMAC; reject 401 if bad
   ├─ load tenant + plenty_connection
   ├─ if status=7 and tracking present:
   │     ├─ find order_sync row by plenty_order_id (scoped to tenant)
   │     ├─ MiraklClient(conn).ship_order(...)  -- OR23
   │     ├─ order_sync.status = SHIPPED
   │     └─ audit_log.append("mirakl.ship", ...)
   │
   └─ 200 OK (or 4xx with diagnostic JSON for the Plenty UI)

Fallback poller (every 5min) for tenants without configured webhook:
   orchestrator.ship_pending_for_all() — uses existing OrderService.ship_orders()
```

### Billing: Stripe Checkout & webhook

```
Signup landing page (out of scope for this repo) → Stripe Checkout
   │
   └─ on success: redirects to `/panel/welcome?session_id=...`
         │
         └─ Stripe sends `checkout.session.completed` webhook
              POST /billing/stripe-webhook
                ├─ verify signature with STRIPE_WEBHOOK_SECRET
                ├─ create tenants row with status=active, generate api_key
                ├─ email API key to merchant via BillionMail (draft only;
                │   user-confirmed action — see §9)
                └─ audit_log.append("stripe.subscription.created", ...)

Recurring events:
  invoice.payment_succeeded     → status=active, current_period_end=...
  invoice.payment_failed        → status=past_due, alert
  customer.subscription.deleted → status=canceled
  customer.subscription.updated → sync price_id / current_period_end
```

---

## 4. Module layout

```
app/
├── api/                       # Existing Mirakl + Plenty HTTP clients (unchanged signatures)
│   ├── mirakl_client.py       #   + .from_connection(MiraklConnection) classmethod
│   ├── plenty_client.py       #   + .from_connection(PlentyConnection) classmethod
│   └── schemas.py
│
├── auth/                      # NEW
│   ├── __init__.py
│   ├── api_keys.py            # bcrypt hash/verify, key generation
│   └── deps.py                # FastAPI Depends(current_tenant), Depends(admin_only)
│
├── billing/                   # NEW
│   ├── __init__.py
│   ├── stripe_client.py
│   ├── webhook.py             # signature verify + event handlers
│   └── quota.py               # increment, check, reset (called by Celery beat)
│
├── tenancy/                   # NEW
│   ├── __init__.py
│   ├── crypto.py              # Fernet helpers for connection secrets
│   ├── context.py             # ContextVar + asyncpg "SET LOCAL app.current_tenant"
│   └── models.py              # Tenant, MiraklConnection, PlentyConnection ORM
│
├── audit/                     # NEW
│   ├── __init__.py
│   └── log.py                 # append() — structlog AND audit_log row
│
├── panel/                     # NEW
│   ├── __init__.py
│   ├── routes.py
│   └── templates/
│       ├── base.html
│       ├── tenant.html
│       └── admin.html
│
├── webhooks/                  # NEW
│   ├── __init__.py
│   ├── plenty.py              # /webhooks/plenty/<tenant>/order-status
│   └── stripe.py              # mounted under /billing/stripe-webhook
│
├── services/
│   ├── order_service.py       # existing — unchanged class, used via factories
│   ├── inventory_service.py   # existing — unchanged class
│   └── orchestrator.py        # NEW — fans out per (tenant, conn) pair
│
├── tasks/                     # Existing — celery_app + sync_tasks call orchestrator
│   ├── celery_app.py
│   └── sync_tasks.py
│
├── models/                    # Existing — tables.py extended
│   ├── tables.py              # + tenant_id, + new tables, + RLS
│   └── database.py            # + per-tenant session helper
│
├── core/
│   └── logging.py             # unchanged
│
├── config.py                  # extended with stripe_*, fernet_key, admin_api_key
└── main.py                    # mounts panel + webhooks + auth deps
```

---

## 5. Quota & billing semantics

* Counter increments **only on successful Plenty order create** (in
  `OrderService._import_single_order` happy path, after the Plenty POST
  returns 2xx).
* Atomic SQL: `UPDATE usage_counters SET orders_imported = orders_imported + 1
  WHERE tenant_id=$1 AND period_year=$2 AND period_month=$3
  RETURNING orders_imported`. UPSERT via `INSERT ... ON CONFLICT DO UPDATE`.
* At 80% (160/200) → BillionMail ops alert to the tenant's contact_email.
* At 100% (200/200) → orchestrator skips this tenant for the rest of the
  month, sets `tenants.status='quota_exceeded'` (a derived status that
  reverts on month-rollover OR upsell).
* Overage tracking (€0.15/extra): `orders_overage` accumulates AFTER the
  block lifts via upsell. **For the first ship date this is bookkeeping
  only — billing is invoiced manually** (see decision #15). Stripe Metered
  Billing is a phase-2 deliverable.
* Monthly reset: Celery beat task on day 1 at 00:05 UTC inserts a fresh
  `usage_counters` row per tenant, resets `tenants.status` from
  `quota_exceeded` back to `active` (only that derived value).

---

## 6. Auth

### Per-tenant API key

* On tenant create, generate `key = "pmc_" + secrets.token_urlsafe(32)`.
  Store `bcrypt(key)` in `tenants.api_key_hash`. Return plaintext **once**
  in the create response (CLI prints it; Stripe-flow emails it).
* All `/api/*` routes require `X-Api-Key`. Middleware looks up the tenant
  by hash (linear scan over active tenants — fine up to ~10k tenants;
  swap for prefix-indexed lookup later).
* Sets request-scoped `tenant_ctx`; downstream queries auto-filter.

### Admin

* `ADMIN_API_KEY` env var (long random token). Required for:
  * `/panel/admin`
  * `/api/admin/*` routes (tenant CRUD via HTTP)
  * `scripts/saas_admin.py` (which can also use `DATABASE_URL` directly).

### Suspended / past_due → 402

* `current_tenant` dep checks `tenants.status`. If not in `('active','trial')`,
  raises `HTTPException(402, detail="subscription inactive")`.
* Same status helper used by orchestrator to skip processing.

---

## 7. Panel

Single template family (`base.html` + `tenant.html` + `admin.html`),
**dark + dense + filterable**, auto-refresh via HTMX every 5s, no SPA.

`GET /panel` (session-based or `?api_key=...` one-shot for embedding):
* Header: tenant name, plan, status badge, current MRR
* KPI grid: orders today / 7d / 30d, error count, last successful Mirakl
  poll (per connection), last successful Plenty call, quota usage % bar
* Recent activity table (audit_log last 50 rows, filterable by action)
* Per-connection card: connection label, last poll OK?, consecutive failures

`GET /panel/admin` (admin key):
* All tenants table sortable by status / MRR / errors
* MRR sum, total orders this month, churn-risk highlight (errors > 5/day)

Visual style mirrors `~/.claude/skills/render-queue-panel/` — single-file
HTML, inline CSS, no JS framework. Just `htmx.min.js` from CDN.

---

## 8. Order state machine (extended)

Existing: `NEW → IMPORTED → CONFIRMED → SHIPPED` plus `ERROR` terminal.

New status `QUARANTINE_TENANT_SUSPENDED`: orders pulled from Mirakl after
the tenant got suspended. They sit untouched until the tenant reactivates;
on reactivation the orchestrator picks them up via the existing NEW path.
**Decision: not implemented in this iteration** — instead, when a tenant
is suspended we simply *don't poll* their Mirakl. The Mirakl side will
hold the orders in WAITING_ACCEPTANCE for the standard acceptance window;
operator must reactivate before the window expires. This keeps the state
machine clean. Documented in DECISIONS.md #14.

---

## 9. Mail alerts (BillionMail)

Per the privacy + mail rule in MEMORY.md (`feedback_never_send_mail_auto`):
all merchant-facing mail goes via `create_draft` and surfaces in Romic's
Thunderbird for review. Exception: *system alerts to romic@vagabond-consulting.com*
(the operator's own inbox) are sent directly — those are ops signals, not
customer-facing.

Triggers:

| Event | Recipient | Mode |
|---|---|---|
| Tenant signed up | tenant.contact_email | draft (welcome + API key) |
| Quota 80% | tenant.contact_email | draft |
| Quota 100% | tenant.contact_email | draft + ops digest |
| Connection broken (3 failures) | tenant.contact_email | draft |
| Daily ops digest | romic@vagabond-consulting.com | send |
| Stripe payment failed | tenant.contact_email | draft |

The actual sending mechanism is the BillionMail skill API; for this
deliverable the alert helper writes draft files and logs them — wiring to
the BillionMail HTTP endpoint is a one-line call documented as TODO at
`app/billing/quota.py::_alert`.

---

## 10. Open assumptions

* **A1 — RLS as belt-and-braces (see §2)**: code-level scoping is primary;
  RLS only enforces if the operator deploys with the non-superuser
  `connector_app` role. Migration creates the role; tests don't enforce
  RLS, they enforce code-level scoping.
* **A2 — Stripe Metered Billing deferred**: overage (€0.15/order) tracked
  in `usage_counters.orders_overage` for now. Stripe Metered Billing
  hookup is a follow-up task. Manual invoicing in the meantime is fine
  (volume will be tiny in early months).
* **A3 — Plenty webhook signing**: We assume Plenty's event-procedure
  POST can include an HMAC header. If not, we fall back to a
  per-connection shared-secret query parameter (`?secret=...`). The code
  supports both; the deployment doc walks the operator through whichever
  Plenty actually offers.
* **A4 — Carrier mapping shared across tenants**: `CARRIER_MAP` in
  `app/api/schemas.py` stays global. Per-tenant carrier overrides are a
  future-feature, not blocking.
* **A5 — Mirakl OR10 (refund) is Phase 1.5**: out of scope for this
  iteration. Refunds happen by hand for now.
* **A6 — Single Plenty mandant per tenant**: the schema allows N
  `plenty_connections` per tenant but the order-flow code assumes 1
  active. Multi-Plenty per tenant is a future-feature.
* **A7 — Smoke E2E creates orders in Plenty test instance and leaves
  them**: with a `SMOKE_E2E_` prefix on commercial_id; cleanup is a
  separate `--cleanup` flag the user can run. Deliberate to keep the
  smoke script idempotent + fast.
* **A8 — Playwright panel E2E deferred**: acceptance criteria says
  "panel reachable on localhost:8000/panel". A curl smoke test is in
  the integration suite. Full Playwright E2E is a follow-up.

---

## 11. Acceptance summary

* `pytest -m "not integration"` green — covers tenant scoping, quota
  enforcement, Stripe webhook signature, OR23 ship payload shape, idempotency,
  and the `default` tenant migration path.
* `pytest -m integration` documented (real Plenty test instance, mocked
  Mirakl). Not run from this session because it touches a live system.
* `scripts/saas_admin.py tenant list` works against a fresh DB after `alembic
  upgrade head`.
* `scripts/smoke_e2e.py` runs against the Plenty p73736 test instance and
  exercises the full create-order + tracking-update + would-call-OR23 path
  (Mirakl mocked).
* Panel reachable at `localhost:8000/panel` (with API key) and `/panel/admin`
  (with admin key).

---

## 12. Production routing (added 2026-05-14)

Live demo edge runs **Cloudflare Tunnel**, not Traefik:

```
internet → Cloudflare edge (TLS) → cloudflared-pmc (docker) → api:8000
```

* Tunnel id `2c82f717-1846-4095-ad30-9cc0fb7aad48` (`pmc-connector`).
* Container `cloudflared-pmc` on docker networks `traefik_default`,
  `plenty-mirakl-prod_internal`, `pmc-staging_internal` so it can resolve
  both backends by docker DNS name.
* Ingress in CF Tunnel config:
  * `connector.vagabond-consulting.com` → `http://plenty-mirakl-prod-api-1:8000`
  * `staging-connector.vagabond-consulting.com` → `http://pmc-staging-api-1:8000`
* DNS: CF CNAME → `<tunnel-id>.cfargotunnel.com`, proxied=true.
* Traefik labels stripped from compose files — Traefik no longer involved.

Why pivot from Traefik:
1. Let's Encrypt rate-limited the vagabond-consulting.com account due to
   pre-existing failing subdomains (drinkmate, ai-support-helper).
2. Traefik docker-provider showed no router-load events for our containers
   even with correct labels — root cause not isolated within demo budget.

CF Tunnel benefits realised:
* No Let's Encrypt dependency.
* Survives Traefik restarts.
* Origin server not exposed on port 443 (defense in depth).
* `~50ms TLS handshake at edge.

Note on `staging-connector` vs `staging.connector`:
* CF free Universal SSL covers `*.vagabond-consulting.com` (one level only).
* `staging.connector.vagabond-consulting.com` (two levels) needs CF Advanced
  Cert Manager ($10/mo). Per the no-money rule we used `staging-connector.*`
  (one level) instead.
