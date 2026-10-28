# Deployment & Operations

**Audience:** engineers and operators running IntelliDoc.
**Scope:** Docker Compose stack, configuration, nginx, scripts, ports, persistence.

Everything below was verified against the compose files, Dockerfiles, nginx configs and scripts in this repository. Where the repository contains something that does not work, this document says so rather than describing the intent.

---

## 1. Compose file layout — read this first

| File | Role |
|---|---|
| `docker-compose.yml` | Base stack, **10 services** |
| `docker-compose.override.yml` | Development overlay — **auto-loaded by every bare `docker compose` command** |
| `docker-compose.prod.yml` | Production overlay (see the warning in §7) |
| `docker-compose.ssl.yml` | Certbot / Let's Encrypt overlay |

> **The single most important operational fact:** because `docker-compose.override.yml` sits in the repository root, Docker Compose merges it automatically. Any plain `docker compose …` command therefore runs the **development** configuration — Django `runserver`, `DEBUG=True`, the Vite dev server, and `nginx.dev.conf`. The only way to exclude it is to name files explicitly:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
> ```
>
> `scripts/production.sh` is the only script that does this.

### Services in the base stack

| Service | Image | Container |
|---|---|---|
| `postgres` | `postgres:15-alpine` | `ai_catalogue_postgres` |
| `redis` | `redis:7-alpine` | `ai_catalogue_redis` |
| `etcd` | `quay.io/coreos/etcd:v3.5.12` | `ai_catalogue_etcd` |
| `minio` | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | `ai_catalogue_minio` |
| `milvus` | `milvusdb/milvus:v2.6.0` | `ai_catalogue_milvus` |
| `backend` | build `./backend` | `ai_catalogue_backend` |
| `frontend` | build `./frontend` | `ai_catalogue_frontend` |
| `nginx` | `nginx:alpine` | `ai_catalogue_nginx` |
| `pgadmin` | `dpage/pgadmin4:latest` | `ai_catalogue_pgadmin` |
| `attu` | `zilliz/attu:latest` | `ai_catalogue_attu` |

The dev overlay adds an eleventh service, `frontend-dev` (`ai_catalogue_frontend_dev`), and replaces `frontend` in practice — when the overlay is active, `frontend` is not started.

Network: `ai_catalogue_network`, bridge driver, subnet `172.20.0.0/16` (Docker names it `ai_catalogue_ai_catalogue_network`).

---

## 2. Ports

Only **nginx (80/443)** is published on all interfaces. Every other port binds `127.0.0.1`, so it is reachable from the host itself but not from the network.

| Host port | Service | Notes |
|---|---|---|
| **80** | nginx | Primary entry point |
| **443** | nginx | TLS |
| 8000 | backend | Django direct, including `/admin/` — loopback only |
| 5173 | frontend-dev | Vite dev server + HMR — loopback only; reach the app via nginx |
| 3000 | frontend | Production Node server — loopback only |
| 5432 | postgres | Loopback only |
| 6379 | redis | Loopback only, and `requirepass` is enforced (`REDIS_PASSWORD`). |
| 19530 | milvus | gRPC; authentication enabled. Loopback only. |
| 9091 | milvus | HTTP — `/healthz`, `/webui/`. Loopback only. |
| 8080 | pgadmin | Container port 80. Loopback only. |
| 3001 | attu | Milvus web UI. Loopback only. |
| — | etcd, minio | Not published; container network only |

To reach a loopback-bound service from your workstation, use an SSH tunnel rather than republishing the port, e.g. `ssh -L 8080:127.0.0.1:8080 <host>` for pgAdmin.

**Still worth doing:** nothing outstanding on port exposure. Review this table whenever a service is added.

---

## 3. Configuration

Copy `.env.example` to `.env` and fill in the values marked REQUIRED. Variables without a compose default will stop the stack from starting if unset, which is deliberate — it is safer than falling back to a value published in this repository.

### Genuinely required

| Variable | Why |
|---|---|
| `PROJECT_API_KEY_ENCRYPTION_KEY` | No default. Fernet key encrypting per-project API keys at rest. |
| `MILVUS_ROOT_USER`, `MILVUS_ROOT_PASSWORD` | No default; `start-dev.sh` and `production.sh` abort if empty. |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | Same startup guards — see the note below. |
| `DJANGO_SECRET_KEY` | No compose default. Also enforced in settings: with `DEBUG=False`, a missing or public dev key raises `ImproperlyConfigured` at startup. |
| `API_KEY_ENCRYPTION_KEY` | No default. Encrypts `llm_eval.APIKeyConfig`. A literal key was previously hardcoded in `docker-compose.yml` and is public in git history — it has been rotated and must not be reused. |
| `DB_PASSWORD` | No default. The previous default (`ai_catalogue_password`) is published in this repository. |
| `REDIS_PASSWORD` | No default. Redis runs with `requirepass`; the cache holds fetched web content and rate-limit counters, so write access can poison LLM inputs. |
| At least one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` | Otherwise no LLM node can execute. |

