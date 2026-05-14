# PlentyONE ↔ Mirakl Connector (SaaS)

Multi-tenant async connector between Mirakl marketplaces (Douglas, Shop-Apotheke,
Galaxus, Otto, ...) and PlentyONE ERP.

> **v2 — SaaS evolution.** The original single-tenant connector still runs as
> the seeded `default` tenant after migration 002. See `ARCHITECTURE_SAAS.md`
> for the full multi-tenant design and `DECISIONS.md` #12+ for new trade-offs.

**Pricing:** €29/mo includes 200 synced orders, then €0.15/extra. Stripe-powered.

**Operator panel:** `https://plenty-mirakl.420.ovh/panel?key=<api_key>` (tenant)
or `/panel/admin?key=<admin_key>` (cross-tenant overview).

## Architecture

```
Mirakl (Douglas)  ←→  Connector (FastAPI + Celery)  ←→  PlentyONE
                              ↕
                    PostgreSQL (state) + Redis (queue)
```

**Data flows:**
1. **INBOUND**: Mirakl NEW orders → PlentyONE → Accept back in Mirakl
2. **OUTBOUND**: PlentyONE tracking → Ship in Mirakl
3. **SYNC**: PlentyONE stock → Mirakl Offers

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env with your real API keys and URLs
```

**Required variables:**

| Variable | Description |
|---|---|
| `MIRAKL_BASE_URL` | Your Mirakl shop URL (e.g. `https://yourshop.mirakl.net`) |
| `MIRAKL_API_KEY` | Mirakl seller API key |
| `MIRAKL_SHOP_ID` | Mirakl shop ID (0 if single-shop) |
| `PLENTY_BASE_URL` | PlentyONE base URL |
| `PLENTY_USERNAME` | PlentyONE API user |
| `PLENTY_PASSWORD` | PlentyONE API password |
| `PLENTY_REFERRER_ID` | Marketplace referrer ID in PlentyONE |
| `PLENTY_WAREHOUSE_ID` | Default warehouse ID |
| `PLENTY_PLENTY_ID` | PlentyONE client/store ID |
| `TRAEFIK_DOMAIN` | Domain for Traefik routing |

### 2. Create Traefik network (once)

```bash
docker network create traefik_default
```

### 3. Run validation script

```bash
chmod +x scripts/validate.sh
./scripts/validate.sh
```

### 4. Start services

```bash
docker-compose up -d
```

### 5. Import SKU mappings

Prepare a CSV file:
```csv
mirakl_sku,plenty_variant_id,plenty_sku,ean
SKU-CHANEL-NO5,12345,CH-NO5,3145891115308
SKU-DIOR-JADORE,12346,DR-JA,3348901419093
```

Import:
```bash
python scripts/import_sku_mapping.py --file your_mapping.csv
# Validate first:
python scripts/import_sku_mapping.py --file your_mapping.csv --dry-run
```

---

## Operations

### Reading logs

```bash
# All services
docker-compose logs -f

# Worker only (order processing)
docker-compose logs -f worker

# API only
docker-compose logs -f api

# Filter by level (logs are JSON, use jq)
docker-compose logs -f worker | jq 'select(.level == "error")'

# Find failed orders
docker-compose logs -f | jq 'select(.event | startswith("order_service")) | {event, order_id, error}'
```

### Handling SKU_NOT_FOUND errors

When orders land in ERROR state with `SKU_NOT_FOUND`:

1. **Find affected orders:**
   ```sql
   SELECT mirakl_order_id, error_message, created_at
   FROM order_sync
   WHERE status = 'ERROR'
   AND error_message LIKE 'SKU_NOT_FOUND%';
   ```

2. **Add the missing SKU to the mapping:**
   ```bash
   echo "MISSING-SKU,12345,," >> new_mappings.csv
   python scripts/import_sku_mapping.py --file new_mappings.csv
   ```

3. **Retry the failed order** (via API):
   ```bash
   curl -X POST http://localhost:8000/orders/MRK-001/retry
   ```
   The next import cycle (within 5 min) will re-process it.

### Manual re-trigger of failed orders

```bash
# Single order
curl -X POST https://connector.domain.de/orders/{mirakl_order_id}/retry

# All ERROR orders (via DB)
psql -U connector -d connector -c "
  UPDATE order_sync
  SET status='NEW', error_count=0, error_message=NULL
  WHERE status='ERROR';
"
```

### Monitoring / Status

```bash
# Order counts by status
curl https://connector.domain.de/status

# Health check
curl https://connector.domain.de/health

# Celery worker status
docker-compose exec worker celery -A app.tasks.celery_app.celery_app inspect active

# Scheduled tasks
docker-compose exec worker celery -A app.tasks.celery_app.celery_app inspect scheduled
```

