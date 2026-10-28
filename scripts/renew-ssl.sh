#!/bin/bash
#
# AI Catalogue - SSL certificate renewal.
#
# This is the ONLY renewal mechanism for this host. certbot's own
# snap.certbot.renew.timer must stay disabled: it renews against the system
# store (/etc/letsencrypt) which nothing here serves, has no hook to copy a new
# certificate into nginx/ssl, and uses the standalone authenticator without
# stopping nginx - so it fails on "port 80 in use" every time it runs. Check
# with:  systemctl is-enabled snap.certbot.renew.timer   (expected: disabled)
#
# Renewal is driven by the per-certificate hooks in
#   certbot/certs/renewal/aicc.uksouth.cloudapp.azure.com.conf
# which certbot runs itself:
#   pre_hook    stop nginx, freeing port 80 for the standalone challenge
#   renew_hook  copy the new cert into nginx/ssl/ and set ownership/permissions
#   post_hook   start nginx again, which picks the new certificate up
# Renewal therefore means a short outage while nginx is down. Nothing is
# renewed until the certificate is inside its 30-day window; outside it,
# certbot exits 0 having done nothing.
#
# Runs as a normal user; sudo is needed for the standalone challenge to bind
# port 80 and for the root-owned certbot directories.

set -uo pipefail     # deliberately NOT -e: certbot's exit code is handled below

DOMAIN="aicc.uksouth.cloudapp.azure.com"
PROJECT_DIR="/home/alokkrsahu/ai_catalogue"
LOCK_FILE="/tmp/ai-catalogue-ssl-renew.lock"

cd "$PROJECT_DIR" || { echo "❌ Cannot cd to $PROJECT_DIR"; exit 1; }

# One renewal at a time. Without this a slow run that overruns the next cron
# tick would have two certbots competing for port 80, and the pre/post hooks
# would fight over nginx. Non-blocking: a second invocation exits rather than
# queueing behind the first.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "⏭️  $(date -Is) another renewal is already running (lock: $LOCK_FILE) - exiting."
    exit 0
fi

echo "🔄 $(date -Is) AI Catalogue SSL certificate renewal"
echo "Domain: $DOMAIN"
echo ""

if [ -f .env ]; then
    # shellcheck disable=SC1091
    source .env
fi

echo "📜 Running certbot renew..."
sudo certbot renew \
    --config-dir "$PROJECT_DIR/certbot/certs" \
    --work-dir   "$PROJECT_DIR/certbot/work" \
    --logs-dir   "$PROJECT_DIR/certbot/logs"
RESULT=$?

if [ $RESULT -ne 0 ]; then
    # Previously this branch was unreachable: `set -e` aborted the script at the
    # certbot call, so a failed renewal printed nothing at all and the cron log
    # simply stopped. A failure was invisible until the certificate expired.
    echo "❌ $(date -Is) certbot exited $RESULT - renewal FAILED"
    echo "🔍 See $PROJECT_DIR/certbot/logs/letsencrypt.log"
    echo "🔍 If nginx did not come back up: docker compose up -d nginx"
    exit 1
fi

# certbot exits 0 both when it renewed and when nothing was due, so report what
# the certificate on disk actually says rather than implying work was done.
CERT="$PROJECT_DIR/certbot/certs/live/$DOMAIN/fullchain.pem"
if sudo test -r "$CERT"; then
    NOT_AFTER=$(sudo openssl x509 -in "$CERT" -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -n "$NOT_AFTER" ]; then
        EXPIRY_EPOCH=$(date -d "$NOT_AFTER" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))
        echo "✅ $(date -Is) certbot completed. Certificate expires $NOT_AFTER (${DAYS_LEFT} days)."
        if [ "$DAYS_LEFT" -lt 0 ]; then
            echo "❌ The certificate has EXPIRED. Renewal is not working."
            exit 1
        elif [ "$DAYS_LEFT" -lt 10 ]; then
            echo "⚠️  Fewer than 10 days left and it was not renewed - investigate now."
            exit 1
        fi
    else
        echo "✅ $(date -Is) certbot completed (could not read the expiry date)."
    fi
else
    echo "⚠️  $(date -Is) certbot exited 0 but $CERT is not readable."
fi

# What nginx is serving can lag what is on disk if renew_hook failed.
SERVED=$(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
         | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$SERVED" ]; then
    echo "🌐 nginx is serving a certificate expiring $SERVED"
    if [ -n "${NOT_AFTER:-}" ] && [ "$SERVED" != "$NOT_AFTER" ]; then
        echo "⚠️  Served certificate differs from the one on disk - renew_hook may not have run."
        echo "    Copy it across and reload:  see renew_hook in certbot/certs/renewal/$DOMAIN.conf"
    fi
else
    echo "⚠️  Could not read the served certificate - is nginx up? (docker compose ps nginx)"
fi
