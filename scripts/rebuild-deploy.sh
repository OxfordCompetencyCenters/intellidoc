#!/bin/bash
# scripts/rebuild-deploy.sh — Rebuild with minimal downtime
# Use when: requirements.txt, package.json, or Dockerfile changed
# Downtime: < 10 seconds

set -e

# --- stack guard -------------------------------------------------------------
# These scripts drive `docker compose` with no -f flags, so Compose auto-merges
# docker-compose.override.yml and acts on the DEVELOPMENT stack. If the host is
# currently running the production stack, that would swap the built frontend for
# the Vite dev server -- putting a dev server back on a public port. Refuse
# instead, and point at the production equivalent.
if docker ps --format '{{.Names}}' | grep -qx 'ai_catalogue_frontend'; then
    echo "❌ The production stack is running (ai_catalogue_frontend is up)."
    echo "   This script targets the development stack and would replace the"
    echo "   built frontend with the Vite dev server."
    echo
    echo "   For production use:"
    echo "     docker compose -f docker-compose.yml -f docker-compose.prod.yml \\"
    echo "       up -d --no-deps backend frontend nginx"
    echo
    echo "   To switch back to development deliberately:"
    echo "     docker compose stop frontend && docker compose up -d"
    exit 1
fi
# -----------------------------------------------------------------------------
echo "🔨 Rebuild deploy (dependency changes)..."

# Pull latest code
echo "📥 Pulling latest code..."
git pull

# Build new images WHILE old containers still serve traffic
echo "📦 Building new images (app still running)..."
docker compose -f docker-compose.yml -f docker-compose.override.yml build backend frontend-dev

# Run migrations before swap
echo "📦 Running migrations..."
docker compose exec backend python manage.py migrate --noinput 2>/dev/null || true

# Restart only app services (databases stay running)
echo "🔄 Swapping to new containers..."
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-deps --force-recreate backend
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --no-deps --force-recreate frontend-dev

# Wait for backend health
echo "⏳ Waiting for backend..."
TIMEOUT=180
COUNTER=0
while [ $COUNTER -lt $TIMEOUT ]; do
    if curl -f -s http://localhost:8000/admin/ > /dev/null 2>&1; then
        echo "✅ Backend healthy!"
        break
    fi
    if [ $((COUNTER % 10)) -eq 0 ]; then
        echo "   ⏱️  Waiting... ($COUNTER/${TIMEOUT}s)"
    fi
    sleep 2
    COUNTER=$((COUNTER + 2))
done

if [ $COUNTER -ge $TIMEOUT ]; then
    echo "⚠️  Backend health check timed out. Check logs: docker compose logs backend"
fi

# Reload nginx
docker compose exec nginx nginx -s reload 2>/dev/null || true

echo ""
echo "✅ Rebuild complete! Databases were never stopped."
docker compose ps
