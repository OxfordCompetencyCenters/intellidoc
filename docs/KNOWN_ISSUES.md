# Known Issues & Dead Code

A register of defects and vestigial code found while deriving the documentation from the source. Everything here was verified in the code; nothing is speculative. Grouped by severity so it can be triaged.

Open items are a to-do list. Items that have been fixed are moved to the **Resolved** section at the end rather than deleted, so the history of what was wrong stays visible.

---

## Security — needs attention

| # | Issue | Where |
|---|---|---|
| S8 | **CORS maps a `null` or empty `Origin` to `*`** in both the dev and prod nginx configs; `nginx.ssl.conf` sets `Access-Control-Allow-Origin: *` on the public chatbot path. | `nginx/nginx.dev.conf`, `nginx.prod.conf`, `nginx.ssl.conf` |
| S9 | The **deployment CORS middleware only blocks preflight**. A cross-origin `POST` from a disallowed origin still executes; only the response headers are withheld. Requests with **no** `Origin` header are always allowed. | `agent_orchestration/middleware/deployment_cors.py` |
| S10 | **Deployment rate limiting fails open** — any exception in the limiter allows the request. | `agent_orchestration/deployment_rate_limiter.py` |
| S11 | The **trusted-admin-origin bypass** in the public chat gate accepts a matching `Referer` as well as `Origin`, which is attacker-controllable in some contexts. A management command (`audit_public_chat_cors`) exists to detect the dangerous configuration. | `agent_orchestration/deployment_views.py` |
| S12 | `create_default_icons` **creates a superuser `admin@example.com` / `adminpassword`** if no superuser exists. | `users/management/commands/create_default_icons.py` |

---

## Broken — cannot work as written

| # | Issue | Where |
|---|---|---|
| B3 | **`NameError` in the parallel GroupChatManager path.** `_execute_nodes_in_parallel` references `execution_id`, which is neither a parameter nor assigned in that method. Any GCM dispatched in parallel fails, the error is swallowed per-node, and downstream nodes then report "Missing required inputs". | `agent_orchestration/workflow_executor.py` |
| B4 | **`DELETE` on a workflow detail route returns 405** — `destroy` is unrouted, yet the frontend calls it. Workflow deletion is broken end to end via that path; the working path is `DELETE /api/projects/{pid}/agent_workflow/?workflow_id=…`. | `core/urls.py`, `frontend/.../api.ts` |
| B5 | **`PUT` on a workflow detail route cannot succeed.** The serializer is `fields='__all__'` while `project`, `created_by` and `graph_json` are required and non-null, and `to_representation` never returns them — so no read-modify-write round trip produces a valid body. Use `PATCH`. | `agent_orchestration/serializers.py` |
| B6 | **`/api/projects/{id}/digest/` is a plain Django view returning un-rendered DRF `Response` objects**, and its `has_user_access` check runs against `AnonymousUser` because JWT is never processed. It only works when called internally. | `vector_search/consolidated_api_views.py` |
| B7 | **The Splitter's allocated subtask is silently dropped** for any target that is a plain agent with a single input: single-input agents receive `conversation_history`, which the Splitter never writes to. Subtasks only arrive via the multi-input path or a GroupChatManager. | `agent_orchestration/workflow_executor.py`, `workflow_parser.py` |
| B8 | **After a human-input pause, `SplitterAgent` and `MCPServer` nodes are silently skipped.** The resume loop has no `else` branch for them, so consumers see `[No output from …]`. | `agent_orchestration/workflow_executor.py` |
| B9 | **`clear_pending_inputs_for_execution` raises `FieldError`** — it filters on `response_submitted_at` and sets `status`, neither of which exists on `HumanInputInteraction`. The exception is swallowed. | `agent_orchestration/human_input_views.py` |
| B10 | **`AgentWorkflowViewSet.stop` is both unrouted and broken** — it queries `WorkflowExecutionMessage.objects.get(execution_id=…)`, a field that does not exist on that model. | `agent_orchestration/workflow_views.py` |
| B11 | **`temperature: 0` is silently coerced to `0.7`** by `float(config.get('temperature') or 0.7)`. | `agent_orchestration/llm_provider_manager.py` |
| B12 | **The provider aliases `'claude'` and `'gemini'` always fail.** They reach class selection but the key lookup uses the same raw string, and `ProjectAPIKey.provider_type` only permits `openai`/`google`/`anthropic`. | `agent_orchestration/llm_provider_manager.py` |
| B13 | **`get_llm_provider` is awaited incorrectly in two places** — called without `await` and without `project`, binding a truthy coroutine that passes the `if not llm_provider` guard and fails later. | `human_input_handler.py`, `conversation_orchestrator.py` |
| B14 | **`upgrade_to_unified` cannot be parsed** — it contains backslash-escaped quotes outside a string, a `SyntaxError` on every Python version. It also imports a non-existent module. | `vector_search/management/commands/upgrade_to_unified.py` |
| B15 | **`create_templates` cannot import** — it targets `templates.definitions` (the real directory is `template_definitions`) and `templates.models.ProjectTemplate`, which does not exist. | `templates/management/commands/create_templates.py` |
| B16 | **`api/views.py:process_project_documents` and `search_project_documents` would `NameError`** on `ChunkingVectorSearchManager`, which is never imported. (Both are unrouted, so unreachable.) | `api/views.py` |
| B17 | **`tasks.py` imports a package that does not exist** (`agent_orchestration.autogen.simple_executor`). Unreachable because Celery is unwired. | `agent_orchestration/tasks.py` |
| B18 | **`.odt` is advertised as supported but has no handler** — such files fall through to placeholder text. | `vector_search/enhanced_hierarchical_processor.py` |
| B19 | **`nginx/nginx.conf` is mounted by nothing**; a base-only nginx start serves the stock default page. Two volumes also target `/etc/nginx/ssl`. | `docker-compose.yml` |
| B20 | **`nginx.dev.conf` requires another project's containers** (`chatgpt_analytics_*`) to be on the network, or nginx will not start. | `nginx/nginx.dev.conf` |
| B21 | **`nginx.ssl.conf` lacks the 1500 s API timeouts** present in the dev and prod configs, so switching to the SSL overlay would break long document processing at nginx's 60 s default. | `nginx/nginx.ssl.conf` |
| B22 | **`deploy.replicas: 2` is a no-op** under `docker compose` and conflicts with the fixed `8000:8000` / `3000:3000` host bindings. | `docker-compose.prod.yml` |
| B23 | **`stop.sh` never stops** `redis`, `attu` or `frontend-dev`. | `scripts/stop.sh` |

