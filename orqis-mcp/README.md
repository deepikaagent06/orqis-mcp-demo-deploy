# ORQIS MCP

ORQIS MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes read-only ORQIS AI Workspace capabilities — workspaces, use cases, agents, shared memory, and temporal knowledge — as tools for MCP-compatible AI clients such as Claude Desktop.

## What it provides

- **Workspaces** — list an organization's ORQIS workspaces
- **Use Cases** — browse the shared use-case catalog
- **Agents** — list registered agent definitions
- **Shared Memory** — read existing shared-memory entries for an agent/tenant
- **Temporal Knowledge** — retrieve an entity's state as-of a point in time, plus its full version history

## Architecture

ORQIS MCP is a thin MCP interface over an existing ORQIS backend — it introduces no new storage and no new business logic.

```
MCP Client
    ↓
ORQIS MCP
    ↓
ORQIS Backend
    ↓
ORQIS capabilities
```

## Quick Start

**Prerequisites:** Python 3.11+, and a local ORQIS repository checkout with `backend/` fully configured and runnable (dependencies installed, `.env` set up with a working `DATABASE_URL`).

```bash
cd orqis-mcp
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -e .
pip install -r /path/to/ORQIS/backend/requirements.txt
```

Configure the backend path:

```bash
cp .env.example .env
# edit .env and set ORQIS_BACKEND_PATH to your ORQIS/backend directory
```

Run the server:

```bash
orqis-mcp
```

This starts the server on stdio. Point an MCP client at the `orqis-mcp` command, for example in Claude Desktop's `mcpServers` config, with `ORQIS_BACKEND_PATH` set in its `env`.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `list_workspaces` | List workspaces for a tenant |
| `list_use_cases` | List the shared use-case catalog |
| `list_agents` | List registered agent definitions |
| `get_shared_memory` | Read shared-memory entries |
| `get_temporal_knowledge` | Get an entity's state as-of a timestamp plus its version history |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Docker / Streamable HTTP

In addition to stdio, `orqis-mcp` supports the Streamable HTTP transport, controlled by `MCP_TRANSPORT`, `MCP_HOST`, and `MCP_PORT`. This standalone repository does not include Docker packaging; the current Docker/Compose deployment configuration for running `orqis-mcp` in a container lives in the ORQIS monorepo.

## Security

Tenant-scoped tools take an explicit `tenant_id` argument rather than deriving it from an authenticated session. There is currently no authentication layer mapping a caller's identity to a `tenant_id`, so any client able to invoke these tools can read any tenant's data. Do not point this at a production ORQIS deployment or database.

## License

MIT
