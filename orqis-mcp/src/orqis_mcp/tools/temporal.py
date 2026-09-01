"""MCP tool: get_temporal_knowledge.

Thin, read-only wrapper around the existing
services.knowledge_temporal_service — no new query logic. Exposes exactly
the temporal semantics that module already defines (entity_type, entity_ref,
optional as_of), and calls both of its read functions rather than inventing
a new one:
  - compose_temporal_context(): the single ledger version effective at
    as_of, plus its real citations.
  - get_version_history(): every recorded version for that entity, oldest
    first — the full timeline, not just the as_of snapshot.
"""
from datetime import datetime

from services import knowledge_temporal_service

from orqis_mcp.tools import tenant_scope


def register(mcp) -> None:
    @mcp.tool()
    async def get_temporal_knowledge(
        tenant_id: str,
        entity_type: str,
        entity_ref: str,
        as_of: str | None = None,
    ) -> dict:
        """Read governed temporal knowledge for one entity (read-only).

        entity_type / entity_ref identify the entity exactly as
        services.knowledge_temporal_service does (the same entity_refs
        JSONB membership check the backend already uses). as_of is an
        optional ISO 8601 timestamp ("what was true at this moment in real-world
        effective time"); when omitted it defaults to now.

        Returns a dict with:
          - "effective": the services.knowledge_temporal_service.compose_temporal_context()
            result — the single ledger version effective at as_of plus its
            citations, or null if no version was ever effective at that time.
          - "version_history": the services.knowledge_temporal_service.get_version_history()
            result — every recorded version for this entity, oldest first.
        """
        moment = datetime.fromisoformat(as_of) if as_of else None
        async with tenant_scope(tenant_id):
            effective = await knowledge_temporal_service.compose_temporal_context(
                entity_type, entity_ref, as_of=moment,
            )
            history = await knowledge_temporal_service.get_version_history(entity_type, entity_ref)
        return {"effective": effective, "version_history": history}
