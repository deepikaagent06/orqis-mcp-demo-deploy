"""Knowledge Ledger foundation — an append-oriented, tenant-owned log of
knowledge facts distinct from the Knowledge Base article store
(services/knowledge_base_service.py, which holds authored documents).

Built future-ready for a graph-backed knowledge graph (currently implemented
on Graphiti — see services/knowledge_graph_service.py):
- knowledge_context_id groups entries into a shared context/session, which
  will map to a graph/group id in that graph backend.
- entity_refs stores graph entity references (entity_id/entity_type/name)
  so entries can later be projected into graph nodes without a schema
  migration.
- relationships stores directed edges between entity_refs (source/target/
  type/confidence) so entries can later be projected into graph edges.

entity_refs and relationships are stored as opaque JSONB, read/written
verbatim regardless of which graph backend (if any) is active.

graph_ingested/graph_episode_id/graph_ingested_at/graph_ingestion_status
(the external, ORQIS-owned names for this entry's underlying
graphiti_ingested/graphiti_episode_id/graphiti_ingested_at/
graphiti_ingestion_status columns — see _row_to_dict() below) track whether
this entry has been pushed through the Knowledge Graph backend's LLM entity/
relationship extraction (services/knowledge_graph_service.py) so a re-run
never pays that extraction cost twice for the same entry. Ingestion is
optional and entirely separate from the synchronous, free Knowledge Context
Layer projection below. The database columns themselves keep their original
`graphiti_*` names for now (no migration has been run); only the Python/API
surface uses the ORQIS-owned `graph_*` names.

Every entry created here is also projected into the Knowledge Context Layer
(services/knowledge_context_service.py) — normalized, deduplicated
knowledge_entities/knowledge_relationships/knowledge_citations tables that
back the generic GET /api/knowledge-context/{entity_type}/{entity_ref} API.
"Knowledge Context Layer" is this project's own name for that layer; Graphiti
remains one possible graph backend implementation for it, not something
wired into the layer itself.

Temporal Knowledge (services/knowledge_temporal_service.py) is built directly
on top of this table rather than a second knowledge base: effective_from/
effective_to/superseded_by_entry_id/superseded_at below let a caller ask "what
was true at time T" without disturbing the Knowledge Context Layer's own
current-state projection above (knowledge_entities dedupes by entity_ref, so
it inherently answers only "what's true now" — the temporal columns here are
what let knowledge_temporal_service.py reconstruct the "as of" answer from the
underlying per-version ledger entries instead). created_at remains this
entry's ingestion time; effective_from/effective_to are a separate, optional
notion of when the fact itself was/is true in the real world. Every entry is
still "current" (effective_from defaults to its own created_at, effective_to
stays NULL) unless a caller explicitly declares otherwise or supersede_entry()
below closes it out — so existing callers that never pass these fields are
unaffected.
"""
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_ledger_entries (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant',
    knowledge_context_id VARCHAR(100),
    source_type VARCHAR(50) NOT NULL DEFAULT 'manual',
    source_id VARCHAR(100),
    title VARCHAR(500) NOT NULL,
    content TEXT NOT NULL,
    entity_refs JSONB NOT NULL DEFAULT '[]',
    relationships JSONB NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 1.0,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    uploaded_by VARCHAR(200) NOT NULL DEFAULT 'system',
    projected_at TIMESTAMPTZ,
    graphiti_ingested BOOLEAN NOT NULL DEFAULT FALSE,
    graphiti_episode_id VARCHAR(200),
    graphiti_ingested_at TIMESTAMPTZ,
    graphiti_ingestion_status VARCHAR(20) NOT NULL DEFAULT 'not_ingested',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS uploaded_by VARCHAR(200) NOT NULL DEFAULT 'system';
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ;
-- Knowledge Graph ingestion tracking, so a ledger entry already extracted
-- into the graph is never re-sent through the graph backend's LLM
-- extraction pipeline again. Column names below are unchanged pending a
-- future schema migration — see _row_to_dict()'s graph_* aliasing.
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS graphiti_ingested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS graphiti_episode_id VARCHAR(200);
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS graphiti_ingested_at TIMESTAMPTZ;
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS graphiti_ingestion_status VARCHAR(20) NOT NULL DEFAULT 'not_ingested';

-- Temporal Knowledge — see module docstring. effective_from defaults to this
-- row's own created_at (backfilled below for any pre-existing rows), so
-- "current" behavior for every entry that predates this migration is
-- unchanged: always effective, never superseded.
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ;
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ;
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS superseded_by_entry_id VARCHAR(50)
    REFERENCES knowledge_ledger_entries(id) ON DELETE SET NULL;
ALTER TABLE knowledge_ledger_entries ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;
UPDATE knowledge_ledger_entries SET effective_from = created_at WHERE effective_from IS NULL;
ALTER TABLE knowledge_ledger_entries ALTER COLUMN effective_from SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kle_tenant ON knowledge_ledger_entries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kle_context ON knowledge_ledger_entries(knowledge_context_id);
CREATE INDEX IF NOT EXISTS idx_kle_temporal ON knowledge_ledger_entries(tenant_id, effective_from);
CREATE INDEX IF NOT EXISTS idx_kle_entity_refs_gin ON knowledge_ledger_entries USING GIN (entity_refs);
"""

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    _schema_ready = True
    logger.info("Knowledge Ledger schema ensured (knowledge_ledger_entries)")


def _row_to_dict(r: asyncpg.Record) -> dict:
    # Reads the (unchanged, still-named-after-the-implementation) database
    # columns but returns the ORQIS-owned graph_* names — this is the single
    # place internal callers and the API both see the renamed contract from,
    # with no schema migration required.
    return {
        "id": r["id"], "knowledge_context_id": r["knowledge_context_id"], "source_type": r["source_type"],
        "source_id": r["source_id"], "title": r["title"], "content": r["content"],
        "entity_refs": r["entity_refs"], "relationships": r["relationships"], "confidence": r["confidence"],
        "status": r["status"], "uploaded_by": r["uploaded_by"],
        "projected_at": r["projected_at"].isoformat() if r["projected_at"] else None,
        "graph_ingested": r["graphiti_ingested"], "graph_episode_id": r["graphiti_episode_id"],
        "graph_ingested_at": r["graphiti_ingested_at"].isoformat() if r["graphiti_ingested_at"] else None,
        "graph_ingestion_status": r["graphiti_ingestion_status"],
        "effective_from": r["effective_from"].isoformat() if r["effective_from"] else None,
        "effective_to": r["effective_to"].isoformat() if r["effective_to"] else None,
        "superseded_by_entry_id": r["superseded_by_entry_id"],
        "superseded_at": r["superseded_at"].isoformat() if r["superseded_at"] else None,
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


async def create_entry(
    *, title: str, content: str, knowledge_context_id: str | None = None, source_type: str = "manual",
    source_id: str | None = None, entity_refs: list[dict] | None = None, relationships: list[dict] | None = None,
    confidence: float = 1.0, uploaded_by: str = "system",
    effective_from: datetime | None = None, effective_to: datetime | None = None,
) -> dict:
    """effective_from/effective_to are optional Temporal Knowledge fields
    (see module docstring and services/knowledge_temporal_service.py).
    effective_from defaults to this entry's own created_at — i.e. "effective
    as soon as ingested" — which is exactly today's behavior for every
    existing caller that never passes these kwargs. effective_to=None means
    open-ended/current; pass an explicit datetime, or call supersede_entry()
    below once the replacement entry exists, to close it out."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    entry_id = f"kle-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO knowledge_ledger_entries "
            "(id, tenant_id, knowledge_context_id, source_type, source_id, title, content, "
            "entity_refs, relationships, confidence, status, uploaded_by, created_at, updated_at, "
            "effective_from, effective_to) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'active', $11, $12, $12, $13, $14)",
            entry_id, tenant_id, knowledge_context_id, source_type, source_id, title, content,
            entity_refs or [], relationships or [], confidence, uploaded_by, now,
            effective_from or now, effective_to,
        )
    logger.info("Knowledge Ledger entry created: id=%s context=%s uploaded_by=%s", entry_id, knowledge_context_id, uploaded_by)

    # Project into the Knowledge Context Layer (services/knowledge_context_service.py)
    # — normalizes this entry's entity_refs/relationships into knowledge_entities/
    # knowledge_relationships/knowledge_citations. Imported lazily to keep this
    # module import-independent of the context layer built on top of it.
    from services import knowledge_context_service
    await knowledge_context_service.project_ledger_entry(entry_id)

    return await get_entry(entry_id)