Generate the keys:

```bash
# Django secret key
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Fernet key for PROJECT_API_KEY_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **On rotating either encryption key, read §11 first.** `PROJECT_API_KEY_ENCRYPTION_KEY` cannot be changed without re-encrypting stored data.

> **MinIO is required but unused.** The startup scripts refuse to run without `MINIO_ROOT_*`, yet Milvus is configured with `MINIO_ADDRESS: ""` and `COMMON_STORAGETYPE: local`, so it stores segments on its own volume and never contacts MinIO. Set the variables to satisfy the guard; expect the `minio` service to sit idle.

### Other variable groups

- **Database** — `DB_NAME` (`ai_catalogue_db`), `DB_USER` (`ai_catalogue_user`), `DB_PASSWORD`, `DB_PORT` (`5432`), `DB_AUTH_METHOD` (`md5`). `DB_HOST` is forced to `postgres` for the backend container. Tuning: `DB_CONN_MAX_AGE` (`300`, `600` in prod), `DB_CONNECT_TIMEOUT` (`60`), `DB_SSL_MODE` (`prefer`).
- **Vector store** — `MILVUS_HOST`/`MILVUS_PORT` (forced to `milvus`/`19530` for the backend).
- **Redis** — `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB` (`0`), `REDIS_PASSWORD` (**required**). Used as the Django cache backend and the web-search cache. The credential is carried in the cache `LOCATION` URL (URL-quoted), so the raw redis-py client used for pattern deletes inherits it automatically — there is only one place to configure it.
- **Django** — `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `ENVIRONMENT`, `JWT_ACCESS_TOKEN_LIFETIME` (60 min), `JWT_REFRESH_TOKEN_LIFETIME` (1440 min).
- **LLM** — `OPENAI_API_KEY` / `OPENAI_MODEL` (`gpt-3.5-turbo`), `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` / `GEMINI_MODEL` (`gemini-1.5-flash`), and `AICC_CHATBOT_OPENAI_API_KEY` — a **separate** key used only by the public chatbot so that public traffic never consumes the platform key.
- **Frontend** — `BACKEND_URL` (server-side SvelteKit, `http://backend:8000`), `VITE_BACKEND_URL` and `VITE_API_BASE_URL` (browser-facing; point these at your public origin).
- **Nginx / SSL** — `NGINX_HTTP_PORT`, `NGINX_HTTPS_PORT`, `DOMAIN_NAME`, `SSL_EMAIL`.
- **Admin UIs** — `PGADMIN_EMAIL`, `PGADMIN_PASSWORD`, `PGADMIN_PORT`, `ATTU_PORT`, `ATTU_HOST_URL`.

