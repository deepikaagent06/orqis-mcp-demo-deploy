"""Knowledge Context Layer — this project's own name for the normalized
knowledge graph built on top of the Knowledge Ledger
(services/knowledge_ledger_service.py). The optional Knowledge Graph backend
(services/knowledge_graph_service.py, currently Graphiti/Neo4j Aura) is a
separate, additive layer; nothing here is a Knowledge Graph integration.

Three tables:
- knowledge_entities — one row per (tenant, entity_type, entity_ref), built
  by deduplicating knowledge_ledger_entries.entity_refs across every entry
  that mentions it. mention_count/first_seen_at/last_seen_at accumulate
  across projections.
- knowledge_relationships — one row per (tenant, source, target,
  relationship_type) edge between two knowledge_entities, built the same way
  from knowledge_ledger_entries.relationships.
- knowledge_citations — provenance: which Knowledge Ledger entry (and which
  uploader) backs a given entity or relationship fact.

Two operations:
- project_ledger_entry() — ledger-to-entity projection. Purely mechanical:
  it upserts exactly what a ledger entry's entity_refs/relationships already
  declare and records a citation back to that entry. No LLM calls, no
  prompt-based reasoning, no inference of new facts, no demo/seed data.
- compose_context() — context composition. Reads back one entity, its
  one-hop related entities, and the citations backing all of it. This is
  what backs GET /api/knowledge-context/{entity_type}/{entity_ref}.
"""
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_entities (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL,
    entity_type VARCHAR(100) NOT NULL,
    entity_ref VARCHAR(200) NOT NULL,
    name VARCHAR(500) NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, entity_type, entity_ref)
);
CREATE INDEX IF NOT EXISTS idx_kent_tenant_type ON knowledge_entities(tenant_id, entity_type);

