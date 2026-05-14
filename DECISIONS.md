# Integration Decisions Log

All architectural decisions, assumptions, and trade-offs made during implementation.

---

## 1. PlentyONE Order Type ID

**Decision:** `typeId: 1` (Sales Order)
**Why:** API documentation research confirmed that `typeId: 3` = Returns, NOT Standard order.
The original spec stated "Order Type: 3 (Standard)" which appears to be a documentation error.
Type 1 = Sales Order is the correct type for marketplace orders.
**Impact:** If your PlentyONE instance uses a custom order type for marketplace orders, update `typeId` in `plenty_client.py → create_order()`.

---

## 2. Retry Policy

**Decision:** 3 attempts, exponential backoff (4s → 8s → 16s max 10s cap)
**Why:** Balance between reliability and API rate limits. 5 attempts is overkill for transient errors;
if the API is truly down, retrying more doesn't help. Exponential backoff prevents thundering herd.
**Note:** 429 (rate limit) on Mirakl triggers the same retry. If Mirakl's `Retry-After` > 10s, the
tenacity backoff will be shorter than required — in production, consider reading `Retry-After` and
sleeping that exact duration (see `RateLimitError` class for hook point).

---

## 3. Mirakl Authentication

**Decision:** API Key in `Authorization` header (not OAuth2 Bearer)
**Why:** Simpler to configure, no token refresh needed, sufficient for seller API.
OAuth2 is available for platform-level integrations; not needed here.

---

## 4. PlentyONE Payment Status

**Decision:** `statusId: 5.0` (Release for Dispatch) at order creation
**Why:** Douglas pays sellers immediately (as stated in spec: "Douglas zahlt sofort").
Creating orders already in "ready to ship" status avoids a separate payment status update step.
The spec mentioned `paymentStatus: 2 (Bezahlt)` but PlentyONE uses `statusId` on the order level
for workflow state. The `fullyPaid` payment status is set implicitly for marketplace referrer orders.

---

## 5. SKU Resolution: Three-Tier Fallback

**Decision:** Mapping table → EAN lookup → Quarantine (ERROR)
**Why:** Strict fail-fast for unmapped SKUs prevents creating phantom orders in PlentyONE.
Orders in ERROR state are preserved with full raw_json for manual resolution.
The EAN fallback handles cases where PlentyONE variations exist but weren't in the initial
CSV import (e.g., recently added products).

---

## 6. HTTP Connection Pool Limits

**Decision:** Mirakl: 20 connections / PlentyONE: 10 connections
**Why:** PlentyONE limits concurrent sessions to 3 users. 10 connections shares one session.
Mirakl allows 1000 req/min; 20 connections is conservative and safe.

---

## 7. Celery vs Pure Asyncio

**Decision:** Celery with Redis broker
**Why:** Celery provides durable task persistence, easy monitoring via Flower, and Beat scheduling.
The polling tasks are infrequent (every 5-15 min) so Celery's overhead is acceptable.
A pure asyncio scheduler (APScheduler) was considered but lacks the distributed task management needed.

---

## 8. DB Schema: mirakl_order_id as Primary Key

**Decision:** `mirakl_order_id` is the PK (not an auto-increment surrogate)
**Why:** Natural key prevents duplicate imports at the DB constraint level.
`INSERT ... ON CONFLICT DO NOTHING` or `get()` check both work correctly.
Trade-off: Mirakl order IDs are strings (up to 100 chars) — slightly less efficient than int PK
but the safety guarantee outweighs the performance difference at expected volumes.

---

## 9. Inventory Sync: Async Import (OF01/OF02)

**Decision:** CSV-based async import flow (not per-offer synchronous update)
**Why:** Mirakl's OF01/OF02 async import is the recommended approach for batch updates.
Individual offer update endpoints exist (OF24) but Douglas volumes (potentially thousands of SKUs)
make per-offer updates impractical within rate limits.

---

## 10. Address Parsing: Street/House Number Split

**Decision:** Heuristic split on last space character
**Why:** PlentyONE separates street name (address1) from house number (address2).
Mirakl delivers a single `street1` field. German addresses typically follow "Streetname 12" format.
Edge cases (e.g., "Am Hang 5a", "Große Straße 1-3") are handled by the heuristic but may need
manual correction in edge cases. A proper address parser (e.g., `postal-address` library) is
Phase 2 improvement.

