# orqis-mcp-demo-deploy

A **private, temporary, single-container** deployment package for the
`orqis-mcp` MCP server — built for a Glama demo for Pullak. This directory
is separate from, and does not modify:

- the standalone `orqis-mcp/` repository (its source is copied in, unchanged)
- `ORQIS/backend` (only specific files are copied, unchanged, into `backend_slice/`)
- the existing ORQIS monorepo Docker/Compose setup (`ORQIS/docker/`, `ORQIS/docker-compose.yml`)

It contains **only synthetic demo data** for a fictional company
("Northwind Retail") under a dedicated demo tenant id, `demo-tenant-pullak`.
No real ORQIS customer, tenant, or company data is included anywhere in this
package.

## What this is

One Docker image containing:

1. A local PostgreSQL instance, initialized and started **inside the
   container**, bound only to `127.0.0.1` — never reachable from outside the
   container, and never configurable to point elsewhere (see "Isolation"
   below).
2. `backend_slice/` — the 12 `ORQIS/backend` service modules (plus
   `config.py`, `database/`, `middleware/tenant_context.py`) proven
   necessary, by import-graph investigation, to run the five orqis-mcp
   tools. Not the full backend — no routers, no platform/, no frontend, no
   Redis, no Neo4j/Graphiti, no OpenAI calls.
3. `orqis-mcp/` — the unmodified standalone MCP server source (same five
   tools: `list_workspaces`, `list_use_cases`, `list_agents`,
   `get_shared_memory`, `get_temporal_knowledge`).
4. `seed_demo.py` — an idempotent synthetic-data seed, run once at container
   startup, that calls the real `backend_slice` service functions
   (`create_definition()`, `write_semantic_memory()`,
   `knowledge_ledger_service.create_entry()`, ...) rather than hand-written
   SQL, so seeded data goes through the same validation/projection real
   callers get.

## Why a self-contained Postgres instead of SQLite

Every `backend_slice` service uses raw `asyncpg` SQL with Postgres-specific
features (`JSONB` columns with a custom type codec, `$1`/`$2` positional
params, `ANY($1::text[])` array casts, `ON CONFLICT`, `TIMESTAMPTZ`) —
not an ORM. SQLite is not a safe drop-in without rewriting the service SQL,
which is out of scope (this package does not modify backend service files).
Running a real, disposable Postgres inside the container keeps the image
self-contained without touching any service code.

## Backend modules copied (`backend_slice/`)

| File | Why |
|---|---|
| `config.py` | `Settings` (all fields have safe defaults; only `DATABASE_URL` is actually used) |
| `database/__init__.py` | asyncpg pool + JSONB/JSON type codec |
| `middleware/tenant_context.py` | ambient `tenant_id` contextvar (`tenant_scope()` in orqis-mcp sets it per tool call) |
| `services/workspace_service.py` | `list_workspaces` |
| `services/tenant_service.py` | workspace_service's per-tenant enablement overlay |
| `services/runs_service.py` | workspace_service's run-stats overlay |
| `services/use_case_service.py` | `list_use_cases` |
| `services/agent_definition_service.py` | `list_agents` |
| `services/agent_runtime_service.py` | imported by agent_definition_service (not invoked by `list_definitions`, but the import must resolve) |
| `services/ingestion_service.py` | imported by agent_runtime_service (same reason) |
| `services/capability_catalog.py` | static Python capability catalog, no DB |
| `services/agent_memory_service.py` | `get_shared_memory` |
| `services/knowledge_ledger_service.py` | `get_temporal_knowledge` |
| `services/knowledge_context_service.py` | creates `knowledge_citations` (required by `get_temporal_knowledge`'s citation reads, even though this module is never imported by the tool itself — a real gap found during investigation: `knowledge_temporal_service.ensure_schema()` alone does not create this table) |
| `services/knowledge_temporal_service.py` | `get_temporal_knowledge` |

**Known limitation:** `list_use_cases(include_workflows=True)` is not
supported by this minimal slice — that path needs
`services/workflow_definition_service.py`, which is intentionally not
copied here to keep the slice to what the five tools' default/tested
call paths actually require. The tool still registers and works correctly
with `include_workflows=False` (its default).

## Architecture

```
docker build  →  one image
docker run    →  one container:
  entrypoint.sh
    1. initdb (first run only) + start local Postgres, 127.0.0.1-only
    2. create demo role/database if missing (idempotent)
    3. compute DATABASE_URL internally (never taken from outside)
    4. python seed_demo.py   (idempotent synthetic data)
    5. exec orqis-mcp        (streamable-http, non-root user)
```

MCP endpoint: `http://<host>:8001/mcp`
Health check: `http://<host>:8001/health`

## Security / isolation

- **DATABASE_URL is always computed inside `entrypoint.sh`** from
  `DEMO_PG_USER`/`DEMO_PG_PASSWORD`/`DEMO_PG_DB` and `127.0.0.1`. Any
  `DATABASE_URL` passed into the container from outside (`docker run -e
  DATABASE_URL=...`) is overwritten before the app ever starts — this image
  can never be pointed at the real ORQIS Postgres or any other external
  database.
- Postgres `listen_addresses` is set to `127.0.0.1` only — not reachable
  even from other containers on the same Docker network, let alone the host.
- No host port is published for Postgres; only `8001` (MCP) is `EXPOSE`d,
  and even that is not published unless the `docker run`/Glama deployment
  explicitly maps it.
- No `.env`, credential, or secret file from `ORQIS/backend` is copied into
  the image (`.dockerignore` excludes `.env`/`*.env` everywhere in the build
  context as defense in depth). All `config.py` defaults are blank or
  dev-safe; only `DATABASE_URL` (generated in-container) is actually used by
  the five tools' code paths.
- **Tenant isolation is unchanged from the standalone `orqis-mcp` demo
  limitation:** `tenant_id` is a trusted, unauthenticated tool argument (see
  `orqis-mcp/README.md`'s "Tenant isolation" section). Since this database
  contains *only* the synthetic `demo-tenant-pullak` data seeded by
  `seed_demo.py`, the practical exposure is: anyone who can reach the `/mcp`
  endpoint can read that same synthetic data under any `tenant_id` they
  choose to pass — never anything beyond what `seed_demo.py` wrote. This is
  acceptable for a private, throwaway demo but is not production
  authentication (per the task's explicit instruction not to add any).
- Restarting the container reuses the same Postgres data directory inside
  its writable layer; `seed_demo.py` is idempotent so this never duplicates
  rows. Removing the container discards all data — there is no volume
  mount, so every fresh container starts from a clean, identical synthetic
  dataset.

## Local verification

```bash
docker build -t orqis-mcp-demo:local -f Dockerfile .
docker run --rm -p 8001:8001 --name orqis-mcp-demo orqis-mcp-demo:local
curl http://localhost:8001/health
npx @modelcontextprotocol/inspector --cli http://localhost:8001/mcp --transport http --method tools/list
```

Call a tool (example via MCP Inspector CLI):

```bash
npx @modelcontextprotocol/inspector --cli http://localhost:8001/mcp --transport http \
  --method tools/call --tool-name list_workspaces \
  --tool-arg tenant_id=demo-tenant-pullak
```

## Not done yet (by design)

- Not deployed to Glama.
- Not pushed/committed anywhere.
- `glama.json` / Glama-specific deployment config not added here — this
  package only proves the image builds and runs correctly standalone.
