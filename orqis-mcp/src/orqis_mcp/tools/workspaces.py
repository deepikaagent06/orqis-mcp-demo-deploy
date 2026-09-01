"""MCP tool: list_workspaces.

Thin, read-only wrapper around the existing
services.workspace_service.list_workspaces() — no new query logic. That
function already overlays each workspace with the caller's own
tenant_workspaces enablement/run-stats (see its module docstring), so this
tool's only added responsibility is tenant-context scoping
(orqis_mcp.tools.tenant_scope).
"""
from services import workspace_service

from orqis_mcp.tools import tenant_scope


def register(mcp) -> None:
    @mcp.tool()
    async def list_workspaces(tenant_id: str) -> list[dict]:
        """List ORQIS workspaces visible to the given tenant (read-only).

        Calls services.workspace_service.list_workspaces() exactly as the
        existing GET /api/workspaces endpoint does, scoped to tenant_id via
        the same tenant-context mechanism the ORQIS backend already uses.
        Each item includes the catalog fields (name, slug, description,
        status, owner, health, ...) plus this tenant's own enabled/
        runtime_status/last_run/recent_runs.
        """
        async with tenant_scope(tenant_id):
            return await workspace_service.list_workspaces()
