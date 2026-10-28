# Reproducibility & Data-Segregation Analysis

**Date:** 2026-08-11
**Question:** the IntelliDoc repository is public. Which settings currently live only in the runtime datastores (PostgreSQL, Redis, Milvus, Docker) that *should* be in the repository, so that someone cloning it can stand up an equivalent environment — without ever publishing private data?

**Method:** every finding below was verified against the running system and the repository. Where a claim is testable, the test and its result are stated. The analysis was performed first; §7 records what was subsequently implemented.

---

## 1. Verdict

The **privacy** side of the segregation is in good shape. The **reproducibility** side has real gaps: a clean clone that follows `.env.example` **cannot connect to Milvus**, gets an unbounded Redis cache, an untuned PostgreSQL, and no usable example workflow.

The distinction that matters is not "config vs data" but:

| Category | Belongs in the repo? | Why |
|---|---|---|
| **Infrastructure settings** — memory limits, eviction policy, tuning, index parameters | **Yes** | Behaviour differs materially without them. They are not secret. |
| **Bootstrap procedure** — how a datastore reaches a working state | **Yes** | Otherwise it exists only as tribal knowledge. |
| **Reference content** — an example workflow, provider registry rows | **Yes** | A cloner has nothing to test against. |
| **Instance secrets** — keys, passwords, certificates | **No** | Already correctly excluded. |
| **Operational data** — documents, conversations, embeddings, accounts, usage logs | **No** | Already correctly excluded. |

Most gaps below are in the first three rows: **non-secret settings that currently exist only because someone typed them into a running system.**

---

## 2. What is already segregated correctly

Confirmed by scanning the working tree and the full history:

- **`.env` has never been committed** and is covered by four `.gitignore` patterns.
- **No provider API key, private key, or credential file exists anywhere in history.** The one historical exposure (a Fernet key committed as a compose default) has been rotated and the default removed.
- **`backup/`, `volumes/`, `media/`, `logs/`, `*.sql`, `*.pem` are all ignored** — the database dumps and `.env` copies created during recent key-rotation work are untracked.
- **A pre-commit hook blocks** environment files, private keys, database dumps and key-shaped literals before they can be committed.
- **Operational data stays in the datastores**, as it should: uploaded documents, conversation histories, embeddings, user accounts, encrypted per-project API keys, request logs.

This part needs no change. The recommendations below add *non-secret* configuration; none of them weaken this.

---

## 3. Findings

### 3.1 `.env.example` gives Milvus credentials that do not work — **critical**

`.env.example` instructs a cloner to set:

```
MILVUS_ROOT_USER=milvusadmin
MILVUS_ROOT_PASSWORD=your_secure_milvus_password_here
```

Milvus does not create a `milvusadmin` user. Authorisation is enabled (`COMMON_SECURITY_AUTHORIZATIONENABLED: "true"`) and Milvus only ever has its built-in `root`. **Nothing in the repository creates a Milvus user or applies a password** — a repo-wide search for `create_user`, `create_role` or `grant_role` against Milvus returns nothing.

Verified by connecting with each credential set:

| Credentials | Result |
|---|---|
| `.env.example` values verbatim | **fails** |
| `.env.example` user + default password | **fails** |
| This instance's actual values | connects (27 collections) |
| Milvus built-in `root` / `Milvus` | connects (27 collections) |

So a cloner following the documented example hits an authentication failure with no guidance, and the only working path is undocumented.

### 3.2 The Milvus password is the vendor default — **security + reproducibility**

The last two rows above are the same account: this instance's `MILVUS_ROOT_PASSWORD` is literally `Milvus`, the value Milvus ships with. Authorisation is switched on, but protected by a password published in Milvus's own documentation.

Exposure is currently limited because port 19530 binds `127.0.0.1`, so the loopback binding — not the password — is what is actually protecting the vector store. Any cloner who copies this arrangement inherits the same posture without realising it.

**What the repo should carry:** a documented bootstrap step that changes the Milvus root password on first run, and honest `.env.example` values (`MILVUS_ROOT_USER=root`) with a note that the password must be rotated away from the default.

### 3.3 Redis has no memory ceiling and will refuse writes rather than evict — **high**

Live configuration, none of which is expressed anywhere in the repository:

| Setting | Value | Consequence |
|---|---|---|
| `maxmemory` | `0` (unlimited) | Grows until host memory is exhausted |
| `maxmemory-policy` | `noeviction` | On reaching any limit, **writes are rejected** instead of old keys being dropped |
| `appendonly` | `yes` | Set in compose ✓ |
| `save` | `3600 1 300 100 60 10000` | Image default, undocumented |

This matters more here than in a typical cache, because Redis holds **fetched web-page content** for the web-search feature — a single project can cache hundreds of pages. Current usage is 91.5 MB across 1,215 keys, with a 129 MB peak, and that grows with every URL added to an agent.