---

## Misleading — works, but not as documented or expected

| # | Issue | Where |
|---|---|---|
| M1 | **`index_type` on DocAware searches has no runtime effect.** Only `metric_type` is forwarded to Milvus; the selection merely labels `algorithm_used`. AUTOINDEX/HNSW/IVF_FLAT are cosmetic. | `django_milvus_search/services.py` |
| M2 | **Configured `metric_type` is also ignored** — every method overrides it with the collection's actual index metric. | `agent_orchestration/docaware/service.py` |
| M3 | **`source` is `"Unknown"` for six of seven search methods and `page` is always `1`**, because the Milvus schema has neither field. Only `hybrid_search` maps `file_name`. | `agent_orchestration/docaware/service.py` |
| M4 | **`keyword_search` advertises BM25 and `requires_embedding: false`.** It does neither — it embeds the query, runs a FLAT vector search, then rescores by keyword overlap. | `agent_orchestration/docaware/search_methods.py` |
| M5 | **`hierarchical_search` levels never match.** It filters on `paragraph`/`chapter`/`sentence`/`document`, but ingestion only writes `complete_document`, `content`, `section`, `introduction` (+`_part`). Only `section` can hit. | `search_methods.py` vs `enhanced_hierarchical_processor.py` |
| M6 | **`merge_strategy: round_robin` validates but is not implemented.** | `agent_orchestration/docaware/service.py` |
| M7 | **Document chunking has zero overlap** — there is no sliding window, despite `overlap` being a common expectation. Chunk size is 35,000 *characters*. | `enhanced_hierarchical_processor.py` |
| M8 | **`GET /api/agent-orchestration/projects/<id>/deployment/` mutates state** — it `get_or_create`s a deployment row on a GET. | `agent_orchestration/deployment_views.py` |
| M9 | **`GET /api/agent-orchestration/human-input/pending/` mutates state** — every call force-completes any pause older than one hour. | `agent_orchestration/human_input_views.py` |
| M10 | **The 1000-character `user_query` cap is enforced on the non-streaming deployment endpoint but not on `/stream/`.** | `deployment_views.py` |
| M11 | **`/api/dashboard-icons/choices/` declares admin-only but is reachable by any authenticated user** — `get_permissions()` ignores per-action `permission_classes`. | `api/views.py` |
| M12 | **Project update (`PUT`/`PATCH`) has no admin gate**, while project create and delete do. | `api/universal_project_views.py` |
| M13 | **`/api/projects/health/` and `/api/vector-search/health/` require a JWT**, which is unusual for health endpoints and makes them unusable for external monitoring. | `api/universal_project_views.py`, `vector_search/api_views.py` |
| M14 | **`MinIO` credentials gate startup but MinIO is unused** — Milvus runs with `COMMON_STORAGETYPE: local` and `MINIO_ADDRESS: ""`. | `docker-compose.yml`, `scripts/*.sh` |
| M16 | **`WEBSEARCH_CONFIG['FETCH_CONCURRENCY']` is read but never defined**, so the fetcher always uses its hardcoded default of 10. | `core/settings.py`, `websearch/fetcher_service.py` |
| M17 | **`VECTOR_SEARCH_LIMIT` has no consumers**; `VECTOR_EMBEDDING_MODEL` is only used at query time (ingestion hardcodes the short model name); `GEMINI_MODEL` is not what the live extractor uses (`gemini-2.5-flash`). | `core/settings.py` |
| M18 | **`SIMPLE_JWT['BLACKLIST_AFTER_ROTATION']` is inert** — `token_blacklist` is not installed and rotation is disabled. | `core/settings.py` |
| M19 | **The Browsable API HTML renderer is active on every DRF endpoint**, because `DEFAULT_RENDERER_CLASSES` is unset. | `core/settings.py` |
| M20 | **`folder_uploaded_*` / `file_uploaded_*` ids are incompatible with the `content_filters` grammar.** They belong to the file-attachment picker; nothing validates the distinction, so using them as DocAware filters silently matches nothing. | `docaware_views.py`, `docaware/filter_expr.py` |
| M21 | **StartNode only executes if it is first in the topological order** — it is handled by a pre-loop block guarded on index 0, so a graph that sorts it later never runs it. | `workflow_executor.py` |
| M22 | **There is no keep-alive on the deployment SSE stream**; the generator does not yield on queue timeouts. | `deployment_views.py` |
| M23 | **`docker-cleanup.sh` claims `./volumes/*` are preserved data.** Those directories are mounted by no compose file and are empty leftovers. | `scripts/docker-cleanup.sh` |

