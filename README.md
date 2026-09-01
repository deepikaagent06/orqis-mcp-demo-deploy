# ORQIS MCP Demo

A small, self-contained demo deployment of the ORQIS MCP server
for the ORQIS AI Workspace.

## What it demonstrates

This demo exposes five ORQIS MCP tools over Streamable HTTP:

- `list_workspaces`
- `list_use_cases`
- `list_agents`
- `get_shared_memory`
- `get_temporal_knowledge`

It runs against an isolated local PostgreSQL database populated
with synthetic demo data.

## Package contents

- `orqis-mcp/` — ORQIS MCP server
- `backend_slice/` — minimal backend services required by the demo
- `seed_demo.py` — creates synthetic demo data
- `entrypoint.sh` — starts the isolated demo environment
- `Dockerfile` — builds the demo container

## Run locally

From the repository root:

```bash
docker build -t orqis-mcp-demo:local .
docker run --rm -p 8001:8001 orqis-mcp-demo:local
```

The MCP endpoint is available at `http://localhost:8001/mcp`, with a
health check at `http://localhost:8001/health`.

## Verifying the server

```bash
curl http://localhost:8001/health

npx @modelcontextprotocol/inspector --cli http://localhost:8001/mcp \
  --transport http --method tools/list
```

Call a tool:

```bash
npx @modelcontextprotocol/inspector --cli http://localhost:8001/mcp \
  --transport http --method tools/call --tool-name list_workspaces \
  --tool-arg tenant_id=demo-tenant-pullak
```

## Notes

- All data in this demo is synthetic — no real customer or tenant data
  is included.
- The database runs entirely inside the container and is not exposed
  outside it.
