"""Optional Agent Memory — "what does this particular agent retain from
previous executions that may help it perform future work," distinct from
governed enterprise Knowledge (services/knowledge_context_service.py,
services/knowledge_temporal_service.py). No memory infrastructure existed in
this codebase before this module (no episodic/semantic/conversation store,
no `memory` table) — this is the smallest useful implementation, not a port
of another framework's memory taxonomy.

Enablement: purely AgentDefinition.memory_enabled (models/agent_definition.py,
plumbed through services/agent_definition_service.py) — a plain boolean,
defaulting to False for every existing and new AgentDefinition. Nothing in
this module enables memory for an agent on its own; services/
agent_runtime_executor.py.execute()/_finalize() are the only callers, and
both check definition["memory_enabled"] before touching this module at all
(see that file). This is what makes memory disabled == memory_enabled is
False mean "no retrieval, no write" — not merely "no write."

Memory types — kept to the smallest meaningful distinction rather than an
enumerated taxonomy:
- episodic: a short, auto-generated summary of one completed Agent Runtime
  execution ("previous agent experiences/executions"). Written automatically
  by agent_runtime_executor._finalize() — and ONLY there, i.e. only once an
  execution has actually finalized as a governed decision (never for a
  paused-for-review, blocked, or failed run; never per LLM token or retry
  attempt). This is the module's sole automatic write trigger, deliberately
  narrow so memory can never become an uncontrolled write sink: at most one
  row per finalized execution.
- semantic: a durable fact/preference, written only via write_semantic_memory()
  below — an explicit call, never inferred from free-text LLM output. ORQIS's
  Agent Runtime makes a single, non-tool-calling llm.chat() completion per
  execution (services/llm_service.py) with no structured-output parsing or
  agent-initiated tool loop, so there is currently no execution path that can
  safely originate a semantic write on an agent's own initiative — inventing
  one (e.g. regex-parsing the LLM's free text for "durable facts") risks
  treating hallucinated or ambiguous text as durable memory, exactly what
  MEMORY SAFETY below forbids. write_semantic_memory() is implemented and
  fully governed/isolated like every other function here, ready for a future,
  explicitly-triggered caller — see the FINAL REPORT's "intentionally
  deferred" section for this limitation, not silently glossed over.

MEMORY SAFETY: an agent_memory_entries row is never treated as, or promoted
into, governed enterprise Knowledge. Nothing in this module writes to
knowledge_ledger_entries/knowledge_entities/knowledge_citations, and nothing
in services/agent_runtime_executor.py folds a memory entry into
step_result["citations"] or a DecisionRecord's evidence list — those remain
sourced exclusively from real, ledger-backed citations (see that module's
_finalize()). Memory retrieved here is injected into the LLM system prompt as
plainly-labeled agent context, not evidence.

Tenant isolation: every read and write below is scoped by tenant_id
(middleware.tenant_context.get_tenant_id(), the same convention every other
service in this codebase uses) — a tenant never sees another tenant's
memory, even for the same AgentDefinition id string. This is unconditional
and independent of scope below.

Ownership/scope — ORQIS's memory architecture is Shared Memory (one
tenant-owned store), not an independent private store per agent: an
AgentDefinition never gets its own memory "universe." What an authorized
agent may retrieve is governed by an explicit `scope` on each entry, not by
a hard per-agent partition of the table itself:
- AGENT (the default for every write path in this module, preserving prior
  behavior exactly): visible only when queried for the same
  agent_definition_id that wrote it — this agent's own private context.
- SHARED: visible to any memory_enabled AgentDefinition in the same tenant,
  regardless of which agent originally wrote it — the actual cross-agent
  "Agent A can retrieve what Agent B wrote" path. An entry only becomes
  SHARED via an explicit, caller-chosen scope="shared" on
  write_semantic_memory() below — never automatically, and never for the
  automatic episodic write (record_episodic_memory() is always AGENT-scoped;
  see its docstring). Choosing to write as SHARED is itself the
  authorization act — there is no separate ACL table, per the sprint's
  instruction not to invent scopes/authorization machinery beyond what the
  architecture actually calls for.

retrieve_context() below is what the Agent Runtime actually injects into a
prompt, and reads both this agent's own AGENT-scoped entries and the
tenant's SHARED entries. list_memory() (used directly by tests and any
caller wanting one specific agent's own entries) stays scoped to entries
written under that agent_definition_id, matching its pre-existing contract.

Retention: NOT implemented — no automatic deletion, no TTL, no row cap. This
is a conservative, deliberately minimal choice per the sprint's own
instruction not to invent a retention policy the architecture doesn't
already specify; ORQIS has no existing retention policy for any table this
module could model itself after. Unbounded growth is bounded only at read
time — list_memory()/retrieve_context() below always cap what they return —
so a long-lived agent's memory table can grow indefinitely without affecting
prompt size. This is called out explicitly in the FINAL REPORT as an
unresolved, intentionally deferred concern, not a silent gap.
"""
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)

