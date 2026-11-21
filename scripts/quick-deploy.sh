#!/bin/bash
# scripts/quick-deploy.sh — Near-zero downtime update for code changes
# Use when: git pull with only code/migration/config changes
# Downtime: < 2 seconds (Django auto-reload)

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
echo "⚡ Quick deploy (code-only, < 2 seconds downtime)..."

# Pull latest code — volume mounts immediately reflect changes
echo "📥 Pulling latest code..."
git pull

# Run migrations if needed (safe while server is running)
echo "📦 Running migrations..."
docker compose exec backend python manage.py migrate --noinput 2>/dev/null || true

# Collect static files if needed
docker compose exec backend python manage.py collectstatic --noinput 2>/dev/null || true

# Django runserver auto-detects .py file changes and reloads (~2 sec)
# Frontend Vite HMR auto-detects .svelte/.ts changes (instant)
# If auto-reload didn't trigger (rare), touch a file to force it:
docker compose exec backend touch /app/core/settings.py

# Reload nginx config (zero downtime — graceful)
docker compose exec nginx nginx -s reload 2>/dev/null || true

echo ""
echo "✅ Deploy complete!"
echo "   Backend: auto-reloaded (~2 seconds)"
echo "   Frontend: HMR (instant)"
echo "   Nginx: graceful reload (0 seconds)"
