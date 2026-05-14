#!/bin/bash
# Pre-flight validation script for Douglas Mirakl Connector
# Run before first deploy or after config changes.
set -e

echo "=== PlentyONE-Mirakl Connector: Pre-flight Check ==="

# ── 1. Environment Variables ─────────────────────────────────────────────────
echo ""
echo "1. Checking required environment variables..."

REQUIRED_VARS=(
  MIRAKL_BASE_URL
  MIRAKL_API_KEY
  PLENTY_BASE_URL
  PLENTY_USERNAME
  PLENTY_PASSWORD
  DATABASE_URL
  REDIS_URL
)

MISSING=0
for VAR in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!VAR}" ]; then
    echo "  ❌ Missing: $VAR"
    MISSING=$((MISSING + 1))
  else
    echo "  ✓ $VAR is set"
  fi
done

if [ $MISSING -gt 0 ]; then
  echo ""
  echo "ERROR: $MISSING required environment variable(s) missing."
  echo "Copy .env.example to .env and fill in the values."
  exit 1
fi

# ── 2. Traefik Network ───────────────────────────────────────────────────────
echo ""
echo "2. Checking Traefik network..."
if docker network inspect traefik_default >/dev/null 2>&1; then
  echo "  ✓ traefik_default network exists"
else
  echo "  Creating traefik_default network..."
  docker network create traefik_default
  echo "  ✓ traefik_default network created"
fi

# ── 3. Build images ──────────────────────────────────────────────────────────
echo ""
echo "3. Building Docker images..."
docker-compose build --quiet
echo "  ✓ Images built"

# ── 4. Database connectivity ─────────────────────────────────────────────────
echo ""
echo "4. Testing database connectivity..."
docker-compose run --rm --no-deps api python -c "
import asyncio
from app.models.database import engine
async def test():
    async with engine.connect() as conn:
        from sqlalchemy import text
        await conn.execute(text('SELECT 1'))
    await engine.dispose()
asyncio.run(test())
print('DB connection OK')
"

# ── 5. Run Alembic migrations ────────────────────────────────────────────────
echo ""
echo "5. Running database migrations..."
docker-compose run --rm --no-deps api alembic upgrade head
echo "  ✓ Migrations applied"

# ── 6. Start services and check health ──────────────────────────────────────
echo ""
echo "6. Starting services and checking health endpoint..."
docker-compose up -d
sleep 15

HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)
if [ "$HEALTH_STATUS" == "200" ]; then
  echo "  ✓ /health returned 200"
else
  echo "  ❌ /health returned $HEALTH_STATUS"
  docker-compose logs api
  exit 1
fi

# ── 7. Check Celery worker ───────────────────────────────────────────────────
echo ""
echo "7. Checking Celery worker..."
sleep 5
WORKER_STATUS=$(docker-compose exec -T worker celery -A app.tasks.celery_app.celery_app inspect ping 2>&1)
if echo "$WORKER_STATUS" | grep -q "pong"; then
  echo "  ✓ Celery worker is responding"
else
  echo "  ⚠ Could not verify Celery worker (may still be starting)"
fi

echo ""
echo "=== All systems operational. Ready for Douglas integration. ==="
echo ""
echo "Next steps:"
echo "  1. Import SKU mappings:  python scripts/import_sku_mapping.py --file your_mapping.csv"
echo "  2. Verify Traefik route: https://${TRAEFIK_DOMAIN:-connector.domain.de}/health"
echo "  3. Monitor logs:         docker-compose logs -f worker"