---

## Vestigial — present but unreachable

| # | Item | Note |
|---|---|---|
| V1 | **Celery** — no app, no settings, no worker, no task dispatch. `SimulationRun.celery_task_id` is residue. | Remove from `requirements.txt` or wire it up |
| V2 | **Django Channels / WebSockets** — not in `INSTALLED_APPS`, no `routing.py`, plain ASGI. `consumers.py` is dead; the frontend's `workflowWebSocket.ts` cannot connect. Real-time is SSE. | |
| V3 | **`agent_orchestration/validation.py`** (386 lines) — zero references. Would also reject legal `reflection`/`delegate` back-edges, contradicting the live validator. | |
| V4 | **`agent_orchestration/simple_workflow_views.py`** (574 lines) — routed nowhere; writes `graph_json` with no validation. | |
| V5 | **`templates/urls.py` is never included**, so everything reachable only through it is dead: `EnhancedTemplateViewSet`, `templates/advanced/*`, `enhanced_duplication_views.py`, `templates/views.py:IntelliDocProjectViewSet`, and all dynamic per-template route registration. `security_views.py` has no references at all. | ~2,000 lines |
| V6 | **`parallel_executor.py`** — zero references; real parallelism lives in `_find_ready_nodes`. | |
| V7 | **`core/settings_minimal.py` / `urls_minimal.py`** — referenced by nothing and materially divergent (SQLite, hardcoded secret). Dangerous if activated. | |
| V8 | **Dead models** — `WorkflowTemplate` (no references); `SimulationRun` / `AgentMessage` (only the unwired Celery path writes them). `DashboardIcon.generate_collection_name()` references a field the model does not have. | |
| V9 | **Half of `api/views.py`** is unrouted and shadowed by `templates.views` equivalents: `IntelliDocProjectViewSet`, `ProjectTemplateViewSet`, `process_project_documents`, `get_project_vector_status`, `search_project_documents`. | |
| V10 | **`k8s/`** — gitignored, last touched 2025-09, image drift (Milvus 2.5.15 vs 2.6.0), and **no Redis** despite it now being required. ~50 `K8S_*`/`AZURE_*` variables in `.env` exist only for it. | |
| V11 | **Duplicate definitions** — `vector_search/api_views.py` defines `get_vector_status` three times and `process_unified` twice (earlier ones shadowed); `vector_search/summarization.py` is shadowed by the `summarization/` package and can never be imported. | |
| V13 | **`deployment_urls.py` builds a router that is never included** — all 12 `@action` decorators on `DeploymentViewSet` are inert metadata; the explicit paths do the routing. | |
| V14 | **Unreachable OPTIONS branches** in four public deployment views — the CORS middleware answers preflight first. | |
| V15 | **Empty template includes** — `/api/templates/legal/`, `/medical/`, `/history/` include `urlpatterns = []`. The `aicc-intellidoc` include is commented out and replaced by a hardcoded lambda labelled "EMERGENCY FIX". | |
| V16 | **Unreachable executor branches** — the `StartNode` branch inside the main loop and the `EndNode` branch are both dead because `_find_ready_nodes` skips those types. `workflow_end` messages come only from the resume loop. | |
| V17 | **No-caller functions** — `WebRAGService.ensure_indexed`, `DuckDuckGoService.search_news`, `get_rate_limit_info`, `_is_public_chat_access_allowed`, `_extract_document_fields`, and ~190 lines of legacy parsing helpers in `workflow_views.py`. | |
| V18 | **Junk in the repo** — `backend/=4.0.0` (captured pip stdout), `backend/venv/` (broken stale artifact), `conversation_orchestrator.py.backup.*` (three ~114 KB copies), `backend/documents/` (one orphaned PDF, not a package). | |
| V19 | **`api/` and `users/` have no `__init__.py`** (implicit namespace packages); `llm_eval` has no `apps.py`. | |
| V20 | **`AUTH_USER_MODEL` is declared twice** in settings; `IconClass`/`ColorTheme` embed ~115 UI enum values in the model layer. | |

