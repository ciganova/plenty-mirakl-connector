# Customer Demo — Plenty/Mirakl Connector

**Live PROD**: https://connector.vagabond-consulting.com  ✅
**Live STAGING**: https://staging-connector.vagabond-consulting.com  ✅

Both routed via **Cloudflare Tunnel** (`cloudflared-pmc` on servicebox), TLS by Cloudflare, Traefik bypassed.

## Demo tenants

| Env     | Tenant ID                              | API Key                                            |
|---------|----------------------------------------|----------------------------------------------------|
| PROD    | `1948b26e-6bcc-4b83-9bf8-020c582d160d` | `pmc_PiWhKpCztEmeQSXmWDdqboqO3mYAieM7tq2qs8xwFfc`  |
| STAGING | `60bae348-91ed-4ff7-8f5c-55637ef61061` | `pmc_mwWU3-2cUBw7I4fmQ5zI8ox5p6lDIeOqimEm2EvUQ-8`  |

Both wired to: Mirakl Douglas2-Dev sandbox (shop 2297) + Plenty p73736 sandbox.

## Demo flow (5 clicks)

1. **Landing** — open https://connector.vagabond-consulting.com on phone, scroll Pricing + FAQ.
2. **Trial** — click "Start trial" → Stripe Checkout (test card `4242 4242 4242 4242`, any future date, any CVC).
3. **Panel** — https://connector.vagabond-consulting.com/panel?key=pmc_PiWhKpCztEmeQSXmWDdqboqO3mYAieM7tq2qs8xwFfc
4. **Trigger order sync** — paste in terminal:
   ```bash
   curl -X POST https://connector.vagabond-consulting.com/webhooks/plenty/1948b26e-6bcc-4b83-9bf8-020c582d160d/order-status \
     -H 'Content-Type: application/json' \
     -d '{"order_id":12345,"status":7,"tracking_number":"DHL-123456"}'
   ```
5. **OR23 audit** — show Mirakl audit log entry in panel → "Tracking pushed to Douglas in 1.2s".

## Pricing pitch

- **€29/mo** flat, includes 200 synced orders.
- **€0.15** per extra order, no hard stop.
- vs. native Mirakl connectors (€99-199/mo + setup fees).
- 14-day trial, cancel any time.

## What's stubbed for demo

- Stripe in **TEST mode** (one-line key swap when customer signs).
- Mirakl operators provisioned: Douglas, Shop-Apotheke. Otto, Kaufland on request (~1 day each).
- Shared-infra pilot — best-effort 99%, no signed SLA yet.

## Smoke checks (run any time)

```bash
curl -fsS https://connector.vagabond-consulting.com/healthz
curl -fsS https://staging-connector.vagabond-consulting.com/healthz
curl -X POST https://connector.vagabond-consulting.com/billing/checkout \
  -H "X-Api-Key: pmc_PiWhKpCztEmeQSXmWDdqboqO3mYAieM7tq2qs8xwFfc" \
  -H "Content-Type: application/json" -d '{"plan":"starter"}'
# -> 303 to checkout.stripe.com/...
```

## Stripe webhook

`we_1TX0LtGtDMICJxj9228XjPnp` → https://connector.vagabond-consulting.com/billing/stripe-webhook (TEST mode).

## Infra note

Routing is **Cloudflare Tunnel** (`cloudflared-pmc` container, joined to `traefik_default` + `plenty-mirakl-prod_internal` + `pmc-staging_internal`). Survives Traefik restarts because it doesn't use Traefik. Tunnel ID `2c82f717-1846-4095-ad30-9cc0fb7aad48`.

> Old hostname `staging.connector.vagabond-consulting.com` exists but Cloudflare free Universal SSL only covers `*.vagabond-consulting.com` (1 level). Use `staging-connector.vagabond-consulting.com` instead.
