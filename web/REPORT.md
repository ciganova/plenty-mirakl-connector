# PlentyMirakl Connector — Landing page (web/) report

## Files produced
- web/index.html, web/imprint.html, web/terms.html, web/privacy.html
- web/style.css, web/Dockerfile, web/nginx.conf, web/og.png (1200x630, 49 KB)

## Conversion-design rationale
Hero leads with outcome+timeframe ("Mirakl orders, in Plenty, in 60 seconds") not feature list.
Tracking promise on second line because it is the second-biggest pain.
Trust strip lists every supported operator below fold so the visitor self-qualifies in one second.
Problem block precedes How so the visitor feels the pain before reading the mechanism.
Pricing within first 1.5 viewports: single plan card next to a compare-row anchoring vs €99-199 native connectors.
Live status uses HTMX hx-get="/api/public/stats" every 30s, fallback shows honest zeros.
Primary CTA repeats 4x, all -> /billing/checkout?plan=starter.
No carousel/popups/cookie banner; no third-party cookies set.
Page weight: index 14.5 KB + OG 49 KB + CSS 0.7 KB + Tailwind/HTMX CDN ~50 KB ~= 115 KB total, well under 200 KB.

## Smoke test
python -m http.server --directory web/ -> all routes 200.
No personal name anywhere. Pricing copy consistent: 5x EUR29, 5x "200", 2x "0.15".

## Unresolved deps on backend agent
1. /api/public/stats - HTML-fragment endpoint feeding the live-status block; expected shape: three .border.rounded.p-6 cards. Static zeros render until live.
2. /billing/checkout?plan=starter - Stripe Checkout redirect. If not live by launch, swap the four href occurrences in index.html to mailto:contact@vagabond-consulting.com?subject=Trial%20request.
3. /panel - dashboard login link in nav, same fallback.
4. docker-compose.staging.yml does not yet exist in repo. Backend agent owns it. Add a `landing` service building ./web, with Traefik labels:
   - rule Host(staging.plenty-mirakl.420.ovh), priority=1 (catches all)
   - FastAPI app routers must claim higher priority on /api /panel /webhooks /billing (>=10)

## A/B test backlog (next week)
1. Hero: current outcome+timeframe vs loss-frame "Stop retyping Douglas orders into Plenty."
2. Pricing: keep EUR99-199 compare-row vs lead with per-order calculator.
3. CTA copy: "Start 14-day trial" vs "Sync your first order free".
4. Trust strip: under hero (current) vs sticky bar above nav.
5. Live-status: above FAQ (current) vs promoted to second section after hero.