`noeviction` is the wrong policy for a cache. Under memory pressure the correct behaviour is to discard the oldest cached pages; instead the platform will start failing writes, which surfaces as web-search caching silently breaking rather than degrading.

**What the repo should carry:** an explicit `maxmemory` and `maxmemory-policy allkeys-lru` in the Redis service command, with the value driven by an env var so operators can size it.

### 3.4 PostgreSQL runs on stock defaults in the only stack that starts — **medium**

Live values, all image defaults:

| Setting | Value |
|---|---|
| `shared_buffers` | 128 MB (16384 × 8 kB) |
| `work_mem` | 4 MB |
| `effective_cache_size` | 4 GB |
| `max_connections` | 100 |
| `log_min_duration_statement` | `-1` (slow-query logging off) |

Tuning *does* exist in `docker-compose.prod.yml` (`max_connections=200`, `shared_buffers=256MB`, …) — and since B1/B2 were fixed on 2026-10-28 that overlay can start. It is still not what runs here by default: `docker-compose.override.yml` is auto-merged by any bare `docker compose` command, so the tuned configuration applies only when you start with `-f docker-compose.yml -f docker-compose.prod.yml`.

**What the repo should carry:** move the tuning into the base compose file, or fix the production overlay so it is reachable. Also worth enabling `log_min_duration_statement` so slow queries are visible at all.

### 3.5 The shipped example workflow is in an obsolete format — **medium**

`backend/schemas/example_workflow.json` exists **on disk but was never tracked by git** — a blanket `*.json` rule in `.gitignore` (intended for large data files, with negations only for `package.json`) excluded it, along with `agent_workflow_schema.json`. So a cloner receives neither the example nor the workflow JSON Schema. On top of that, the file itself was obsolete:

- Top-level keys are `workflow_id, workflow_name, workflow_description, version, created_at, updated_at, project_id, agents`.
- It contains an **`agents`** array — the current format is `graph_json` with **`nodes`** and **`edges`**. Parsed against the live format it yields **0 nodes, 0 edges**.
- It has no `schema_version`, which the import endpoint requires (`schema_version == "1"`).

So it cannot be imported, and it documents a format the engine no longer uses. A cloner ends up with a working platform and no way to see a multi-agent workflow actually run.

**What the repo should carry:** one small, genuinely importable export bundle — e.g. Start → Splitter → two Assistants → End, with prompts written as placeholders. Exporting a real workflow would be the wrong move (see §4), but a synthetic equivalent is pure configuration.

### 3.6 No provider registry seed — **medium**

`LLMProvider` has **0 rows**, and no management command or migration seeds it. The `llm_eval` comparison feature is therefore inert on a fresh install — and, as it happens, on this instance too. `APIKeyConfig` is likewise empty.

**What the repo should carry:** a data migration or seeder inserting the three providers (`openai`, `anthropic`/`claude`, `google`/`gemini`) with their endpoints and token limits. These are public facts, not secrets.

### 3.7 Per-agent operational defaults exist only inside `graph_json` — **medium**

Several values that materially change behaviour live only inside workflow rows in PostgreSQL, with no documented default:

| Setting | Current live values | Where documented |
|---|---|---|
| `web_search_cache_ttl` | 7 days (AICC), 30 days (FIONA) | nowhere |
| `search_method` | `hybrid_search` | code default only |
| `web_search_max_results` / `top_k` | 5 / 5 | code default only |
| `llm_model` per node | `gpt-5.5` | nowhere |

Two live projects are on different cache TTLs purely because they were configured at different times. A cloner has no way to know what a sensible value is.

**What the repo should carry:** a short "recommended agent defaults" table in the documentation, and ideally project-level defaults in the template definitions so new projects start consistent.

### 3.8 Dead Milvus config files are misleading — **low**

`backend/milvus.yaml` (2,627 lines) and `backend/milvus-simple.yaml` (643 lines) are in the repository but **mounted by no compose file**. The Milvus service is configured entirely through environment variables. A cloner reasonably assumes these files are authoritative and edits them with no effect.

**What the repo should do:** delete them, or mount one and make it the real source of Milvus configuration.

### 3.9 Bootstrap creates a superuser with a published password — **security**

`create_default_icons` creates `admin@example.com` / `adminpassword` when no superuser exists. On a fresh clone that is a known-credential administrator account on a system that may be internet-facing. (Already recorded as S12 in `KNOWN_ISSUES.md`; repeated here because it is squarely a *bootstrap* concern.)

**What the repo should do:** prompt for credentials, generate a random password and print it once, or refuse to run non-interactively.

---

## 4. What should stay out of the repository

Recommending these *not* be added is as important as the gaps above:

