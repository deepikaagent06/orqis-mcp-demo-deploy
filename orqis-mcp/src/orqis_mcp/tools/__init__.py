"""Shared helper for the orqis_mcp tool modules.

Each sibling module (workspaces.py, use_cases.py, agents.py, memory.py,
temporal.py) wraps exactly one existing ORQIS service-layer read function as
an MCP tool. orqis_mcp.server puts a local ORQIS/backend checkout on
sys.path before any of these modules are imported, so the plain
`from services import ...` / `from middleware.tenant_context import ...`
imports below resolve to that backend's real code.

tenant_scope() is the only shared piece of logic: for the duration of one
tool call it sets the ambient tenant_id that ORQIS service functions already
read via middleware.tenant_context.get_tenant_id() — the exact same
contextvar SessionAuthMiddleware populates per HTTP request in the real app.
An MCP tool call has no session cookie to derive that from, so callers pass
tenant_id explicitly as a tool argument instead; nothing here bypasses or
replaces the underlying tenant-context mechanism itself. See README.md's
"Tenant isolation" section for why this is a documented demo limitation,
not production authentication.
"""
from contextlib import asynccontextmanager

from middleware.tenant_context import reset_tenant_id, set_tenant_id


@asynccontextmanager
async def tenant_scope(tenant_id: str):
    token = set_tenant_id(tenant_id)
    try:
        yield
    finally:
        reset_tenant_id(token)