**Declared but read by nothing:** `DEVELOPMENT_MODE`, `FRONTEND_DEV_PORT`, `BACKEND_DEV_PORT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, and the whole Kubernetes/Azure block (`K8S_*`, `AZURE_*`, `AKS_*`, `ACR_*`, `HPA_*`, `PROMETHEUS_*`, `GRAFANA_*`) which only feeds the abandoned `k8s/` scripts (§8).

---

## 4. Images

| Dockerfile | Used by | Base | Runs |
|---|---|---|---|
| `backend/Dockerfile` | base `backend` | `python:3.13-slim-bookworm` | collectstatic → migrate → `setup_container_data` → **`runserver`** |
| `backend/Dockerfile.dev` | dev overlay | `python:3.13-slim` | `runserver`; no `COPY` — relies on the `./backend:/app` bind mount |
| `backend/Dockerfile.prod` | prod overlay | `python:3.13-slim` | collectstatic → migrate → **gunicorn (broken, §7)** |
| `frontend/Dockerfile` | base `frontend` | `node:20-alpine`, 2-stage | `node build/index.js` (`adapter-node`) |
| `frontend/Dockerfile.dev` | dev overlay | `node:20-alpine` | `npm run dev -- --host 0.0.0.0 --port 5173` |

Note that even the *base* (non-dev) backend image runs Django's development server. There is no Gunicorn path that currently works.

The backend image installs OCR/document tooling (poppler-utils, tesseract-ocr, antiword, unrtf) and audio tooling (ffmpeg, sox, flac) in best-effort layers that swallow failures — a build can succeed with those tools missing, which silently disables the corresponding extraction paths.

---

## 5. Nginx

There are four configs; three are ever mounted. **`nginx/nginx.conf` is dead** — the base compose `nginx` service mounts no configuration at all, so a base-only start serves nginx's stock page.

| Config | Mounted by | Notes |
|---|---|---|
| `nginx.dev.conf` | `docker-compose.override.yml` | The config in normal use |
| `nginx.prod.conf` | `docker-compose.prod.yml` | Rate limiting, gzip, asset caching, HSTS |
| `nginx.ssl.conf` | `docker-compose.ssl.yml` | Certbot webroot + real HTTP→HTTPS redirect |
| `nginx.conf` | *nothing* | Dead |

Routing (consistent across dev and prod):

| Path | Upstream | Timeout |
|---|---|---|
| `/` | frontend (`frontend-dev:5173` in dev) | 86400s on HTTP for HMR |
| `/api/workflow-deploy*` | backend (CORS handled by Django middleware) | 300s |
| all other `/api/*` | backend | **1500s (25 min)** for long document processing |
| `/admin/`, `/static/`, `/media/` | backend | default |
| `/health` | answered by nginx itself | — |

Body size limits: 500 MB in dev, 100 MB in prod.

Known nginx issues:

- **CORS wildcard fallback.** Both `nginx.dev.conf` and `nginx.prod.conf` map a `null` or empty `Origin` to `*`.
- **Neither the dev nor prod config redirects HTTP to HTTPS** (the prod file has a comment claiming it does). Only `nginx.ssl.conf` does.
- **`nginx.dev.conf` couples this repo to a different project** — it declares `chatgpt_analytics_frontend` / `chatgpt_analytics_backend` upstreams and nginx will not start if those containers are absent from the shared network.
- Two cert layouts coexist: flat `nginx/ssl/{fullchain,privkey}.pem` (dev/prod) versus the Let's Encrypt `live/<domain>/` tree (`nginx.ssl.conf`).

---

## 6. Scripts

`scripts/SCRIPTS.md` holds the short decision table. Day-to-day:

| Task | Script | Effect |
|---|---|---|
| Code-only change (`.py`, `.svelte`, `.ts`) | `./scripts/quick-deploy.sh` | `git pull`, migrate, collectstatic, touch `core/settings.py` to trigger autoreload, reload nginx. ~2s. **Depends on `runserver` + bind mount, so it is a dev-mode path.** |
| New dependency or Dockerfile change | `./scripts/rebuild-deploy.sh` | Rebuilds `backend` + `frontend-dev`, recreates them, waits for health, reloads nginx. ~10s downtime. |
| First run / full rebuild | `./scripts/start-dev.sh` | Validates `.env`, prunes builders, `build --no-cache`, then nine health-gated startup steps. 8–12 min. |
| Restart without rebuilding | `./scripts/restart-dev.sh` | Same ordering, no build or pull. |
| Production mode | `./scripts/production.sh` | The only script that excludes the dev overlay. See §7. |
| First-time TLS issuance | `./scripts/setup-ssl.sh` | Interactive certbot webroot flow; rewrites CORS/CSRF/ALLOWED_HOSTS in `.env`. |
| Renew TLS | `./scripts/renew-ssl.sh` | Uses the **host** certbot, not the container. Hardcodes `/home/alokkrsahu/ai_catalogue`. |
| Reclaim disk | `./scripts/docker-cleanup.sh` | Prunes containers/images/networks/builders. Never touches volumes. |
| Destroy everything | `./scripts/reset.sh` | `down -v`, deletes `./volumes`, `./logs`, node_modules, caches. **Irreversible.** |

Caveats worth knowing:

- `scripts/stop.sh` is incomplete — it never stops `redis`, `attu` or `frontend-dev`.
- **There are no shell scripts under `backend/`.** Run management commands through the container: `docker compose exec backend python manage.py <command>`.
- **There is no backup script anywhere in the repository** — no `pg_dump`, no volume export. See §9.

---

## 7. Production mode — current state

`docker-compose.prod.yml` and `Dockerfile.prod` describe a real production posture: Gunicorn with 4 workers, `DEBUG=False`, `SECURE_SSL_REDIRECT`, HSTS, tuned Postgres, resource limits. **The two defects that prevented it from starting have been fixed** (2026-10-28):

1. **WSGI module path — fixed.** `Dockerfile.prod` ran `gunicorn ai_catalogue.wsgi:application`, but the Django project package is `core` (`backend/core/wsgi.py`; `manage.py` sets `DJANGO_SETTINGS_MODULE=core.settings`). There is no `ai_catalogue` package, so Gunicorn exited with `ModuleNotFoundError`. It now runs `core.wsgi:application`.
2. **`/health/` route — added.** `Dockerfile.prod`'s healthcheck, `docker-compose.prod.yml` and `production.sh` all poll `http://localhost:8000/health/`, which was not registered in `core/urls.py` and returned 404. `backend/core/health.py` now provides two probes, wired at the top of `core/urls.py`:

   | Route | Purpose | Checks | Fails with |
   |---|---|---|---|
   | `/health/` | Liveness — is the process serving? | nothing external | never (200 while the worker is alive) |
   | `/health/ready/` | Readiness — can it serve useful traffic? | PostgreSQL `SELECT 1` | 503 |

   Milvus and Redis are deliberately **not** checked: document search degrades without them but the rest of the site works, so they must not be able to take the backend out of rotation. The existing `/api/vector-search/health/` is unsuitable as a container probe — it requires authentication (a probe gets 401) and reports on Milvus.

   These resolve through Django's normal request path, so the probe's `Host` header must be in `ALLOWED_HOSTS`. The probes use `localhost`, which is in both the `settings.py` default and `.env.example` — **do not remove it**.

   > **Through nginx these routes are shadowed.** Every nginx config carries a prefix `location /health` that returns a hardcoded `200 healthy`. Because it is a prefix match, not `location = /health`, it also swallows `/health/` and `/health/ready/`. Verified against the live site: `/health`, `/health/` and `/health/ready/` all return the literal string `healthy`, while the same paths straight to the backend return `{"status": "ok"}` and `{"status": "ready", "database": "ok"}`.
   >
   > This does not affect the container healthcheck or `production.sh`, which poll `http://localhost:8000/health/` and bypass nginx entirely. It does mean **an external caller cannot use these URLs to learn anything about backend readiness** — nginx answers `healthy` even if Django is down. Treat the public `/health` as an nginx liveness signal only. Making the Django probes externally meaningful would require changing the nginx blocks to `location = /health` and adding a proxied location for `/health/`; that has not been done, because the public site has no need to expose readiness.

Additionally, `deploy.replicas: 2` in the prod overlay is ignored by `docker compose` (it is a Swarm directive) and would in any case conflict with the fixed `8000:8000` and `3000:3000` host port bindings.

**Consequence:** the running deployment is the **development** stack — `Dockerfile.dev`, Django `runserver`, `DEBUG=True`, `DJANGO_DEBUG_TOOLBAR=True`, the Vite dev server, and `nginx.dev.conf`. Setting `DEBUG=False` in `.env` has no effect, because `docker-compose.override.yml` sets `DEBUG: "True"` in the service `environment` block, which takes precedence over `env_file`.

To actually run in production mode, start with `-f docker-compose.yml -f docker-compose.prod.yml` (or `./scripts/production.sh`), which excludes the override file. A further seven dev/prod parity defects were found and fixed on 2026-10-28 — missing routes, an upload cap, the wrong media volume, unserved `/media/` and `/static/`, inert security settings and shadowed health probes. **Read [`PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md) before switching**; it carries the verification evidence and the switchover checklist. The prod overlay has not been exercised under sustained load, so treat the first run as a real deployment.

---

## 8. Kubernetes

`k8s/` contains a complete-looking manifest set (namespaces, Deployments, Services, PVCs, HPAs, Ingresses) plus `k8s/scripts/`. **It is abandoned and should not be used as-is:**

- `k8s/` is listed in `.gitignore`, so it is not tracked. Last touched 2025-09-03, while compose files have changed as recently as 2026-02-05.
- Image drift: Milvus `v2.5.15` vs `v2.6.0` in compose; etcd `v3.5.5` vs `v3.5.12`; MinIO an older release.
- **No Redis anywhere in the manifests**, though the backend depends on it for health.
- `k8s/base/configmap.yaml` carries a Milvus 2.4-era config with `mq.type: rocksmq`, a duplicated `common:` key, and a **plaintext MinIO password**.

The `K8S_*` / `AZURE_*` variables in `.env` exist only for these scripts.

---

## 9. Persistence and backup

Eleven named volumes (all `driver: local`):

| Volume | Holds |
|---|---|
| `postgres_data` | **All relational data** — users, projects, workflows, deployments, evaluations, encrypted API keys |
| `backend_media` / `backend_media_dev` | **Uploaded documents** (the dev volume is the one in use under the default overlay) |
| `milvus_data` | Project vector segments and indexes |
| `etcd_data` | Milvus metadata — losing this orphans every collection |
| `redis_data` | AOF-persisted cache (web-search cache, rate limits) |
| `milvus_volumes` | Backend-side Milvus working files |
| `backend_logs` / `backend_logs_dev` | Application and error logs |
| `minio_data` | MinIO object store — effectively unused |
| `pgadmin_data` | pgAdmin server list and preferences |

`docker compose down -v` destroys all of them: the database, every uploaded document, and all vectors with their metadata.

> **There is no backup tooling in this repository.** Before any destructive operation, take your own dump, e.g.:
>
> ```bash
> docker exec ai_catalogue_postgres pg_dump -U "$DB_USER" "$DB_NAME" > backup.sql
> docker run --rm -v ai_catalogue_backend_media_dev:/data -v "$PWD":/out alpine \
>   tar czf /out/media-backup.tar.gz -C /data .
> ```

The directories `./volumes/{postgres,milvus,etcd,minio}` are created by the startup scripts but **mounted by no compose file** — they are leftovers from an earlier bind-mount design and contain nothing. `docker-cleanup.sh` still describes them as preserved data, which is misleading.

---

## 10. Known infrastructure defects

Collected for triage; each is verifiable in the file cited.

| # | Issue | Location |
|---|---|---|
| 1 | Live deployment runs the dev stack with `DEBUG=True` and `runserver` | `docker-compose.override.yml` |
| ~~2~~ | ~~`gunicorn ai_catalogue.wsgi` — module does not exist~~ **Fixed 2026-10-28** — now `core.wsgi:application` | `backend/Dockerfile.prod` |
| ~~3~~ | ~~`/health/` healthcheck target is not a registered route~~ **Fixed 2026-10-28** — `/health/` and `/health/ready/` added | `backend/core/health.py`, `core/urls.py` |
| 4 | Literal Fernet key committed as `API_KEY_ENCRYPTION_KEY` default | `docker-compose.yml` |
| 7 | `nginx/nginx.conf` mounted by nothing; base nginx has no config | `docker-compose.yml` |
| 8 | Duplicate mount target `/etc/nginx/ssl` from two sources | `docker-compose.yml` |
| 9 | CORS maps `null`/empty Origin to `*` | `nginx.dev.conf`, `nginx.prod.conf` |
| 10 | `nginx.ssl.conf` lacks the long AI timeouts | `nginx.ssl.conf` |
| 12 | MinIO credentials gate startup but MinIO is unused | `start-dev.sh`, `production.sh` |
| 13 | `stop.sh` leaves redis, attu, frontend-dev running | `scripts/stop.sh` |
| 14 | `backend/*.sh` hardcode a macOS path; two are echo-only | `backend/*.sh` |
| 15 | `deploy.replicas` is a no-op and conflicts with fixed host ports | `docker-compose.prod.yml` |
| 16 | `.env.example` missing ~14 variables the stack reads | `.env.example` |
| 17 | No backup tooling for any volume | repository-wide |
| 18 | `k8s/` drifted and untracked | `k8s/` |
| 20 | `nginx.dev.conf` requires another project's containers to be present | `nginx.dev.conf` |

---

## 11. Rotating encryption keys

Two keys encrypt data at rest. They behave very differently, so read this before touching either.

| Key | Encrypts | Rotating it needs re-encryption? |
|---|---|---|
| `PROJECT_API_KEY_ENCRYPTION_KEY` | `ProjectAPIKey.encrypted_api_key`, `MCPServerCredential.encrypted_credentials` — via PBKDF2 with the project id as salt | **Yes.** Changing it without re-encrypting makes every stored provider key unreadable, and no workflow can call an LLM. |
| `API_KEY_ENCRYPTION_KEY` | `llm_eval.APIKeyConfig.api_key` only | Only if that table has rows. |

`ProjectAPIKeyEncryption` reads `PROJECT_API_KEY_ENCRYPTION_KEY` **directly from the process environment**, not from Django settings, so the `PROJECT_API_KEY_SETTINGS['ENCRYPTION_KEY']` fallback in `settings.py` does not apply to it.

### Procedure

```bash
# 0. Back up first. Non-negotiable — there is no other copy.
docker exec ai_catalogue_postgres pg_dump -U "$DB_USER" "$DB_NAME" > db-backup.sql

# 1. Generate the new key
NEWKEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 2. Dry run — proves every row decrypts with the current key and that
#    re-encryption round-trips. Writes nothing.
docker compose exec backend python manage.py rotate_encryption_key --new-key "$NEWKEY" --dry-run

# 3. Apply. The database now holds ciphertext for the NEW key, while the running
#    container still has the OLD key in its environment — do step 4 immediately.
docker compose exec backend python manage.py rotate_encryption_key --new-key "$NEWKEY" --apply

# 4. Update .env, then restart so the process picks up the new key
#    PROJECT_API_KEY_ENCRYPTION_KEY=<new key>
docker compose up -d --no-deps backend

# 5. Verify every key decrypts under the new environment
docker compose exec backend python manage.py shell -c "
from project_api_keys.services import ProjectAPIKeyService
from users.models import ProjectAPIKey
svc = ProjectAPIKeyService()
bad = [k.pk for k in ProjectAPIKey.objects.select_related('project')
       if not svc.get_project_api_key(k.project, k.provider_type)]
print('failed:', bad or 'none')"
```

The command refuses to write anything unless **every** row decrypts with the old key, verifies each new ciphertext round-trips before committing, and runs inside a single transaction — so a partially-rotated table is not reachable. It never logs plaintext; rows are identified by a short SHA-256 fingerprint.

Between steps 3 and 4 the running container cannot decrypt these rows, so LLM calls fail. Keep that window to seconds.

### Rotating the database password

```bash
NEWPW=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
docker exec -e PGPASSWORD="$OLD" ai_catalogue_postgres \
  psql -U "$DB_USER" -d "$DB_NAME" -c "ALTER USER $DB_USER WITH PASSWORD '$NEWPW';"
# update DB_PASSWORD in .env, then:
docker compose up -d --no-deps backend
```

`POSTGRES_PASSWORD` in compose only applies when the data volume is first initialised, so it does not change an existing cluster — `ALTER USER` is what actually rotates it.

### If a key reaches version control

Treat it as public permanently; rewriting history does not help, because clones and forks retain it. Rotate, remove the value from the working tree, and confirm nothing re-introduces it:

```bash
git grep -nIE "[A-Za-z0-9_-]{43}=" -- .     # Fernet-shaped literals in tracked files
```

---

## 12. First run on a fresh clone

`docker compose up` gives you running containers, not a working system. Three things
live only in the datastores and must be established once — see
[`REPRODUCIBILITY_ANALYSIS.md`](REPRODUCIBILITY_ANALYSIS.md) for why each is not
already covered.

```bash
cp .env.example .env          # fill in every value marked REQUIRED
./scripts/start-dev.sh        # first run: 8-12 min
./scripts/bootstrap.sh        # rotates the Milvus password, seeds providers, creates an admin
```

`bootstrap.sh` is idempotent — safe to re-run. It:

1. **Rotates the Milvus root password.** Milvus ships with the public default
   `Milvus` and creates no other user; nothing else in the codebase changes it. The
   new value is written to `.env`, so restart the backend afterwards
   (`docker compose up -d --no-deps backend`).
2. **Seeds the `LLMProvider` registry.** The `llm_eval` comparison feature reads rows
   that no migration creates, so without this the feature is inert.
3. **Creates an administrator** with a generated password, printed once. A superuser is
   no longer created implicitly — the previous fallback used a published password.
4. **Restores dashboard icons** via the existing management command.

Then, in the app: create a project from a template, upload documents, run **Start
Processing**, and add a provider API key under the project's API-key settings. LLM
nodes read **per-project** keys; there is no environment-variable fallback, so a
project without a key cannot execute any LLM node.

### Try a workflow without building one

`backend/schemas/example_workflow.json` is a valid import bundle — Start → Splitter →
two specialists → synthesiser → End, with placeholder prompts. Import it from the
workflow designer, or:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @backend/schemas/example_workflow.json \
  http://localhost/api/projects/<project-uuid>/workflows/import/
```

### Recommended agent defaults

These live in each node's `graph_json` and have no documented default, so new agents
are easy to configure inconsistently. Sensible starting points:

| Setting | Suggested | Notes |
|---|---|---|
| `llm_model` (content agents) | `gpt-5.5` | Verify the model is current before relying on it — a deprecated model fails at call time even while it still appears in `/v1/models`. |
| `llm_model` (routers) | `gpt-5.4-mini` | Routing is a small, frequent call; a mini model is both cheaper and faster. |
| `search_method` | `hybrid_search` | The code default. Note `index_type` and `metric_type` have no runtime effect (KNOWN_ISSUES M1/M2). |
| `web_search_cache_ttl` | `604800` (7 days) | Match it to how often the source site changes. Keep it consistent across projects. |
| `web_search_max_results` / `top_k` | `5` / `5` | |
| `temperature` | `0.7` | A literal `0` is silently coerced to `0.7` (KNOWN_ISSUES B11). |

A full cache refresh (clear + re-fetch + re-embed + re-index) takes roughly **8 minutes
for ~300 URLs**. Fetching is fast; embedding and the Milvus insert dominate at about
seven times the fetch time.
