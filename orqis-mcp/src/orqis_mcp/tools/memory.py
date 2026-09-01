"""MCP tool: get_shared_memory.

Thin, read-only wrapper around the existing
services.agent_memory_service.list_shared_memory() — no new query logic and
no new retrieval behavior. This exposes that function's existing list/read
behavior only. It is deliberately NOT called or framed as "semantic search":
ORQIS does not currently provide a semantic-search function over agent
memory (see agent_memory_service.py's module docstring — retrieve_context()
does newest-first merging of AGENT + SHARED entries, not embedding/similarity
search), so this tool does not claim capability the backend doesn't have.
"""
from services import agent_memory_service

from orqis_mcp.tools import tenant_scope


def register(mcp) -> None:
    @mcp.tool()
    async def get_shared_memory(
        tenant_id: str,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Read the tenant's cross-agent Shared Memory entries (read-only).

        Calls services.agent_memory_service.list_shared_memory() exactly —
        every entry any AgentDefinition in this tenant has explicitly
        written with scope="shared", most-recent-first, optionally filtered
        to memory_type ("episodic" or "semantic"). This is a list/read
        operation, not semantic search.
        """
        async with tenant_scope(tenant_id):
            return await agent_memory_service.list_shared_memory(
                memory_type=memory_type, limit=limit,
            )
