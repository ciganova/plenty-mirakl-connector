#!/usr/bin/env bash
# plenty-mirakl-connector deploy script — laeuft auf servicebox unter
# /opt/plenty-mirakl-connector{,-staging}/.
#
# Usage:
#   bin/deploy.sh staging                # build + recreate alle staging-Container
#   bin/deploy.sh prod                   # build + rolling-recreate prod ohne Downtime
#   bin/deploy.sh prod --skip-build      # nur restart (z.B. nach env-Change)
#   bin/deploy.sh prod --migrate         # alembic upgrade head VOR dem swap (mit pg_dump)
#   bin/deploy.sh prod --ref v0.1.0      # checkout git ref + deploy (rollback)
#   bin/deploy.sh prod --rollback        # shortcut: --ref auf vorletzten tag
#
# Mirrors the wismo-saas safety pipeline (see ~/.claude/skills/safe-deploy-pipeline/):
#   - pg_dump pre-migration backup
#   - alembic head pre-flight check
#   - rolling recreate with --wait + healthcheck
#   - smoke retry against /healthz
#   - additive only — no destructive actions on existing servicebox stack
set -euo pipefail

ENV="${1:-}"; shift || true
SKIP_BUILD=0; DO_MIGRATE=0; GIT_REF=""; DO_ROLLBACK=0
while (( $# > 0 )); do
    case "$1" in
        --skip-build) SKIP_BUILD=1; shift ;;
        --migrate)    DO_MIGRATE=1; shift ;;
        --ref)        GIT_REF="${2:-}"; shift 2 ;;
        --ref=*)      GIT_REF="${1#--ref=}"; shift ;;
        --rollback)   DO_ROLLBACK=1; shift ;;
        *)            echo "unknown flag: $1"; exit 2 ;;
    esac
done

case "$ENV" in
    staging)
        DIR=/opt/plenty-mirakl-connector-staging
        FILE=docker-compose.staging.yml
        PROJECT=pmc-staging
        ENVFILE=.env.staging
        URL=https://staging.connector.vagabond-consulting.com
        DB_CONTAINER=pmc-staging-postgres-1
        BACKUP_DIR=/var/backups/pmc-staging
        ;;
    prod)
        DIR=/opt/plenty-mirakl-connector
        FILE=docker-compose.prod.yml
        PROJECT=
        ENVFILE=.env
        URL=https://connector.vagabond-consulting.com
        DB_CONTAINER=plenty-mirakl-connector-postgres-1
        BACKUP_DIR=/var/backups/pmc
        ;;
    *)
        echo "Usage: $0 staging|prod [--skip-build] [--migrate] [--ref <git-ref>] [--rollback]"
        exit 2 ;;
esac

cd "$DIR"
DC=(docker compose ${PROJECT:+-p "$PROJECT"} --env-file "$ENVFILE" -f "$FILE")

# Git ref / rollback
if [[ $DO_ROLLBACK -eq 1 && -z "$GIT_REF" ]]; then
    GIT_REF=$(git -C "$DIR" describe --tags --abbrev=0 HEAD~1 2>/dev/null || echo "HEAD~1")
    echo "▶ --rollback to $GIT_REF"
fi
if [[ -n "$GIT_REF" ]]; then
    echo "▶ checkout $GIT_REF"
    if ! git -C "$DIR" rev-parse --verify "$GIT_REF" >/dev/null 2>&1; then
        git -C "$DIR" fetch --all --tags --prune 2>&1 | tail -3
    fi
    git -C "$DIR" checkout --detach "$GIT_REF" 2>&1 | tail -3
fi

echo "================================================================="
echo " Deploying $ENV ($URL) — dir=$DIR compose=$FILE"
echo "================================================================="

# Build
if [[ $SKIP_BUILD -eq 0 ]]; then
    echo "▶ build"
    "${DC[@]}" build api worker scheduler
fi

# pg_dump pre-migration backup
if [[ $DO_MIGRATE -eq 1 ]]; then
    echo "▶ pg_dump pre-migration backup → $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    BACKUP_FILE="$BACKUP_DIR/${TS}-pre-deploy.sql.gz"
    PGPW=$(grep -E '^POSTGRES_PASSWORD=' "$ENVFILE" | head -1 | cut -d= -f2-)
    if [[ -z "$PGPW" ]]; then
        echo "❌ POSTGRES_PASSWORD missing in $ENVFILE"; exit 8
    fi
    if ! docker exec -e PGPASSWORD="$PGPW" "$DB_CONTAINER" \
            pg_dump -U connector -d connector --no-owner --no-privileges \
                | gzip -9 > "$BACKUP_FILE"; then
        echo "❌ pg_dump failed"; rm -f "$BACKUP_FILE"; exit 9
    fi
    SIZE=$(stat -c '%s' "$BACKUP_FILE" 2>/dev/null || stat -f '%z' "$BACKUP_FILE")
    if [[ "$SIZE" -lt 1024 ]]; then
        echo "❌ backup suspiciously small ($SIZE bytes)"; rm -f "$BACKUP_FILE"; exit 9
    fi
    echo "  ✓ backup: $BACKUP_FILE ($((SIZE/1024)) KB)"

    # rotate keep-30
    OLD=$(ls -1t "$BACKUP_DIR"/*-pre-deploy.sql.gz 2>/dev/null | tail -n +31 || true)
    [[ -n "$OLD" ]] && echo "$OLD" | xargs -r rm -f
fi

# Alembic state check
echo "▶ alembic state check"
CURRENT_REV=$("${DC[@]}" exec -T api alembic current 2>/dev/null \
              | grep -oE '[0-9]{3}_[a-z0-9_]+' | head -1 || echo "")
HEAD_REV=$("${DC[@]}" run --rm --no-deps api alembic heads 2>/dev/null \
           | grep -oE '[0-9]{3}_[a-z0-9_]+' | head -1 || echo "")
echo "  current=${CURRENT_REV:-<none>} head=${HEAD_REV:-<none>}"

if [[ -n "$CURRENT_REV" && -n "$HEAD_REV" && "$CURRENT_REV" != "$HEAD_REV" ]]; then
    if [[ $DO_MIGRATE -eq 1 ]]; then
        echo "▶ alembic upgrade $CURRENT_REV → $HEAD_REV"
        if ! "${DC[@]}" run --rm --no-deps api alembic upgrade head; then
            echo "❌ alembic failed"; exit 10
        fi
    else
        echo "⚠ schema mismatch — re-run with --migrate"; exit 3
    fi
fi

# Rolling recreate
echo "▶ rolling recreate"
if ! "${DC[@]}" up -d --no-deps --wait --wait-timeout 90 api worker scheduler; then
    echo "❌ containers not healthy after 90s"
    "${DC[@]}" logs --tail 50 api worker scheduler
    exit 11
fi

# Smoke
echo "▶ smoke /healthz (max 6 retries)"
HTTP=000
for i in 1 2 3 4 5 6; do
    HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -m 10 "$URL/healthz" || echo "000")
    [[ "$HTTP" == "200" ]] && break
    echo "  attempt $i: $HTTP, retry in 3s"; sleep 3
done
if [[ "$HTTP" != "200" ]]; then
    echo "❌ /healthz returned $HTTP after 6 retries"
    "${DC[@]}" logs --tail 50 api
    echo "   rollback: bin/deploy.sh $ENV --rollback"
    exit 4
fi

echo "✅ deploy $ENV ok ($URL/healthz=200)"
"${DC[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