### Dry-run mode (safe testing)

```bash
DRY_RUN=true docker-compose up
```
In dry-run mode all API calls are simulated — no writes to Mirakl or PlentyONE.

---

## Running Tests

```bash
# Unit tests (no external dependencies)
pytest tests/unit/ -v

# Integration tests (requires PostgreSQL)
TEST_DATABASE_URL=postgresql+asyncpg://connector:secret@localhost:5432/connector_test \
  pytest tests/integration/ -v

# All tests with coverage
pytest --cov=app --cov-report=term-missing
```

---

## Traefik Integration

The API service exposes itself via Traefik labels in `docker-compose.yml`.
Ensure:
1. Traefik is running with `websecure` entrypoint and `letsencrypt` cert resolver
2. `traefik_default` external network exists
3. `TRAEFIK_DOMAIN` is set in `.env`

Access: `https://{TRAEFIK_DOMAIN}/health`

---

## Extending for AT/CH Markets

The current implementation hardcodes `countryId: 1` (Germany).
To support Austria (countryId: 40) or Switzerland (countryId: 58):
1. Add country code mapping in `plenty_client.py → _build_plenty_address()`
2. Use `MiraklAddress.country_iso_code` to determine `countryId`

---

## SaaS Operations Quick Reference

```bash
# Bootstrap a new tenant (returns api_key — store it!)
python scripts/saas_admin.py tenant create --name "Acme" --email ops@acme.de --quota 200

# Add their Mirakl shop
python scripts/saas_admin.py mirakl-conn add \
    --tenant <uuid> --label "Douglas DE" \
    --base-url https://acme.mirakl.net --api-key <KEY> --shop-id 0

# Add their Plenty mandant (with auto-generated webhook secret)
python scripts/saas_admin.py plenty-conn add \
    --tenant <uuid> --label "Acme Plenty" \
    --base-url https://p12345.my.plentysystems.com \
    --user wismo-readonly --password <PW> \
    --plenty-id 12345 --gen-webhook-secret

# Live ping the credentials
python scripts/saas_admin.py mirakl-conn test --id <uuid>
python scripts/saas_admin.py plenty-conn test --id <uuid>

# Discover Plenty referrer/warehouse/status IDs for onboarding
python scripts/discover_plenty_ids.py --conn-id <plenty_conn_uuid>

# Usage report
python scripts/saas_admin.py usage report
python scripts/saas_admin.py usage report --tenant <uuid>

# E2E smoke test (live Plenty test instance + mocked Mirakl)
PLENTY_BASE_URL=... PLENTY_USERNAME=... PLENTY_PASSWORD=... \
    python scripts/smoke_e2e.py
```

## Running Tests

```bash
# Unit tests — fully mocked, fast (CI gate)
pytest -v -m "not integration"

# Integration tests — needs real Postgres at TEST_DATABASE_URL,
# external HTTP still mocked
TEST_DATABASE_URL=postgresql+asyncpg://connector:secret@localhost:5432/connector_test \
    pytest -v -m integration
```

## Webhooks

* **Plenty event-procedure** → `POST /webhooks/plenty/<tenant_uuid>/order-status`
  Auth: `X-Plenty-Signature: sha256=<hex>` (HMAC of raw body) or `?secret=...` query.
* **Stripe** → `POST /billing/stripe-webhook` (set `STRIPE_WEBHOOK_SECRET` env).

## Required environment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | postgresql+asyncpg://... — runtime DB connection |
| `REDIS_URL` | redis://... — Celery broker |
| `FERNET_KEY` | At-rest encryption key for tenant API creds (`Fernet.generate_key()`) |
| `ADMIN_API_KEY` | Long random token for /panel/admin and admin CLI |
| `STRIPE_SECRET_KEY` | sk_test_... or sk_live_... |
| `STRIPE_WEBHOOK_SECRET` | whsec_... — verifies incoming Stripe events |
| `STRIPE_PRICE_DEFAULT` | price_... for the €29 plan |
| `OPS_EMAIL` | Where daily ops digest is sent (defaults to romic@vagabond-consulting.com) |

The legacy single-tenant env vars (`MIRAKL_*`, `PLENTY_*`) are still read at
migration time to seed the `default` tenant. They become inert after a tenant
is created via `saas_admin.py tenant create`.

## Deploy

* Push to `main`/`master` → auto-deploy to **staging** (`staging.plenty-mirakl.420.ovh`)
* Tag `v*.*.*` → auto-deploy to **prod** (`plenty-mirakl.420.ovh`)
* Manual rollback: `ssh servicebox 'sudo /opt/plenty-mirakl-connector/bin/deploy.sh prod --rollback'`
