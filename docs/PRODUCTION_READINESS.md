# Production Readiness

Derived by auditing the production overlay against the **running** development
stack on 2026-10-28, not from reading the compose files. Everything below was
verified against the live system.

The live deployment is still the development stack. This document records what
had to change before switching could be safe, and what must happen on the day.

---

## 1. What would have broken

Seven behaviours worked in development and would have stopped working under
`-f docker-compose.yml -f docker-compose.prod.yml`. All are fixed.

| # | Defect | Consequence | Fix |
|---|---|---|---|
| 1 | `nginx.prod.conf` had no `/chatgpt-analytics/` upstreams or locations | A separate live project, serving 200 today, goes offline | Upstreams and both location blocks added |
| 2 | `client_max_body_size` 100M in prod, 500M in dev | Uploads between 100M and 500M rejected | Raised to 500M |
| 3 | Prod mounted `backend_media`, dev writes `backend_media_dev` | All 60 uploaded documents (43 MB) invisible — `backend_media` is empty | Prod mounts the volumes that hold the data |
| 4 | `core/urls.py` adds the `/media/` route only when `DEBUG` is true | Every document download 404s | nginx serves `/media/` from the volume |
| 5 | Nothing serves `/static/` under Gunicorn + `DEBUG=False` | Django admin and DRF browsable API unstyled | nginx serves `/static/` from `backend_static` |
| 6 | `settings.py` never read the overlay's `SECURE_*` vars | The advertised hardening did nothing | Wired, with safe defaults |
| 7 | nginx prefix `location /health` | Shadowed the backend probes | Made exact; proxied `/health/` added |

### Notes on two of these

**#3 — why the volume keeps a `_dev` name.** `docker-compose.override.yml` has
always pointed the running stack at `backend_media_dev`, so that is where real
user data lives. Production now mounts the same volume rather than migrating a
live one, which needs no copy and no downtime. The name is historical. **Do not
rename it to `backend_media` without migrating the data first.**

**#5 — why nginx, not WhiteNoise.** WhiteNoise is the usual answer, but adding
it to `MIDDLEWARE` would crash the running backend on its next reload, before
its image contained the package. Serving static from nginx needs no new
dependency and is the more conventional split for Gunicorn behind a proxy.

### Also fixed

- `deploy.replicas: 2` removed — a Swarm directive `docker compose` ignores,
  which would in any case conflict with the fixed `8000:8000` / `3000:3000`
  host bindings.
- The `frontend` healthcheck probed `http://localhost:3000`, which resolves to
  IPv6 `::1` inside the container while the Node server binds IPv4 `0.0.0.0`.
  It reported "connection refused" and the container sat **unhealthy for five
  weeks** while serving correctly. Now `127.0.0.1`.
- The prod frontend image dated from 2026-07-04, older than the source.
  Rebuilt; the container is healthy and serving the current build.

---

## 2. How this was verified

`nginx.prod.conf` was run against the live backend in a throwaway container on
alternate loopback ports (18080/18443), with the real static and media volumes
mounted. Each route was compared with the production site:

| Path | Prod config | Live site |
|---|---|---|
| `/` | 200 | 200 |
| `/login` | 200 | 200 |
| `/admin/` | 302 | 302 |
| `/static/admin/css/base.css` | 200 | 200 |
| `/chatgpt-analytics/` | 200 | 200 |
| `/health` | 200 | 200 |
| `/health/` | 200 | 200 |
| `/api/token/` | 405 | 405 |
| `/chat/<deployment>/` (both live bots) | 200 | 200 |
| `/api/workflow-deploy/<project>/` | 405 | 405 |

`/media/<a real document>.pdf` returned 200 and 883,577 bytes of
`application/pdf`. `/health/` returned `{"status": "ok"}` rather than nginx's
canned `healthy`, confirming the shadowing is gone. The live stack was not
modified during any of this.

The production **image** was separately built from `Dockerfile.prod` and run
against the real database with its startup command overridden, so the check
could not migrate or mutate anything: Gunicorn booted, the literal HEALTHCHECK
command exited 0, `/health/ready/` returned `{"status": "ready", "database":
"ok"}`, and `DEBUG` was `False`.

---

## 3. Still true in production mode

Not defects introduced by the switch, but worth knowing.

- **`/media/` is public.** Any uploaded document is retrievable by anyone who
  knows its URL — no authentication. This is the behaviour today in development
  too (Django serves it when `DEBUG` is true), so the switch preserves it
  rather than introducing it. If documents are confidential, this needs an
  authenticated download view; it is not a small change.
- **The prod overlay has never run under sustained load.** Treat the first
  switchover as a real deployment.
- Two mounts still target `/etc/nginx/ssl` in the base file (B19); the later
  one wins.

---

## 4. Switchover checklist

```bash
# 1. Back up first - reset/rollback is not free.
docker exec ai_catalogue_postgres pg_dump -U "$DB_USER" "$DB_NAME" > pre-prod.sql

# 2. Confirm the images are current.
docker compose build backend frontend

# 3. Start with the prod overlay (excludes docker-compose.override.yml).
./scripts/production.sh          # or: -f docker-compose.yml -f docker-compose.prod.yml

# 4. Verify before announcing.
curl -sf http://localhost:8000/health/
curl -sf http://localhost:8000/health/ready/
curl -s -o /dev/null -w '%{http_code}\n' https://aicc.uksouth.cloudapp.azure.com/
curl -s -o /dev/null -w '%{http_code}\n' https://aicc.uksouth.cloudapp.azure.com/chatgpt-analytics/
curl -s -o /dev/null -w '%{http_code}\n' https://aicc.uksouth.cloudapp.azure.com/static/admin/css/base.css
```

**Rollback** is `docker compose up -d` with no `-f` flags: the override file is
auto-merged again and the development stack returns. Nothing in these changes
is one-way, and no volume is renamed or deleted.

`ALLOWED_HOSTS` must keep `localhost` — the healthcheck and `production.sh`
probe `http://localhost:8000/health/` through Django's normal request path.
