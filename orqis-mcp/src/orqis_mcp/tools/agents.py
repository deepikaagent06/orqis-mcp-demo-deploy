"""MCP tool: list_agents.

Thin, read-only wrapper around the existing
services.agent_definition_service.list_definitions() — no new query logic.
"""
from services import agent_definition_service

from orqis_mcp.tools import tenant_scope


def register(mcp) -> None:
    @mcp.tool()
    async def list_agents(
        tenant_id: str,
        status: str | None = None,
        include_marketplace: bool = False,
        reports_to_agent_id: str | None = None,
        top_level_only: bool = False,
    ) -> list[dict]:
        """List AgentDefinitions visible to the given tenant (read-only).

        Calls services.agent_definition_service.list_definitions() exactly
        as the existing GET /api/agent-definitions endpoint does, scoped to
        tenant_id via the same tenant-context mechanism the ORQIS backend
        already uses. status/include_marketplace/reports_to_agent_id/
        top_level_only map directly to that function's own parameters — see
        its docstring for their exact semantics (include_marketplace also
        includes the read-only shared Marketplace Registry catalog).
        """
        async with tenant_scope(tenant_id):
            return await agent_definition_service.list_definitions(
                status=status,
                include_marketplace=include_marketplace,
                reports_to_agent_id=reports_to_agent_id,
                top_level_only=top_level_only,
            )
