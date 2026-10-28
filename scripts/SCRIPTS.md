# Scripts Guide

Every shell script in this directory, verified against its source. Deeper context lives in [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

> **All of these except `production.sh` run the development stack.** `docker-compose.override.yml` sits in the repository root, so Docker Compose auto-merges it into any bare `docker compose …` command — which means `runserver`, `DEBUG=True`, and the Vite dev server. Only `production.sh` passes `-f docker-compose.yml -f docker-compose.prod.yml` to exclude it.

---

## Which script to use?

| Scenario | Script | Downtime |
|---|---|---|
| First run after cloning | `start-dev.sh` then `bootstrap.sh` | ~8–12 min |
| Code changes (`.py`, `.svelte`, `.ts`) | `quick-deploy.sh` | ~2 s |
| New migrations | `quick-deploy.sh` | ~2 s |
| nginx config changes | `quick-deploy.sh` | 0 s |
| New pip/npm packages | `rebuild-deploy.sh` | ~10 s |
| Dockerfile changes | `rebuild-deploy.sh` | ~10 s |
| Restart everything, no rebuild | `restart-dev.sh` | ~1–2 min |
| Fresh install / infra changes | `start-dev.sh` | ~8–12 min |
| Production mode | `production.sh` | see the warning below |
| First-time TLS certificate | `setup-ssl.sh` | interactive |
| Renew TLS certificate | `renew-ssl.sh` | 0 s |
| Stale Vite chunk 404s | `fix-frontend-cache.sh` | frontend only |
| Reclaim disk (keeps data) | `docker-cleanup.sh` | full stop |
| Destroy everything | `reset.sh` | irreversible |

---

## First run

### `start-dev.sh` — full build and ordered startup

Validates that `.env` exists and that `MILVUS_ROOT_*` and `MINIO_ROOT_*` are non-empty, creates `./volumes` and `./logs`, prunes builders older than 24 h, builds with `--no-cache`, then starts eight groups in dependency order with health gates:

```
postgres → etcd + minio → milvus (240 s gate) → redis → backend → frontend-dev → nginx → pgadmin + attu
```

### `bootstrap.sh` — make a fresh clone usable

```bash
./scripts/bootstrap.sh
```

Idempotent, and safe to re-run. Four steps that `docker compose up` does not cover:

1. Rotates the Milvus root password away from the vendor default `Milvus` and writes the new value into `.env`.
2. Seeds the `LLMProvider` rows that the llm_eval comparison feature reads — no migration creates them.
3. Creates an administrator with a generated password, shown once and never stored.
4. Restores the dashboard icons.

It prints no secret other than that admin password. Run it after `start-dev.sh`, with the backend already up.

### `install-git-hooks.sh` — one-time, per clone

Installs the repository's pre-commit hook, which blocks commits containing API keys, Fernet keys, private keys or `.env` files. Git does not version `.git/hooks`, so **every clone must run this once**.

---

## Day-to-day

### `quick-deploy.sh` — the common case

```bash
./scripts/quick-deploy.sh
```

`git pull` → `migrate` → `collectstatic` → `touch /app/core/settings.py` to trigger Django's autoreload → `nginx -s reload`. Every exec step is `|| true`, so a partial failure will not abort it.

This works only because the backend runs `runserver` against a bind mount. It is a development deploy path, not a production one.

### `rebuild-deploy.sh` — dependency or image changes

```bash
./scripts/rebuild-deploy.sh
```

`git pull` → builds `backend` and `frontend-dev` while the old containers keep serving → migrates → force-recreates both → polls `http://localhost:8000/admin/` for up to 180 s → reloads nginx. Databases stay up throughout.

### `restart-dev.sh` — bounce the stack

Same ordered, health-gated startup as `start-dev.sh` but with no build and no image pull. This is the everyday "turn it off and on again".

---

## Production and TLS

### `production.sh`

The only script that runs the production overlay:

```bash
./scripts/production.sh [--ssl] [--rebuild]
```

Sets `ENVIRONMENT=production`, `DEBUG=False`, and starts with `-f docker-compose.yml -f docker-compose.prod.yml`.

> **The two defects that blocked this path were fixed on 2026-10-28** (B1 and B2 in [`../docs/KNOWN_ISSUES.md`](../docs/KNOWN_ISSUES.md)): Gunicorn now runs `core.wsgi:application`, and `/health/` is a registered route. The overlay has not yet been exercised under sustained production load, so treat the first run as a deployment, not a formality — and note `deploy.replicas: 2` remains a no-op under `docker compose`.

### `setup-ssl.sh`

Interactive first-time Let's Encrypt issuance. Writes `DOMAIN_NAME` and `SSL_EMAIL` into `.env`, checks DNS against your public IP, brings up nginx with the SSL overlay, runs `certbot certonly --webroot` (dry run first), then **rewrites `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` and `ALLOWED_HOSTS` in `.env`** and restarts.

### `renew-ssl.sh`

Renews using the **host's** certbot, not the container, against the project-local
store in `certbot/certs` — and hardcodes `/home/alokkrsahu/ai_catalogue`. The
per-certificate hooks in `certbot/certs/renewal/<domain>.conf` do the work:
`pre_hook` stops nginx to free port 80 for the standalone challenge,
`renew_hook` copies the new certificate into `nginx/ssl/`, and `post_hook`
starts nginx again. Renewal therefore causes a short outage — but only when a
certificate is actually due; certbot skips the hooks entirely otherwise, so
running the script when nothing is due does not touch nginx.

It takes a non-blocking `flock`, so an overrunning renewal can never have two
certbots competing for port 80, and reports the certificate's real expiry plus
what nginx is actually serving, so a `renew_hook` that failed to copy is
visible rather than silent.

> **This must remain the only renewal mechanism.** certbot's own
> `snap.certbot.renew.timer` renews the *system* store (`/etc/letsencrypt`),
> which nothing here serves, has no hook to install a new certificate, and uses
> the standalone authenticator without stopping nginx — so it fails on "port 80
> in use" on every run. Verify with
> `systemctl is-enabled snap.certbot.renew.timer` (expected: `disabled`).

This script is installed in the crontab and runs unattended; do not rename or
move it without updating cron.

---

## Maintenance

| Script | What it does |
|---|---|
| `docker-cleanup.sh` | Interactive. Stops the stack, removes exited containers, dangling images, unused networks and build cache. **Never touches volumes.** Its claim that `./volumes/*` holds preserved data is wrong — those directories are mounted by nothing. |
| `stop.sh` | Stops frontend, nginx, backend, milvus, postgres, etcd, minio, pgadmin. **Leaves `redis`, `attu` and `frontend-dev` running** — use `docker compose stop` if you want everything down. |
| `fix-frontend-cache.sh` | Deletes the Vite caches to clear stale-chunk 404s. |
| `reset.sh` | **Destructive.** `docker compose down -v`, deletes `./volumes` and `./logs`, wipes frontend and backend caches. Destroys the database, every uploaded document, and all vector data. There is no backup tooling in this repository — take a dump first (see DEPLOYMENT.md §9). |

---

## Also in this directory

Not deployment-related: `analyze_experiment_logs.py`, `extract_experiment_metrics.py`, `check_evaluation_times.py` — research utilities that bootstrap Django and query the evaluation models.

`git-hooks/pre-commit` is the hook source installed by `install-git-hooks.sh`.

## Running management commands

There are no shell scripts under `backend/`. Use the container directly:

```bash
docker compose exec backend python manage.py <command>
```