async def mark_projected(entry_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE knowledge_ledger_entries SET projected_at = NOW() WHERE id = $1", entry_id,
        )


async def mark_graph_ingestion(
    entry_id: str, *, status: str, episode_id: str | None = None,
) -> dict | None:
    """Record the outcome of a Knowledge Graph (Graphiti) ingestion attempt
    for one entry. status is one of 'ingested', 'failed', 'unavailable'
    (Neo4j Aura not configured/reachable). Only 'ingested' sets the
    underlying graphiti_ingested column to TRUE — callers use the aliased
    graph_ingested flag (see _row_to_dict()) to skip re-sending an entry
    through graph extraction on the next run."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE knowledge_ledger_entries SET graphiti_ingested = $2, graphiti_episode_id = $3, "
            "graphiti_ingested_at = $4, graphiti_ingestion_status = $5 WHERE id = $1",
            entry_id, status == "ingested", episode_id,
            datetime.now(timezone.utc) if status == "ingested" else None, status,
        )
    return await get_entry(entry_id)


async def mark_superseded(old_entry_id: str, new_entry_id: str) -> dict | None:
    """Closes out old_entry_id's validity window at new_entry_id's own
    effective_from (falling back to now if that entry has none), and records
    the forward link. Both ids are read via get_entry() first, so this is a
    no-op returning None for either id outside the caller's own tenant —
    there is no cross-tenant supersession. Does not delete or mutate the old
    entry's content/citations: its historical facts (and the citations that
    back them) remain fully queryable for any as_of time before this
    supersession, per services/knowledge_temporal_service.py."""
    await ensure_schema()
    old_entry = await get_entry(old_entry_id)
    new_entry = await get_entry(new_entry_id)
    if old_entry is None or new_entry is None:
        return None
    tenant_id = get_tenant_id()
    now = datetime.now(timezone.utc)
    effective_to = datetime.fromisoformat(new_entry["effective_from"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE knowledge_ledger_entries SET effective_to = $3, superseded_by_entry_id = $2, "
            "superseded_at = $4, updated_at = $4 WHERE id = $1 AND tenant_id = $5",
            old_entry_id, new_entry_id, effective_to, now, tenant_id,
        )
    logger.info("Knowledge Ledger entry superseded: id=%s -> %s", old_entry_id, new_entry_id)
    return await get_entry(old_entry_id)


async def get_entry(entry_id: str) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_ledger_entries WHERE id = $1 AND tenant_id = $2", entry_id, tenant_id
        )
    return _row_to_dict(row) if row else None


async def list_entries(*, knowledge_context_id: str | None = None, status: str | None = None) -> list[dict]:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        clauses, params = ["tenant_id = $1"], [tenant_id]
        if knowledge_context_id:
            params.append(knowledge_context_id)
            clauses.append(f"knowledge_context_id = ${len(params)}")
        if status:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        rows = await conn.fetch(
            f"SELECT * FROM knowledge_ledger_entries WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            *params,
        )
    return [_row_to_dict(r) for r in rows]


async def update_entry(entry_id: str, **fields) -> dict | None:
    await ensure_schema()
    existing = await get_entry(entry_id)
    if not existing:
        return None
    tenant_id = get_tenant_id()
    updatable = {"title", "content", "entity_refs", "relationships", "confidence", "status"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in updatable or value is None:
            continue
        params.append(value)
        sets.append(f"{key} = ${len(params)}")
    if not sets:
        return existing
    params.append(datetime.now(timezone.utc))
    sets.append(f"updated_at = ${len(params)}")
    params.extend([entry_id, tenant_id])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE knowledge_ledger_entries SET {', '.join(sets)} "
            f"WHERE id = ${len(params) - 1} AND tenant_id = ${len(params)}",
            *params,
        )
    logger.info("Knowledge Ledger entry updated: id=%s fields=%s", entry_id, list(fields.keys()))
    return await get_entry(entry_id)
