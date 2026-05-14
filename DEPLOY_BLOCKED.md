# Deploy blockers — 2026-05-14 evening

## ✅ DONE

1. Migration 002 GRANT block split into 3 separate `op.execute()` calls (asyncpg can't run multi-statement prepared statements). Both staging + prod now run `alembic upgrade head` cleanly.
2. Cloudflare DNS records created (proxied=false):
   - `staging.connector.vagabond-consulting.com` → 147.189.175.131
   - `connector.vagabond-consulting.com` → 147.189.175.131
3. Compose files updated:
   - staging: routers `connectorstaging` + `connectorstaging-legacy` (both → service `connectorstaging`, port 8000)
   - prod: routers `connectorprod` + `connectorprod-legacy` (both → service `connectorprod`, port 8000)
4. Both stacks deployed and HEALTHY internally:
   - `pmc-staging-{api,worker,scheduler,postgres,redis}` under `/opt/plenty-mirakl-staging/`
   - `plenty-mirakl-prod-{api,worker,scheduler,postgres,redis}` under `/opt/plenty-mirakl-prod/`
   - Both api containers respond `{"ok":true,...}` on internal `/healthz`.
5. Demo tenants created on both envs with Douglas2-Dev Mirakl + p73736 Plenty connections.
6. Stripe prod webhook endpoint created: `we_1TX0LtGtDMICJxj9228XjPnp`.
7. Landing `web/index.html` og:url updated to `connector.vagabond-consulting.com`.
8. Legacy duplicate stack `plenty-mirakl-staging-*` (default project name) stopped + removed (volume preserved).

## 🔴 BLOCKED — Traefik docker-provider not picking up our labels

**Symptom:** All HTTPS requests to `connector.vagabond-consulting.com` and `staging.connector.vagabond-consulting.com` return Traefik default `404 page not found`. Same for the legacy `staging.plenty-mirakl.420.ovh` route.

**Investigated:**
- ✅ Container labels are correctly set (verified via `docker inspect`).
- ✅ Containers are on `traefik_default` network.
- ✅ Traefik is running (`docker ps`), no startup errors related to our routers.
- ✅ Other vagabond-consulting routers (drinkmate, ai-support-helper) DO appear in Traefik logs (with ACME errors but provider sees them).
- ❌ Zero mentions of `connectorstaging`, `connectorprod`, `pmc-staging`, or our hostnames anywhere in Traefik's full log history (212k lines).
- ❌ Tested with renamed router (`connectorstaging` was never in Traefik history) — still ignored.
- ❌ Force-recreated api container — no docker event picked up by Traefik.
- ❌ Restarted Traefik twice; provider config reads docker socket fine but skips our containers silently.
- ❌ A control nginx test was inconclusive due to bash backtick escape issue.

**Hypothesis:** The Traefik docker-provider's in-memory state is stuck. The ONLY way other recent containers got registered (e.g., `drinkmate`) was via a fresh provider scan after a Traefik cold restart — but our containers were already running through the restart and didn't trigger a fresh scan event.

**Fix to try first (5 min, low risk):**
1. `docker stop pmc-staging-api-1 plenty-mirakl-prod-api-1`
2. `docker restart traefik` (wait 30s for Traefik to settle, watch logs)
3. `docker start plenty-mirakl-prod-api-1` (wait 5s, check `docker logs traefik --since 30s | grep connectorprod`)
4. If router appears: `docker start pmc-staging-api-1` (same check)
5. If still blank: brick-test with a brand-new disposable nginx container with `traefik.enable=true` and a unique router name. If THAT works, our containers have something specific (project label?) that's broken.

**Fix to try second (15 min, medium risk):**
- Add a Traefik file-provider via a static yaml file mounted into traefik. Bypasses docker-label scanning entirely. **Requires editing Traefik's compose at `/home/miromic/services/services/docker-compose.yml`** — explicitly forbidden by user's "never break existing services" rule, so only do this with explicit user OK.

**Workaround for tonight's customer demo:**
- SSH-tunnel `ssh -L 8000:plenty-mirakl-prod-api-1:8000 miromic@147.189.175.131` and demo against `http://localhost:8000`.
- The whole API is fully functional internally — only TLS/routing layer is broken.

## Files added/changed this session

```
.deploy_staging.py        (sudo bundle simplified; DOMAIN updated)
.deploy_prod.py           (NEW — mirror of staging for prod)
docker-compose.staging.yml (router rename pmc-staging → connectorstaging + legacy router)
docker-compose.prod.yml    (router rename pmc → connectorprod + legacy router; resolver letsencrypt → mytls)
alembic/versions/002_multitenancy.py (GRANT block split, asyncpg fix)
bin/deploy.sh              (URL constants updated)
web/index.html             (og:url updated)
CUSTOMER_DEMO.md           (NEW — demo cheat sheet)
DEPLOY_BLOCKED.md          (NEW — this file)
```

## Credentials saved

- `~/.claude/credentials/plenty-mirakl-prod.env` — fresh prod secrets + demo tenant info
- `~/.claude/credentials/plenty-mirakl-staging.env` — appended demo tenant info
