"""Temporal Knowledge — answers "what was true, according to our governed
knowledge, at time T" by querying the effective_from/effective_to/
superseded_by_entry_id/superseded_at columns services/knowledge_ledger_service.py
added to the existing knowledge_ledger_entries table. This is NOT a second
Knowledge Base: every function here reads knowledge_ledger_entries and
knowledge_citations, the same tables services/knowledge_context_service.py and
services/agent_context_resolver.py already use — it just answers a different
question than that layer does.

Why a separate module instead of extending knowledge_context_service.py: that
layer's knowledge_entities table is a *current-state* projection — it upserts
one row per (tenant, entity_type, entity_ref) and accumulates mention_count/
last_seen_at across every ledger entry that mentions it, so by design it
collapses "Policy A" and "Policy B" (two ledger entries about the same
entity_ref, effective at different times) into a single current row. That
collapsing is exactly right for "what applies now" but structurally cannot
answer "what applied on 2026-05-01" — the per-version distinction has already
been discarded by the time a row lands in knowledge_entities. This module
queries knowledge_ledger_entries directly, per version, before any such
collapsing happens.

Three concepts, kept distinct per the temporal semantics this module must
preserve:
- effective time (effective_from/effective_to): when the fact was/is true in
  the real world.
- ingestion time (created_at, already on knowledge_ledger_entries): when
  ORQIS learned about it.
- supersession (superseded_by_entry_id/superseded_at): when a later version
  replaced an earlier one — see knowledge_ledger_service.mark_superseded().

Citations: every ledger entry's provenance is already recorded in
knowledge_citations (services/knowledge_context_service.py.project_ledger_entry
writes one row per entity/relationship mention, keyed by ledger_entry_id).
get_citations_for_entry() below reads those same rows for one specific
version — never fabricated, never a second citation table.

Tenant isolation: every function here goes through
middleware.tenant_context.get_tenant_id() and filters by it, exactly like
every other service in this codebase.
"""
import logging
from datetime import datetime, timezone

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)


async def ensure_schema() -> None:
    """The temporal columns this module reads live on knowledge_ledger_entries
    itself — services/knowledge_ledger_service.py.ensure_schema() already
    creates them. No separate table, so nothing to create here."""
    from services import knowledge_ledger_service
    await knowledge_ledger_service.ensure_schema()


def _entry_row_to_dict(r) -> dict:
    from services.knowledge_ledger_service import _row_to_dict
    return _row_to_dict(r)


async def get_effective_entry(entity_type: str, entity_ref: str, *, as_of: datetime | None = None) -> dict | None:
    """The single knowledge_ledger_entries row (version) that was effective
    for this entity at as_of (defaults to now — "what applies now"). Returns
    None, never a fabricated/guessed entry, when no version was effective at
    that time (either nothing has ever been recorded for this entity, or
    as_of predates the earliest recorded version).

    "Mentions this entity" is a direct JSONB membership check against
    entry.entity_refs — the same field services/knowledge_context_service.py
    projects from, so this module's notion of an entity match is identical to
    that layer's, not a parallel definition."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    moment = as_of or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_ledger_entries "
            "WHERE tenant_id = $1 "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(entity_refs) er "
            "            WHERE er->>'entity_type' = $2 AND er->>'entity_id' = $3) "
            "AND effective_from <= $4 AND (effective_to IS NULL OR effective_to > $4) "
            "ORDER BY effective_from DESC LIMIT 1",
            tenant_id, entity_type, entity_ref, moment,
        )
    return _entry_row_to_dict(row) if row else None


async def get_citations_for_entry(entry_id: str) -> list[dict]:
    """Real, ledger-backed citations for one specific version — reused
    verbatim from knowledge_citations rather than a new provenance store.
    Tenant-scoped, so an entry_id from another tenant never leaks citations
    here even if guessed."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM knowledge_citations WHERE tenant_id = $1 AND ledger_entry_id = $2 "
            "ORDER BY created_at",
            tenant_id, entry_id,
        )
    from services.knowledge_context_service import _citation_row_to_dict
    return [_citation_row_to_dict(r) for r in rows]


async def compose_temporal_context(entity_type: str, entity_ref: str, *, as_of: datetime | None = None) -> dict | None:
    """The temporal counterpart to knowledge_context_service.compose_context():
    the single version effective at as_of, plus the real citations backing
    it. Returns None — never fabricated evidence — when no version was ever
    effective at that time for this tenant."""
    entry = await get_effective_entry(entity_type, entity_ref, as_of=as_of)
    if entry is None:
        return None
    citations = await get_citations_for_entry(entry["id"])
    name = entity_ref
    for ref in entry["entity_refs"]:
        if ref.get("entity_type") == entity_type and ref.get("entity_id") == entity_ref:
            name = ref.get("name", entity_ref)
            break
    return {
        "entity_type": entity_type,
        "entity_ref": entity_ref,
        "name": name,
        "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
        "entry": entry,
        "citations": citations,
    }


async def get_version_history(entity_type: str, entity_ref: str) -> list[dict]:
    """Every recorded version mentioning this entity, oldest first —
    auditability over the full timeline, not just the single effective
    version compose_temporal_context() resolves. Tenant-scoped."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM knowledge_ledger_entries "
            "WHERE tenant_id = $1 "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(entity_refs) er "
            "            WHERE er->>'entity_type' = $2 AND er->>'entity_id' = $3) "
            "ORDER BY effective_from ASC",
            tenant_id, entity_type, entity_ref,
        )
    return [_entry_row_to_dict(r) for r in rows]


async def supersede(old_entry_id: str, new_entry_id: str) -> dict | None:
    """Thin passthrough to knowledge_ledger_service.mark_superseded() — kept
    here too so temporal-specific callers don't need to import the ledger
    module directly to record a version transition."""
    from services import knowledge_ledger_service
    return await knowledge_ledger_service.mark_superseded(old_entry_id, new_entry_id)