EPISODIC = "episodic"
SEMANTIC = "semantic"

# Ownership/scope — see module docstring's "Ownership/scope" section. AGENT
# is the default for every write path, so a plain write_semantic_memory()/
# record_episodic_memory() call behaves exactly as it did before SHARED
# existed. SHARED requires an explicit, caller-chosen opt-in.
AGENT = "agent"
SHARED = "shared"

# Default bound on how many memory entries are ever injected into a single
# Agent Runtime execution's system prompt — independent of how many rows
# actually exist for the agent (retention is unbounded; retrieval is not).
_DEFAULT_RETRIEVAL_LIMIT = 5

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_memory_entries (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL,
    agent_definition_id VARCHAR(50) NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    memory_type VARCHAR(20) NOT NULL DEFAULT 'episodic',
    content TEXT NOT NULL,
    run_id VARCHAR(100),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_by VARCHAR(200) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_mem_tenant_agent ON agent_memory_entries(tenant_id, agent_definition_id, created_at DESC);

-- Shared Memory (see module docstring's "Ownership/scope"): 'agent' for
-- every pre-existing row (backfilled below), preserving the prior
-- per-AgentDefinition-only retrieval behavior exactly. 'shared' is opt-in
-- per entry via write_semantic_memory(scope="shared").
ALTER TABLE agent_memory_entries ADD COLUMN IF NOT EXISTS scope VARCHAR(20) NOT NULL DEFAULT 'agent';
UPDATE agent_memory_entries SET scope = 'agent' WHERE scope IS NULL;
CREATE INDEX IF NOT EXISTS idx_agent_mem_tenant_shared ON agent_memory_entries(tenant_id, scope, created_at DESC)
    WHERE scope = 'shared';
"""

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    # agent_memory_entries.agent_definition_id references agent_definitions,
    # so that table must exist first regardless of startup ordering.
    from services import agent_definition_service
    await agent_definition_service.ensure_schema()

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    _schema_ready = True
    logger.info("Agent Memory schema ensured (agent_memory_entries)")


def _row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"], "agent_definition_id": r["agent_definition_id"], "memory_type": r["memory_type"],
        "scope": r["scope"], "content": r["content"], "run_id": r["run_id"], "metadata": dict(r["metadata"]),
        "created_by": r["created_by"], "created_at": r["created_at"].isoformat(),
    }


async def _write(
    *, agent_definition_id: str, memory_type: str, scope: str, content: str, run_id: str | None,
    metadata: dict | None, created_by: str,
) -> dict:
    await ensure_schema()
    tenant_id = get_tenant_id()
    entry_id = f"amem-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO agent_memory_entries "
            "(id, tenant_id, agent_definition_id, memory_type, scope, content, run_id, metadata, created_by, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
            entry_id, tenant_id, agent_definition_id, memory_type, scope, content, run_id, metadata or {}, created_by, now,
        )
    logger.info(
        "Agent memory written: id=%s agent=%s type=%s scope=%s run=%s",
        entry_id, agent_definition_id, memory_type, scope, run_id,
    )
    return _row_to_dict(row)


async def record_episodic_memory(
    agent_definition_id: str, *, run_id: str, content: str, metadata: dict | None = None, created_by: str = "system",
) -> dict:
    """The sole automatic-write path — called only from
    services/agent_runtime_executor.py._finalize(), only after an execution
    has actually finalized as a governed decision. See module docstring for
    why this is the only unconditional trigger. Always AGENT-scoped — an
    automatic write is never the caller's deliberate choice to share it, so
    it must never become tenant-wide SHARED memory on its own."""
    return await _write(
        agent_definition_id=agent_definition_id, memory_type=EPISODIC, scope=AGENT, content=content, run_id=run_id,
        metadata=metadata, created_by=created_by,
    )


async def write_semantic_memory(
    agent_definition_id: str, *, content: str, scope: str = AGENT, metadata: dict | None = None,
    created_by: str = "system",
) -> dict:
    """Explicit write for a durable fact/preference. No current Agent Runtime
    execution path calls this automatically — see module docstring's
    'semantic' explanation for why. Exists so a future explicitly-triggered
    caller (or a test) has a governed, isolated place to write one, without
    needing a second memory store.

    scope=AGENT (default) preserves the pre-existing, per-agent-only
    contract. scope=SHARED is the explicit opt-in that makes this entry
    retrievable by any memory_enabled AgentDefinition in the same tenant via
    retrieve_context() below — see module docstring's "Ownership/scope"."""
    if scope not in (AGENT, SHARED):
        raise ValueError(f"Invalid memory scope: {scope!r} (must be {AGENT!r} or {SHARED!r})")
    return await _write(
        agent_definition_id=agent_definition_id, memory_type=SEMANTIC, scope=scope, content=content, run_id=None,
        metadata=metadata, created_by=created_by,
    )


async def list_memory(agent_definition_id: str, *, memory_type: str | None = None, limit: int = 20) -> list[dict]:
    """Most-recent-first, tenant- and agent-scoped — every entry written
    under this specific agent_definition_id, regardless of its scope (an
    agent can always see its own writes, SHARED or not). Does NOT include
    SHARED entries written by other agents — see retrieve_context() below for
    the cross-agent view. `limit` bounds how much memory is ever read back in
    one call — see module docstring's Retention note for why this is a
    read-time bound, not a deletion policy."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if memory_type:
            rows = await conn.fetch(
                "SELECT * FROM agent_memory_entries WHERE tenant_id = $1 AND agent_definition_id = $2 "
                "AND memory_type = $3 ORDER BY created_at DESC LIMIT $4",
                tenant_id, agent_definition_id, memory_type, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM agent_memory_entries WHERE tenant_id = $1 AND agent_definition_id = $2 "
                "ORDER BY created_at DESC LIMIT $3",
                tenant_id, agent_definition_id, limit,
            )
    return [_row_to_dict(r) for r in rows]


async def list_shared_memory(*, memory_type: str | None = None, limit: int = 20) -> list[dict]:
    """Tenant-scoped, cross-agent SHARED memory — every entry any agent in
    the caller's tenant has explicitly written with scope=SHARED, regardless
    of which AgentDefinition wrote it. This is the actual "Shared Memory"
    read path the ORQIS memory architecture calls for; list_memory() above
    intentionally does not merge this in, so a caller asking for one agent's
    own memory still gets exactly that."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if memory_type:
            rows = await conn.fetch(
                "SELECT * FROM agent_memory_entries WHERE tenant_id = $1 AND scope = $2 "
                "AND memory_type = $3 ORDER BY created_at DESC LIMIT $4",
                tenant_id, SHARED, memory_type, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM agent_memory_entries WHERE tenant_id = $1 AND scope = $2 "
                "ORDER BY created_at DESC LIMIT $3",
                tenant_id, SHARED, limit,
            )
    return [_row_to_dict(r) for r in rows]


async def retrieve_context(agent_definition_id: str, *, limit: int = _DEFAULT_RETRIEVAL_LIMIT) -> list[dict]:
    """What services/agent_runtime_executor.py.execute() injects into an
    execution's system prompt when the AgentDefinition has memory_enabled —
    this agent's own AGENT-scoped entries plus the tenant's SHARED entries
    (from any agent), merged newest-first and capped at `limit`. This is the
    actual authorized cross-agent retrieval path — see module docstring's
    "Ownership/scope" for why AGENT stays the default and SHARED is opt-in
    per entry, not a blanket "every agent sees everything" policy."""
    own, shared = await list_memory(agent_definition_id, limit=limit), await list_shared_memory(limit=limit)
    merged = {e["id"]: e for e in own}
    for e in shared:
        merged.setdefault(e["id"], e)
    ordered = sorted(merged.values(), key=lambda e: e["created_at"], reverse=True)
    return ordered[:limit]
