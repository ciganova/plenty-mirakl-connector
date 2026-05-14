# Customer Demo — Plenty/Mirakl Connector

**Demo URL (PROD)**: `https://connector.vagabond-consulting.com`  ⚠️ see Known Issue below
**Staging URL**: `https://staging.connector.vagabond-consulting.com`  ⚠️ see Known Issue below
**Server**: 147.189.175.131 (servicebox)

## Demo tenant credentials

| Env     | Tenant ID                              | API Key                                            |
|---------|----------------------------------------|----------------------------------------------------|
| PROD    | `1948b26e-6bcc-4b83-9bf8-020c582d160d` | `pmc_PiWhKpCztEmeQSXmWDdqboqO3mYAieM7tq2qs8xwFfc`  |
| STAGING | `60bae348-91ed-4ff7-8f5c-55637ef61061` | `pmc_mwWU3-2cUBw7I4fmQ5zI8ox5p6lDIeOqimEm2EvUQ-8`  |

Both tenants have:
- Mirakl: Douglas2-Dev sandbox (shop 2297) — **ACTIVE**
- Plenty: p73736 sandbox — **ACTIVE**

## ⚠️ Known issue — Traefik routing blocked

Both URLs currently return Traefik default 404. Root cause: Traefik docker-provider on the box stopped picking up labels from new containers some time after 2026-03-31; existing routers (paketwo.de, hustle.420.ovh, etc.) still work.

The app stack itself is healthy and reachable inside Docker:
```
docker exec plenty-mirakl-prod-api-1 python3 -c \
  'import urllib.request; print(urllib.request.urlopen("http://localhost:8000/healthz").read())'
# -> {"ok":true,"ts":...}
```

**Workaround for the live customer call** until routing is fixed:
- SSH-tunnel port 8000 from the api container to your laptop, then demo against `http://localhost:8000`.
- OR demo via the staging.plenty-mirakl.420.ovh URL if Traefik state recovers (router was created via legacy stack 11h ago).

**Fix path for tomorrow** (15 min):
1. Investigate why `traefik` container's docker provider rejects pmc-* / connectorprod / connectorstaging labels (logs show ZERO references to these router names).
2. Suspect: stale state in `/letsencrypt/acme.json` or in-memory router map after multiple restarts. Try a 5-minute Traefik downtime + `docker system prune --filter label=traefik.enable=false` before bringing it back.
3. Alternative: use Traefik file-provider via a static config file; bypasses the docker-label scanner entirely.

## Demo flow (when routing fixed)

1. **Show landing**: open `https://connector.vagabond-consulting.com/` — hero, pricing, FAQ.
2. **Click "Start trial"** → redirects to Stripe Checkout (test card `4242 4242 4242 4242`, any future date, any CVC).
3. **Open panel** with the API key:
   `https://connector.vagabond-consulting.com/panel?key=pmc_PiWhKpCztEmeQSXmWDdqboqO3mYAieM7tq2qs8xwFfc`
4. **Demo the order webhook** without needing a real Mirakl push:
   ```bash
   curl -X POST https://connector.vagabond-consulting.com/webhooks/plenty/1948b26e-6bcc-4b83-9bf8-020c582d160d/order-status \
     -H 'Content-Type: application/json' \
     -d '{"order_id": 12345, "status": 7, "tracking_number": "DHL-123456"}'
   ```
5. **Show Mirakl OR23 audit log** in the panel.

## Stripe webhooks (TEST mode, both verified wired)

| Env     | Webhook ID                           | Endpoint                                                                  |
|---------|--------------------------------------|---------------------------------------------------------------------------|
| PROD    | `we_1TX0LtGtDMICJxj9228XjPnp`        | https://connector.vagabond-consulting.com/billing/stripe-webhook         |
| STAGING | (existing)                           | https://staging.connector.vagabond-consulting.com/billing/stripe-webhook |

## Pricing pitch (memo for the call)

- **€29/mo** includes 200 synced orders.
- **€0.15** per extra order, no hard stop.
- Compare native Mirakl connectors: **€99-199/mo** + per-operator setup fees.
- 14-day trial, cancel any time.

## Disclose if asked

- Stripe is in test-mode (one-click switch to live keys when customer signs).
- Mirakl operators currently provisioned: Douglas, Shop-Apotheke (more on request, ~1-2 days each).
- No SLA contract yet — best-effort 99% on shared infra during the pilot.