- **Workflow `graph_json` for real projects** — the AICC and FAIRsharing workflows contain organisation-specific prompts, curated URL lists and routing policy. They are intellectual work product, and the prompts encode internal knowledge (contact addresses, escalation paths). Ship a synthetic example instead.
- **Uploaded documents and their embeddings** — third-party and potentially licensed content.
- **Conversation histories and deployment sessions** — end-user content, some from public visitors.
- **User accounts, permission grants, API keys** — even encrypted, ciphertext plus a public codebase invites offline attack.
- **TLS private keys and certbot state.**
- **Database dumps and volume archives** — currently under `backup/`, correctly ignored.

The consistent principle: **publish the shape, never the contents.** A schema, an index definition, a memory policy and a bootstrap script are all shape. A workflow full of real prompts, or a table full of real conversations, is contents.

---

## 5. Priority

| # | Finding | Severity | Effort |
|---|---|---|---|
| 1 | `.env.example` Milvus credentials fail (§3.1) | **Critical** — blocks first run | Trivial |
| 2 | Redis `noeviction` with no `maxmemory` (§3.3) | **High** — silent failure under load | Trivial |
| 3 | Milvus vendor-default password (§3.2) | **High** | Small |
| 4 | Bootstrap superuser password (§3.9) | **High** | Small |
| 5 | PostgreSQL untuned in the startable stack (§3.4) | Medium | Small |
| 6 | Example workflow obsolete (§3.5) | Medium | Small |
| 7 | No `LLMProvider` seed (§3.6) | Medium | Small |
| 8 | Agent defaults undocumented (§3.7) | Medium | Small |
| 9 | Dead Milvus YAML files (§3.8) | Low | Trivial |

Findings 1 and 2 are the ones that would actually bite: the first stops a clone from working at all, the second degrades this instance as its web-search cache grows.

---

## 6. Suggested repository additions

None of these contain secrets:

1. `docker-compose.yml` — Redis `--maxmemory ${REDIS_MAXMEMORY:-512mb} --maxmemory-policy allkeys-lru`; PostgreSQL tuning moved into the base stack.
2. `.env.example` — correct Milvus user, an explicit warning about the default password, and the new Redis sizing variable.
3. `scripts/bootstrap.sh` — one idempotent script that rotates the Milvus root password, seeds `LLMProvider` rows, and creates an administrator with a generated password.
4. `backend/schemas/example_workflow.json` — replaced with a valid, importable bundle (`schema_version: "1"`, real `nodes`/`edges`, placeholder prompts).
5. `docs/DEPLOYMENT.md` — a "first run on a fresh clone" section, plus the recommended agent defaults table from §3.7.
6. Delete `backend/milvus.yaml` and `backend/milvus-simple.yaml`, or wire one in.

Together these would let someone clone the repository and reach a working, sensibly-configured system — while every piece of private data stays exactly where it is now.

---

## 7. Implementation status — 2026-08-13

All nine findings addressed. Verified against the running system, not just written.

| # | Finding | Resolution |
|---|---|---|
| §3.1 | `.env.example` Milvus credentials failed | `MILVUS_ROOT_USER=root` with a comment stating Milvus creates no users; password marked REQUIRED and rotate-away-from-default |
| §3.2 | Milvus vendor-default password | `scripts/bootstrap.sh` rotates it via `utility.reset_password` and writes the new value to `.env` |
| §3.3 | Redis unbounded / `noeviction` | `--maxmemory ${REDIS_MAXMEMORY:-512mb} --maxmemory-policy allkeys-lru` in compose. Applied live: 512 MB, `allkeys-lru`, all 1,215 keys survived the restart |
| §3.4 | PostgreSQL untuned in the startable stack | Tuning moved into the base compose file. Applied live: `shared_buffers` 128→256 MB, `work_mem` 4→8 MB, `max_connections` 200, slow-query logging at 2 s. Row counts identical before and after |
| §3.5 | Example workflow obsolete **and untracked** | Replaced with a valid bundle (`schema_version: "1"`, 6 nodes, 6 edges). Verified: passes the graph invariants and imports through the real endpoint with HTTP 201 and no warnings. The blanket `*.json` ignore rule also hid it and `agent_workflow_schema.json` from the repository entirely; narrow negations added for those two files only, leaving the data-file rule intact |
| §3.6 | No `LLMProvider` seed | `bootstrap.sh` seeds the three providers idempotently via `get_or_create` |
| §3.7 | Agent defaults undocumented | Recommended-defaults table added to `DEPLOYMENT.md` §12, including the traps (`temperature: 0` coercion, inert `index_type`) |
| §3.8 | Dead Milvus YAML files | `backend/milvus.yaml` and `milvus-simple.yaml` deleted; nothing referenced them |
| §3.9 | Bootstrap superuser with a published password | `create_default_icons` no longer creates one implicitly — it explains how instead. `bootstrap.sh` generates a password and prints it once |

**Not changed, deliberately:** no project data entered the repository. The example
workflow is synthetic, with placeholder prompts and empty URL lists. Real workflow
`graph_json`, documents, embeddings, conversations, accounts and keys all remain
exclusively in the datastores.
