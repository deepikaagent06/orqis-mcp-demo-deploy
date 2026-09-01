"""orqis-mcp — a read-only Model Context Protocol server exposing five
existing ORQIS backend services as MCP tools.

This is a demo spike, not a new backend, and it is **not standalone**:
  - It does not modify any ORQIS router or service.
  - It does not add a second database, memory store, or temporal knowledge
    store — every tool below calls straight into an existing ORQIS backend
    checkout's services modules, which own that data.
  - It does not introduce Neo4j; the temporal-knowledge tool reads the same
    Postgres-backed knowledge_ledger_entries the backend already maintains.
  - Every tool is read-only (list_*/get_* service functions only).
  - There is no ORQIS HTTP API for these five read operations, so this
    package does not invent one. It imports the backend's `database`,
    `middleware`, and `services` packages in-process instead — see
    _bootstrap_backend_path() below and README.md's "Dependency on ORQIS"
    section for exactly what that requires you to have running locally.

Architecture:  MCP client -> this server -> an existing ORQIS backend
checkout's services -> that backend's own Postgres storage.
_bootstrap_backend_path() is the only "new" plumbing: it puts your local
ORQIS/backend directory on sys.path so orqis_mcp.tools can
`import services...`/`import database`/`import middleware...` and reuse the
real backend code in-process, the same way ORQIS/backend/scripts/
seed_northstar_commerce.py already does for a standalone script.

Run: see README.md.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bootstrap_backend_path() -> None:
    """Put a local ORQIS backend checkout on sys.path so orqis_mcp.tools can
    import its real `database`, `middleware`, and `services` packages
    in-process, without copying or modifying any backend code. Must run
    before anything under orqis_mcp.tools is imported.

    Requires ORQIS_BACKEND_PATH (environment variable, or set in a `.env`
    file — see .env.example) pointing at that ORQIS/backend directory.
    There is no implicit default path: this package is distributed on its
    own and has no fixed relationship to where you keep your ORQIS
    checkout.
    """
    backend_path_str = os.environ.get("ORQIS_BACKEND_PATH")
    if not backend_path_str:
        raise RuntimeError(
            "ORQIS_BACKEND_PATH is not set. orqis-mcp is not standalone: it "
            "imports an existing ORQIS backend checkout's database/"
            "middleware/services packages in-process, so it needs the path "
            "to your local ORQIS/backend directory (with that backend's own "
            "dependencies installed and its DATABASE_URL configured). Set "
            "ORQIS_BACKEND_PATH to that directory — e.g. in a .env file, "
            "see .env.example."
        )
    backend_path = Path(backend_path_str).expanduser().resolve()
    if not backend_path.is_dir():
        raise RuntimeError(f"ORQIS_BACKEND_PATH does not exist: {backend_path}")
    sys.path.insert(0, str(backend_path))


_bootstrap_backend_path()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from orqis_mcp.tools import agents, memory, temporal, use_cases, workspaces  # noqa: E402

# host/port only take effect for the streamable-http transport (see main());
# they're inert under the default stdio transport.
mcp = FastMCP(
    "orqis-mcp",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8001")),
)

workspaces.register(mcp)
use_cases.register(mcp)
agents.register(mcp)
memory.register(mcp)
temporal.register(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Docker/nginx healthcheck (docker/mcp.Dockerfile, docker/nginx.conf).
    Deliberately returns no backend or tenant data — liveness only."""
    return JSONResponse({"status": "ok"})


def main() -> None:
    """Entry point for the `orqis-mcp` console script.

    Runs over stdio by default (unchanged). Set MCP_TRANSPORT=streamable-http
    to serve the MCP endpoint at http://MCP_HOST:MCP_PORT/mcp instead (see
    README.md's "Docker / Streamable HTTP" section).
    """
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