CREATE TABLE IF NOT EXISTS knowledge_relationships (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL,
    source_entity_id VARCHAR(50) NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    target_entity_id VARCHAR(50) NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    relationship_type VARCHAR(100) NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    mention_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, source_entity_id, target_entity_id, relationship_type)
);
CREATE INDEX IF NOT EXISTS idx_krel_tenant_source ON knowledge_relationships(tenant_id, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_krel_tenant_target ON knowledge_relationships(tenant_id, target_entity_id);

CREATE TABLE IF NOT EXISTS knowledge_citations (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL,
    entity_id VARCHAR(50) REFERENCES knowledge_entities(id) ON DELETE CASCADE,
    relationship_id VARCHAR(50) REFERENCES knowledge_relationships(id) ON DELETE CASCADE,
    ledger_entry_id VARCHAR(50) NOT NULL REFERENCES knowledge_ledger_entries(id) ON DELETE CASCADE,
    source_type VARCHAR(50) NOT NULL,
    source_id VARCHAR(100),
    uploaded_by VARCHAR(200) NOT NULL DEFAULT 'system',
    title VARCHAR(500) NOT NULL,
    snippet TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT knowledge_citations_target_chk CHECK (entity_id IS NOT NULL OR relationship_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_kcit_tenant_entity ON knowledge_citations(tenant_id, entity_id);
CREATE INDEX IF NOT EXISTS idx_kcit_tenant_relationship ON knowledge_citations(tenant_id, relationship_id);
CREATE INDEX IF NOT EXISTS idx_kcit_ledger_entry ON knowledge_citations(ledger_entry_id);
"""

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    # knowledge_citations.ledger_entry_id references knowledge_ledger_entries,
    # so that table must exist first regardless of startup ordering.
    from services import knowledge_ledger_service
    await knowledge_ledger_service.ensure_schema()

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    _schema_ready = True
    logger.info("Knowledge Context Layer schema ensured (knowledge_entities, knowledge_relationships, knowledge_citations)")


def _entity_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"], "entity_type": r["entity_type"], "entity_ref": r["entity_ref"], "name": r["name"],
        "mention_count": r["mention_count"],
        "first_seen_at": r["first_seen_at"].isoformat(), "last_seen_at": r["last_seen_at"].isoformat(),
    }


def _citation_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"], "ledger_entry_id": r["ledger_entry_id"], "entity_id": r["entity_id"],
        "relationship_id": r["relationship_id"], "source_type": r["source_type"], "source_id": r["source_id"],
        "uploaded_by": r["uploaded_by"], "title": r["title"], "snippet": r["snippet"],
        "confidence": r["confidence"], "created_at": r["created_at"].isoformat(),
    }


async def project_ledger_entry(entry_id: str, *, force: bool = False) -> dict:
    """Ledger-to-entity projection for a single Knowledge Ledger entry.

    Upserts a knowledge_entities row for every entry.entity_refs item and a
    knowledge_relationships row for every entry.relationships item whose
    source/target were both declared in entity_refs (relationships pointing
    at an undeclared entity_id are skipped and logged, not silently
    invented). Writes a knowledge_citations row back to this entry for every
    entity/relationship it touches, carrying the entry's uploaded_by so
    provenance survives the projection. Idempotent: a previously-projected
    entry is a no-op unless force=True.
    """
    from services import knowledge_ledger_service

    await ensure_schema()
    entry = await knowledge_ledger_service.get_entry(entry_id)
    if entry is None:
        raise ValueError(f"Knowledge Ledger entry {entry_id} not found")
    if entry["projected_at"] and not force:
        return {"entry_id": entry_id, "entities": 0, "relationships": 0, "citations": 0, "skipped": True}

    tenant_id = get_tenant_id()
    now = datetime.now(timezone.utc)
    snippet = entry["content"][:500]
    pool = await get_pool()

    ref_to_id: dict[str, str] = {}
    entities_upserted = 0
    relationships_upserted = 0
    citations_created = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for ref in entry["entity_refs"]:
                entity_pk = f"kent-{uuid.uuid4().hex[:10]}"
                row = await conn.fetchrow(
                    "INSERT INTO knowledge_entities "
                    "(id, tenant_id, entity_type, entity_ref, name, mention_count, first_seen_at, last_seen_at, "
                    "created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, 1, $6, $6, $6, $6) "
                    "ON CONFLICT (tenant_id, entity_type, entity_ref) DO UPDATE SET "
                    "name = EXCLUDED.name, mention_count = knowledge_entities.mention_count + 1, "
                    "last_seen_at = EXCLUDED.last_seen_at, updated_at = EXCLUDED.updated_at "
                    "RETURNING id",
                    entity_pk, tenant_id, ref["entity_type"], ref["entity_id"], ref["name"], now,
                )
                internal_id = row["id"]
                ref_to_id[ref["entity_id"]] = internal_id
                entities_upserted += 1

                await conn.execute(
                    "INSERT INTO knowledge_citations "
                    "(id, tenant_id, entity_id, relationship_id, ledger_entry_id, source_type, source_id, "
                    "uploaded_by, title, snippet, confidence, created_at) "
                    "VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, $11)",
                    f"kcit-{uuid.uuid4().hex[:10]}", tenant_id, internal_id, entry_id, entry["source_type"],
                    entry["source_id"], entry["uploaded_by"], entry["title"], snippet, entry["confidence"], now,
                )
                citations_created += 1

            for rel in entry["relationships"]:
                source_id = ref_to_id.get(rel["source_entity_id"])
                target_id = ref_to_id.get(rel["target_entity_id"])
                if not source_id or not target_id:
                    logger.warning(
                        "Skipping relationship projection for entry=%s: %s -> %s not declared in entity_refs",
                        entry_id, rel["source_entity_id"], rel["target_entity_id"],
                    )
                    continue
                relationship_pk = f"krel-{uuid.uuid4().hex[:10]}"
                row = await conn.fetchrow(
                    "INSERT INTO knowledge_relationships "
                    "(id, tenant_id, source_entity_id, target_entity_id, relationship_type, confidence, "
                    "mention_count, first_seen_at, last_seen_at, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 1, $7, $7, $7, $7) "
                    "ON CONFLICT (tenant_id, source_entity_id, target_entity_id, relationship_type) DO UPDATE SET "
                    "confidence = EXCLUDED.confidence, mention_count = knowledge_relationships.mention_count + 1, "
                    "last_seen_at = EXCLUDED.last_seen_at, updated_at = EXCLUDED.updated_at "
                    "RETURNING id",
                    relationship_pk, tenant_id, source_id, target_id, rel["relationship_type"], rel["confidence"], now,
                )
                internal_rel_id = row["id"]
                relationships_upserted += 1

                await conn.execute(
                    "INSERT INTO knowledge_citations "
                    "(id, tenant_id, entity_id, relationship_id, ledger_entry_id, source_type, source_id, "
                    "uploaded_by, title, snippet, confidence, created_at) "
                    "VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    f"kcit-{uuid.uuid4().hex[:10]}", tenant_id, internal_rel_id, entry_id, entry["source_type"],
                    entry["source_id"], entry["uploaded_by"], entry["title"], snippet, rel["confidence"], now,
                )
                citations_created += 1

            await conn.execute(
                "UPDATE knowledge_ledger_entries SET projected_at = $2 WHERE id = $1", entry_id, now,
            )

    logger.info(
        "Projected ledger entry %s into Knowledge Context Layer: entities=%d relationships=%d citations=%d",
        entry_id, entities_upserted, relationships_upserted, citations_created,
    )
    return {
        "entry_id": entry_id, "entities": entities_upserted, "relationships": relationships_upserted,
        "citations": citations_created, "skipped": False,
    }


async def compose_context(entity_type: str, entity_ref: str) -> dict | None:
    """Context composition: read back one entity, its one-hop related
    entities (both directions), and every citation backing the entity or
    one of those relationships. Pure aggregation over already-projected
    rows — no synthesis, no LLM calls."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        entity_row = await conn.fetchrow(
            "SELECT * FROM knowledge_entities WHERE tenant_id = $1 AND entity_type = $2 AND entity_ref = $3",
            tenant_id, entity_type, entity_ref,
        )
        if not entity_row:
            return None
        entity_id = entity_row["id"]

        relationship_rows = await conn.fetch(
            "SELECT r.id AS relationship_id, r.relationship_type, r.confidence, "
            "r.source_entity_id, r.target_entity_id, "
            "oe.id AS other_id, oe.entity_type AS other_entity_type, oe.entity_ref AS other_entity_ref, "
            "oe.name AS other_name, oe.mention_count AS other_mention_count, "
            "oe.first_seen_at AS other_first_seen_at, oe.last_seen_at AS other_last_seen_at "
            "FROM knowledge_relationships r "
            "JOIN knowledge_entities oe "
            "  ON oe.id = (CASE WHEN r.source_entity_id = $2 THEN r.target_entity_id ELSE r.source_entity_id END) "
            "WHERE r.tenant_id = $1 AND (r.source_entity_id = $2 OR r.target_entity_id = $2) "
            "ORDER BY r.last_seen_at DESC",
            tenant_id, entity_id,
        )

        relationship_ids = [row["relationship_id"] for row in relationship_rows]
        citation_rows = await conn.fetch(
            "SELECT * FROM knowledge_citations WHERE tenant_id = $1 "
            "AND (entity_id = $2 OR relationship_id = ANY($3::varchar[])) "
            "ORDER BY created_at DESC",
            tenant_id, entity_id, relationship_ids,
        )

    related_entities = []
    for row in relationship_rows:
        direction = "outbound" if row["source_entity_id"] == entity_id else "inbound"
        related_entities.append({
            "relationship_id": row["relationship_id"], "relationship_type": row["relationship_type"],
            "direction": direction, "confidence": row["confidence"],
            "entity": {
                "id": row["other_id"], "entity_type": row["other_entity_type"],
                "entity_ref": row["other_entity_ref"], "name": row["other_name"],
                "mention_count": row["other_mention_count"],
                "first_seen_at": row["other_first_seen_at"].isoformat(),
                "last_seen_at": row["other_last_seen_at"].isoformat(),
            },
        })

    return {
        "entity_type": entity_type, "entity_ref": entity_ref,
        "entity": _entity_row_to_dict(entity_row),
        "related_entities": related_entities,
        "citations": [_citation_row_to_dict(r) for r in citation_rows],
    }


async def most_recent_entity_ref(entity_type: str) -> str | None:
    """The most-recently-seen entity_ref for a given entity_type, tenant-
    scoped. Lets a caller with no specific entity_ref to look up (services/
    agent_context_resolver.py's resolve_generic_knowledge_context(), used by
    pipeline_executor.py's generic Citation/Evidence Gate) pick one real,
    citation-backed entity to compose_context() against — summarize_entity_types()
    above answers "does this entity_type exist at all" but never returns an
    entity_ref, since compose_context() is the only read that returns actual
    citation rows."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT entity_ref FROM knowledge_entities WHERE tenant_id = $1 AND entity_type = $2 "
            "ORDER BY last_seen_at DESC LIMIT 1",
            tenant_id, entity_type,
        )
    return row["entity_ref"] if row else None


async def summarize_entity_types(entity_types: list[str]) -> dict[str, dict]:
    """Batched existence/volume summary for a set of entity_types — answers
    "is there any projected knowledge for this type at all" without needing
    a specific entity_ref, unlike compose_context(). Used to summarize the
    knowledge sources available to an agent from its declarative
    knowledge_requirements. Keyed by entity_type; a type with no rows is
    simply absent from the result."""
    if not entity_types:
        return {}
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT entity_type, COUNT(*) AS entity_count, MAX(last_seen_at) AS last_seen_at "
            "FROM knowledge_entities WHERE tenant_id = $1 AND entity_type = ANY($2::varchar[]) "
            "GROUP BY entity_type",
            tenant_id, entity_types,
        )
    return {
        row["entity_type"]: {
            "entity_count": row["entity_count"],
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        }
        for row in rows
    }


async def count_totals() -> dict:
    """Tenant-wide row counts across all three Knowledge Context Layer
    tables — for platform-level summaries (e.g. the Runtime Dashboard) that
    need a total rather than a per-entity_type breakdown."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        entities = await conn.fetchval("SELECT COUNT(*) FROM knowledge_entities WHERE tenant_id = $1", tenant_id)
        relationships = await conn.fetchval("SELECT COUNT(*) FROM knowledge_relationships WHERE tenant_id = $1", tenant_id)
        citations = await conn.fetchval("SELECT COUNT(*) FROM knowledge_citations WHERE tenant_id = $1", tenant_id)
    return {"entities": int(entities or 0), "relationships": int(relationships or 0), "citations": int(citations or 0)}