---

## Consistency notes

- **Two `IsAdminUser` semantics coexist.** The project's version checks `role == 'ADMIN'`; DRF's checks `is_staff`. Different modules import different ones, so a `STAFF`-role user with `is_staff=True` passes one and fails the other. `api/views.py` imports both, the second shadowing the first.
- **The end-node resolution rule is implemented three times** — in `workflow_executor.py` (streaming eligibility), `deployment_executor.py`, and `workflow_evaluator.py`.
- **`generate_collection_name()` is duplicated** in `vector_search/database.py` and `users/models.py`, with comments in both insisting they must stay in sync.
- **`api/permissions.py` uses bare `print()`** for authorization decisions instead of logging.
- **`project_api_keys/integration_examples.py`** — an "examples" module imported by production code.

---

## Resolved — 2026-08-05 security pass

| Was | Issue | Resolution |
|---|---|---|
| S1 | Password reset returned the reset token in the response body | Token is now emailed only; the response is a fixed generic message, so the endpoint also no longer enumerates accounts. Verified: response contains only `detail`. |
| S2 | A literal Fernet key was committed as `API_KEY_ENCRYPTION_KEY`'s default | Key **rotated**; the default removed from `docker-compose.yml` (now `:?` required). The old value must never be reintroduced — treat it as public. |
| S3 | Debug workflow endpoint ignored project access | `has_user_access` check added. Verified: non-member GET/POST → 403, superuser → 200. |
| S4 | All six `/api/milvus/*` endpoints were unauthenticated | Staff-only guard on every view. Verified: all six → 401 anonymous. Nothing in the product calls them. |
| S5 | `/api/templates/refresh/` was unauthenticated and cleared the whole cache | `refresh` is staff-only; `discover` and `endpoints` require authentication. Verified: all → 401 anonymous. The frontend uses the DRF `/enhanced-project-templates/` routes, so nothing broke. |
| S7 | `DEBUG` defaulted to `True`; the insecure `SECRET_KEY` fallback was silent | `DEBUG` now defaults to `False`. The public dev `SECRET_KEY` is only permitted while `DEBUG` is on; with `DEBUG=False` a missing or dev key raises `ImproperlyConfigured` at startup. |
| — | ChromaDB had no authentication and CORS `*` | Service removed entirely (2026-08-05), along with the `public_chatbot` app, its four PostgreSQL tables and its migration records. Its ChromaDB volume was already empty, so the index had been lost before removal. All data exported to `backup/chromadb-removal-2026-08-05/` first. Resolves the old B25 (heartbeat mismatch) and V12 (conflicting legacy addon compose file) as a side effect. |
| — | Redis ran with no password or ACL | `requirepass` enabled via `REDIS_PASSWORD`; the credential travels in the Django cache `LOCATION` URL so every consumer (including the raw redis-py pattern-delete path) picks it up. Verified: anonymous `PING`, `SET` and `FLUSHALL` all refused with `NOAUTH`. |
| M15 | `.env.example` was missing ~14 variables the compose stack reads | Completed: the required ones are marked REQUIRED and an "additional variables" block documents the rest (53 variables total). |
| S13 | Hardcoded `SECRET_KEY` in `core/settings_minimal.py` | Replaced with an env lookup that raises if unset, so the inert module no longer carries a committed secret. (The module remains unused — see V7.) |
| B1 | The production image could not start: `gunicorn ai_catalogue.wsgi:application`, but the Django project package is `core` | Module path corrected to `core.wsgi:application` (2026-10-28). Verified: `core.wsgi:application` imports and yields a `WSGIHandler`, while `import ai_catalogue.wsgi` raises `ModuleNotFoundError`, confirming the old path was dead. |
| B2 | `/health/` was polled by the prod healthcheck, `docker-compose.prod.yml` and `production.sh`, but was not a registered route | Added `core/health.py` with two unauthenticated probes wired at the top of `core/urls.py` (2026-10-28): `/health/` is liveness (no dependencies, so a dependency blip cannot kill the container) and `/health/ready/` is readiness (PostgreSQL only — Milvus and Redis are excluded so degraded search cannot take the backend out of rotation). Both verified 200. Note the probes resolve through Django, so `localhost` must stay in `ALLOWED_HOSTS`. |
| B24 | Every `backend/*.sh` script hardcoded a macOS path (`/Users/alok/Documents/AICC/...`) and a `venv` that does not exist here; two of them only echoed text | All ten removed (2026-08-13). Two also referenced project UUIDs that no longer exist in the database. Management commands now run via `docker compose exec backend python manage.py <command>`. Seven redundant or unsafe `scripts/` entries were removed in the same pass — see the Resolved note below. |
| — | `scripts/` carried superseded and actively harmful scripts | Removed (2026-08-13): `start.sh` and `start-local.sh` (superseded by `production.sh` / `start-dev.sh`), `git-pull.sh` / `git-push.sh` (the latter did `git add .` then force-set upstream — dangerous now the repo is public), `debug_model_cache.sh` and `test_cache_detection.sh` (one-off diagnostics), and `fix-dependencies.sh` (deleted `package-lock.json`, which the production image's `npm ci` requires). Thirteen scripts remain, all referenced and syntax-checked. |

**Also rotated in the same pass**, though not previously listed: `DB_PASSWORD` was the publicly-known default `ai_catalogue_password` from `docker-compose.yml`. Rotated via `ALTER USER`, and the weak default removed from compose (now required). `DJANGO_SECRET_KEY`'s weak compose default was likewise made required.

`PROJECT_API_KEY_ENCRYPTION_KEY` was **not** leaked (`.env` has never been committed) but was rotated anyway as hygiene. All 20 stored provider keys were re-encrypted and verified — see the runbook in [`DEPLOYMENT.md`](DEPLOYMENT.md#11-rotating-encryption-keys).
