"""MCP tool: list_use_cases.

Thin, read-only wrapper around the existing
services.use_case_service.list_use_cases() — no new query logic. This
catalog is tenant-less by design (see that module's docstring: it is the
shared Marketplace "Use Cases" catalog, not per-tenant data), so this tool
takes no tenant_id.
"""
from services import use_case_service


def register(mcp) -> None:
    @mcp.tool()
    async def list_use_cases(
        starter_only: bool = False,
        include_workflows: bool = False,
        include_archived: bool = False,
    ) -> list[dict]:
        """List the ORQIS Use Case catalog (read-only).

        Calls services.use_case_service.list_use_cases() exactly as the
        existing GET /api/use-cases endpoint does. starter_only,
        include_workflows, and include_archived map directly to that
        function's own parameters — see its docstring for their exact
        semantics.
        """
        return await use_case_service.list_use_cases(
            starter_only=starter_only,
            include_workflows=include_workflows,
            include_archived=include_archived,
        )