---

## 11. Carrier Default Fallback (CONTINUED — see end of file for #12+)

**Decision:** Default to "DHL" when PlentyONE doesn't return a carrier name
**Why:** DHL is the dominant carrier for German e-commerce and likely for Douglas beauty products.
This is a safe assumption for the German market. Add explicit carrier mapping in PlentyONE
shipping profiles to make this deterministic.

---

## 12. Surrogate UUID PK on order_sync + sku_mapping (SaaS migration 002)

**Decision:** Replace natural-key PK (`mirakl_order_id`, `mirakl_sku`) with
surrogate `id uuid` and add unique constraint `(mirakl_connection_id, <natural>)`.
**Why:** Multi-tenancy + multi-Mirakl-shop-per-tenant means the same string
collides across shops (Douglas "12345" vs Shop-Apotheke "12345"). Surrogate
UUID + composite-unique-with-connection_id is the only honest solution.
The natural-key columns stay (now part of unique constraint) — no data lost
per the "never delete existing data" rule.

---

## 13. RLS as belt-and-braces, code-level scoping primary

**Decision:** Every tenant-scoped table has RLS policies AND every query in
service code filters by `tenant_id`. The Postgres `connector_app` role
(NOSUPERUSER + NOBYPASSRLS) is created by migration 002 but the runtime
`DATABASE_URL` choice is left to the operator.
**Why:** Wismo SaaS hit the trap where the bootstrap `POSTGRES_USER` was a
superuser → BYPASSRLS → policies silently no-op. Tests in this repo enforce
code-level scoping (does the WHERE clause include `tenant_id`?), not RLS,
so we don't depend on the test DB role.

---

## 14. Tenant suspension = stop polling, not quarantine new orders

**Decision:** When a tenant is suspended, the orchestrator simply skips
their Mirakl polls. Mirakl-side orders stay in WAITING_ACCEPTANCE for the
standard acceptance window; the operator must reactivate before that
window expires.
**Why:** Cleaner state machine. Avoids inventing a `QUARANTINE_TENANT_SUSPENDED`
status that would need new flows on reactivation. If a tenant lets their
sub lapse for >Mirakl-acceptance-window days, the orders auto-cancel in
Mirakl — that's already correct behavior for an inactive seller.

---

## 15. Stripe Metered Billing deferred to Phase 2

**Decision:** €29/mo subscription via standard Stripe Price + €0.15/extra
overage tracked in `usage_counters.orders_overage`. Overage is invoiced
manually for now — no automated metered-billing usage_records.
**Why:** Volume in early months will be tiny; manual reconciliation is
cheap. Stripe Metered Billing adds complexity (per-tenant subscription
items, usage_records POSTs, idempotency keys) that's not earning revenue
yet. Re-evaluate when first tenant exceeds quota.

---

## 16. Per-tenant Plenty webhook secret, falls back to poller

**Decision:** Each PlentyConnection row has its own `webhook_secret`.
Plenty's event-procedure POSTs to `/webhooks/plenty/<tenant>/order-status`
with HMAC-SHA256 of the body in `X-Plenty-Signature` (preferred), or
`?secret=...` query param (fallback).
**Why:** Plenty event-procedures vary in what they can send. HMAC-header
mode is preferred but not always available; the query-param fallback
covers both. The per-connection (not per-tenant) secret limits blast
radius if one mandant gets compromised.

---

## 17. Single Plenty mandant per tenant in Phase 1

**Decision:** Schema allows N `plenty_connections` per tenant; the
order-flow orchestrator picks the FIRST active one.
**Why:** Simpler. Multi-Plenty per tenant is a future-feature. No real
operator has asked for it yet, and the routing rules (which Mirakl shop
maps to which Plenty mandant) need product-design before code.

---

## 18. Audit log without hash chain

**Decision:** `audit_log` is plain append-only by code convention; no DB
trigger blocking UPDATE/DELETE.
**Why:** Wismo SaaS has a hash-chain audit because of GDPR/compliance
requirements per their tenants. This connector's audit is operational
(who-did-what for debugging), not legal-evidentiary. Cheaper schema,
easier to read in panel. If a paying customer demands forensic-grade
audit, add the trigger then.
