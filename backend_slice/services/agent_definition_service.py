"""AgentDefinition persistence — the design-time description of an agent
(name, instructions, default model/budget, human-review posture, and its
relationships to capabilities/use-cases/skills), kept entirely separate from
the existing runtime models (models/agent.py Agent/AgentRun) and from
services/agent_runtime_service.py, which derive live "AI agent" activity
from real workflow execution. get_execution_history() below reads that same
run data for display — it does not own or duplicate it. Since
services/agent_runtime_executor.py now persists each AgentDefinition
execution as a capability-tagged workflow_runs row (see that module's
_persist_run()), a definition's own runtime answers appear here too, indexed
by the same capability_ids this module already stores — no separate
AgentDefinition-run table or new coupling was added to make that work.

Relationships (capability_ids, use_case_ids, skill_ids) are stored as plain
id lists in join tables without hard foreign keys into capabilities/
workspaces/skills — consistent with how services/knowledge_base_service.py
references workspace_ids as a JSONB list rather than a constrained FK,
since those catalogs are owned by separate service modules.

Versioning: every create/update writes a full JSON snapshot of the
definition to agent_definition_versions, so history survives edits. Updates
never mutate a prior version row.
"""
import logging
import re
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import MARKETPLACE_TENANT_ID, get_tenant_id
from services import agent_runtime_service
from services.capability_catalog import get_capability as get_static_capability

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_definitions (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant',
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(200) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    default_model VARCHAR(100),
    default_token_budget INTEGER NOT NULL DEFAULT 4000,
    instructions TEXT NOT NULL DEFAULT '',
    inputs JSONB NOT NULL DEFAULT '[]',
    outputs JSONB NOT NULL DEFAULT '[]',
    knowledge_requirements JSONB NOT NULL DEFAULT '[]',
    human_review_config JSONB NOT NULL DEFAULT '{}',
    environment_id VARCHAR(50),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_by VARCHAR(200) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS inputs JSONB NOT NULL DEFAULT '[]';
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS outputs JSONB NOT NULL DEFAULT '[]';
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS knowledge_requirements JSONB NOT NULL DEFAULT '[]';

-- Per-agent LLM/model selection: default_model used to be NOT NULL DEFAULT
-- 'gpt-4o', which every AgentDefinition (including seed data) always
-- carried whether or not anyone actually chose a model — and the runtime
-- ignored it anyway, always calling the globally configured OPENAI_MODEL.
-- Now that services/agent_runtime_executor.py actually honors this column,
-- NULL is what distinguishes "no explicit per-agent override" (inherit the
-- global default) from a deliberate per-agent choice. Idempotent on an
-- already-nullable column, so this runs safely on every ensure_schema()
-- call — no manual data migration of existing rows is required.
ALTER TABLE agent_definitions ALTER COLUMN default_model DROP NOT NULL;
ALTER TABLE agent_definitions ALTER COLUMN default_model DROP DEFAULT;

-- Agent Organization Model (P1.1): who this agent reports to, within the
-- same AgentDefinition table. NULL means top-level — the CXO Command Agent
-- (which is not itself an AgentDefinition row; see cxo_command_service.py)
-- treats every top-level agent as one of its own direct reports. The FK
-- guarantees the id exists; tenant/workspace/cycle safety is enforced in
-- application code (_validate_reports_to below) since a same-table FK can't
-- express "must be in the same tenant".
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS reports_to_agent_id VARCHAR(50)
    REFERENCES agent_definitions(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_agent_def_reports_to ON agent_definitions(reports_to_agent_id);

-- Scheduler (Part 3) / Heartbeat (Part 4): periodic-execution configuration
-- and runtime claim state, reusing this same row rather than a new table —
-- see services/agent_scheduler_service.py and services/agent_heartbeat_service.py
-- for how these JSONB columns are atomically claimed/released, and
-- models/agent_definition.py's ScheduleConfig/HeartbeatConfig for the shape.
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS schedule_config JSONB NOT NULL DEFAULT '{"enabled": false}'::jsonb;
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS heartbeat_config JSONB NOT NULL DEFAULT '{"enabled": false}'::jsonb;

-- Optional Agent Memory (services/agent_memory_service.py): FALSE for every
-- existing row (including seed data) until an operator explicitly opts an
-- AgentDefinition in — memory is never enabled automatically. A plain
-- boolean, not a JSONB config, since there is no per-agent memory
-- configuration beyond on/off in this chunk.
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Optional Temporal Knowledge (services/knowledge_temporal_service.py):
-- FALSE for every existing row until an operator explicitly opts an
-- AgentDefinition in — independent of memory_enabled, same
-- never-automatic-default convention. See models/agent_definition.py's
-- AgentDefinitionResponse.temporal_knowledge_enabled docstring.
ALTER TABLE agent_definitions ADD COLUMN IF NOT EXISTS temporal_knowledge_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS agent_definition_capabilities (
    agent_definition_id VARCHAR(50) NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    capability_id VARCHAR(50) NOT NULL,
    PRIMARY KEY (agent_definition_id, capability_id)
);

CREATE TABLE IF NOT EXISTS agent_definition_use_cases (
    agent_definition_id VARCHAR(50) NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    workspace_id VARCHAR(50) NOT NULL,
    PRIMARY KEY (agent_definition_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS agent_definition_skills (
    agent_definition_id VARCHAR(50) NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    skill_id VARCHAR(50) NOT NULL,
    PRIMARY KEY (agent_definition_id, skill_id)
);

CREATE TABLE IF NOT EXISTS agent_definition_versions (
    id VARCHAR(50) PRIMARY KEY,
    agent_definition_id VARCHAR(50) NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot JSONB NOT NULL,
    change_note TEXT,
    created_by VARCHAR(200) NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_definition_id, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_def_tenant ON agent_definitions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_def_status ON agent_definitions(status);
CREATE INDEX IF NOT EXISTS idx_agent_def_versions_def ON agent_definition_versions(agent_definition_id);
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
    logger.info(
        "AgentDefinition schema ensured (agent_definitions, agent_definition_capabilities, "
        "agent_definition_use_cases, agent_definition_skills, agent_definition_versions)"
    )


class InvalidHierarchyError(ValueError):
    """Raised when a reports_to_agent_id assignment would violate tenant
    isolation, workspace isolation, or would create/extend a self- or
    circular-reporting relationship."""


# Full-object defaults for a brand-new AgentDefinition's schedule_config/
# heartbeat_config — mirrors models/agent_definition.py's
# ScheduleConfig/HeartbeatConfig pydantic defaults so a direct service-level
# caller (tests, scripts) that omits these kwargs gets the exact same shape
# routers/agent_definitions.py's create endpoint would produce.
_DEFAULT_SCHEDULE_CONFIG = {
    "enabled": False, "interval_seconds": 3600, "allow_overlap": False, "prompt": None,
    "next_run_at": None, "running": False, "running_since": None, "last_run_id": None,
    "last_dispatched_at": None,
}
_DEFAULT_HEARTBEAT_CONFIG = {
    "enabled": False, "interval_seconds": 300, "allow_overlap": False, "probe_question": None,
    "next_check_at": None, "running": False, "running_since": None, "last_run_id": None,
    "last_dispatched_at": None,
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def _validate_reports_to(
    conn: asyncpg.Connection, *, tenant_id: str, definition_id: str | None,
    reports_to_agent_id: str, use_case_ids: list[str] | None,
) -> None:
    """Validates a prospective reports_to_agent_id before it is written.
    `definition_id` is None when validating a brand-new AgentDefinition
    (which cannot yet appear as anyone's parent, so cycle checks only need
    to walk upward from the proposed parent)."""
    if definition_id is not None and reports_to_agent_id == definition_id:
        raise InvalidHierarchyError("An AgentDefinition cannot report to itself")

    parent = await conn.fetchrow(
        "SELECT id, reports_to_agent_id FROM agent_definitions WHERE id = $1 AND tenant_id = $2",
        reports_to_agent_id, tenant_id,
    )
    if parent is None:
        raise InvalidHierarchyError(
            f"reports_to_agent_id '{reports_to_agent_id}' does not reference an existing "
            "AgentDefinition in this tenant"
        )

    if use_case_ids:
        parent_ws_rows = await conn.fetch(
            "SELECT workspace_id FROM agent_definition_use_cases WHERE agent_definition_id = $1",
            reports_to_agent_id,
        )
        parent_workspaces = {r["workspace_id"] for r in parent_ws_rows}
        if parent_workspaces and parent_workspaces.isdisjoint(use_case_ids):
            raise InvalidHierarchyError(
                "reports_to_agent_id belongs to a disjoint set of workspaces — cross-workspace "
                "reporting relationships are not allowed"
            )

    # Walk the proposed parent chain upward to reject a cycle. Bounded so a
    # pre-existing, unrelated cycle (which shouldn't be possible given this
    # same check runs on every write, but isn't worth trusting blindly)
    # can't hang this call.
    visited = {reports_to_agent_id}
    current = parent
    for _ in range(100):
        next_parent_id = current["reports_to_agent_id"]
        if next_parent_id is None:
            break
        if definition_id is not None and next_parent_id == definition_id:
            raise InvalidHierarchyError("This assignment would create a circular reporting relationship")
        if next_parent_id in visited:
            break
        visited.add(next_parent_id)
        current = await conn.fetchrow(
            "SELECT id, reports_to_agent_id FROM agent_definitions WHERE id = $1 AND tenant_id = $2",
            next_parent_id, tenant_id,
        )
        if current is None:
            break


async def _row_to_dict(conn: asyncpg.Connection, r: asyncpg.Record) -> dict:
    cap_rows = await conn.fetch(
        "SELECT capability_id FROM agent_definition_capabilities WHERE agent_definition_id = $1 ORDER BY capability_id",
        r["id"],
    )
    use_case_rows = await conn.fetch(
        "SELECT workspace_id FROM agent_definition_use_cases WHERE agent_definition_id = $1 ORDER BY workspace_id",
        r["id"],
    )
    skill_rows = await conn.fetch(
        "SELECT skill_id FROM agent_definition_skills WHERE agent_definition_id = $1 ORDER BY skill_id",
        r["id"],
    )
    return {
        "id": r["id"], "name": r["name"], "slug": r["slug"], "description": r["description"],
        "status": r["status"], "default_model": r["default_model"], "default_token_budget": r["default_token_budget"],
        "instructions": r["instructions"], "inputs": r["inputs"], "outputs": r["outputs"],
        "knowledge_requirements": r["knowledge_requirements"], "human_review_config": r["human_review_config"],
        "environment_id": r["environment_id"], "reports_to_agent_id": r["reports_to_agent_id"],
        "schedule_config": r["schedule_config"], "heartbeat_config": r["heartbeat_config"],
        "memory_enabled": r["memory_enabled"],
        "temporal_knowledge_enabled": r["temporal_knowledge_enabled"],
        "current_version": r["current_version"],
        "created_by": r["created_by"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
        "capability_ids": [row["capability_id"] for row in cap_rows],
        "use_case_ids": [row["workspace_id"] for row in use_case_rows],
        "skill_ids": [row["skill_id"] for row in skill_rows],
    }


def _snapshot_of(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "current_version"}


async def _write_version(conn: asyncpg.Connection, *, agent_definition_id: str, version: int, snapshot: dict,
                          change_note: str | None, created_by: str) -> None:
    await conn.execute(
        "INSERT INTO agent_definition_versions (id, agent_definition_id, version, snapshot, change_note, created_by, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        f"adv-{uuid.uuid4().hex[:10]}", agent_definition_id, version, snapshot, change_note, created_by,
        datetime.now(timezone.utc),
    )


async def create_definition(
    *, name: str, description: str = "", default_model: str | None = None, default_token_budget: int = 4000,
    instructions: str = "", capability_ids: list[str] | None = None, use_case_ids: list[str] | None = None,
    skill_ids: list[str] | None = None, inputs: list[dict] | None = None, outputs: list[dict] | None = None,
    knowledge_requirements: list[dict] | None = None, human_review_config: dict | None = None,
    environment_id: str | None = None, reports_to_agent_id: str | None = None,
    schedule_config: dict | None = None, heartbeat_config: dict | None = None,
    memory_enabled: bool = False, temporal_knowledge_enabled: bool = False, created_by: str = "system",
) -> dict:
    await ensure_schema()
    tenant_id = get_tenant_id()
    definition_id = f"agentdef-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    capability_ids = capability_ids or []
    use_case_ids = use_case_ids or []
    skill_ids = skill_ids or []
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if reports_to_agent_id is not None:
                await _validate_reports_to(
                    conn, tenant_id=tenant_id, definition_id=None,
                    reports_to_agent_id=reports_to_agent_id, use_case_ids=use_case_ids,
                )
            await conn.execute(
                "INSERT INTO agent_definitions "
                "(id, tenant_id, name, slug, description, status, default_model, default_token_budget, "
                "instructions, inputs, outputs, knowledge_requirements, human_review_config, environment_id, "
                "reports_to_agent_id, current_version, created_by, created_at, updated_at, schedule_config, "
                "heartbeat_config, memory_enabled, temporal_knowledge_enabled) "
                "VALUES ($1, $2, $3, $4, $5, 'draft', $6, $7, $8, $9, $10, $11, $12, $13, $14, 1, $15, $16, $16, "
                "$17, $18, $19, $20)",
                definition_id, tenant_id, name, _slugify(name), description, default_model, default_token_budget,
                instructions, inputs or [], outputs or [], knowledge_requirements or [],
                human_review_config or {}, environment_id, reports_to_agent_id, created_by, now,
                schedule_config or dict(_DEFAULT_SCHEDULE_CONFIG), heartbeat_config or dict(_DEFAULT_HEARTBEAT_CONFIG),
                memory_enabled, temporal_knowledge_enabled,
            )
            for cap_id in capability_ids:
                await conn.execute(
                    "INSERT INTO agent_definition_capabilities (agent_definition_id, capability_id) VALUES ($1, $2)",
                    definition_id, cap_id,
                )
            for ws_id in use_case_ids:
                await conn.execute(
                    "INSERT INTO agent_definition_use_cases (agent_definition_id, workspace_id) VALUES ($1, $2)",
                    definition_id, ws_id,
                )
            for skill_id in skill_ids:
                await conn.execute(
                    "INSERT INTO agent_definition_skills (agent_definition_id, skill_id) VALUES ($1, $2)",
                    definition_id, skill_id,
                )
            row = await conn.fetchrow("SELECT * FROM agent_definitions WHERE id = $1", definition_id)
            snapshot_dict = await _row_to_dict(conn, row)
            await _write_version(
                conn, agent_definition_id=definition_id, version=1, snapshot=_snapshot_of(snapshot_dict),
                change_note="Initial version", created_by=created_by,
            )
    logger.info("AgentDefinition created: id=%s name=%s", definition_id, name)
    return snapshot_dict


async def get_definition(definition_id: str) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_definitions WHERE id = $1 AND tenant_id = $2", definition_id, tenant_id
        )
        if not row:
            return None
        return await _row_to_dict(conn, row)


async def list_definitions(
    *, status: str | None = None, include_marketplace: bool = False,
    reports_to_agent_id: str | None = None, top_level_only: bool = False,
) -> list[dict]:
    """List the caller's own AgentDefinitions. With include_marketplace=True
    (used only by the Marketplace Components listing), also includes the
    shared Marketplace Registry (see MARKETPLACE_TENANT_ID) — read-only, since
    get_definition()/update_definition() stay strictly scoped to the caller's
    own tenant and never grant write access to registry content. Defaults to
    False so existing tenant-scoped consumers (Runtime Dashboard, Agent
    Workspace, Knowledge Graph) are unaffected.

    top_level_only=True filters to agents with no reports_to_agent_id — this
    is also how the CXO Command Agent (not itself an AgentDefinition row)
    discovers its own direct reports; see get_direct_reports(None) below."""
    await ensure_schema()
    tenant_scope = [get_tenant_id()]
    if include_marketplace:
        tenant_scope.append(MARKETPLACE_TENANT_ID)
    where = ["tenant_id = ANY($1::text[])"]
    params: list = [tenant_scope]
    if status:
        params.append(status)
        where.append(f"status = ${len(params)}")
    if top_level_only:
        where.append("reports_to_agent_id IS NULL")
    elif reports_to_agent_id is not None:
        params.append(reports_to_agent_id)
        where.append(f"reports_to_agent_id = ${len(params)}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM agent_definitions WHERE {' AND '.join(where)} ORDER BY updated_at DESC", *params
        )
        return [await _row_to_dict(conn, r) for r in rows]


async def get_direct_reports(reports_to_agent_id: str | None) -> list[dict]:
    """Direct reports of a given AgentDefinition. When reports_to_agent_id
    is None, returns the top-level AgentDefinitions — which are exactly the
    CXO Command Agent's own direct reports, since the CXO is not itself an
    AgentDefinition row (see services/cxo_command_service.py's module
    docstring) and every top-level agent already reports, conceptually, to
    it."""
    if reports_to_agent_id is None:
        return await list_definitions(top_level_only=True)
    return await list_definitions(reports_to_agent_id=reports_to_agent_id)


async def update_definition(definition_id: str, *, change_note: str | None = None, updated_by: str = "system", **fields) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    existing = await get_definition(definition_id)
    if not existing:
        return None

    scalar_updatable = {
        "name", "description", "status", "default_model", "default_token_budget",
        "instructions", "inputs", "outputs", "knowledge_requirements", "human_review_config", "environment_id",
        "reports_to_agent_id", "memory_enabled", "temporal_knowledge_enabled",
    }
    # schedule_config/heartbeat_config are JSONB-merged (Postgres `||`), not
    # replaced wholesale like scalar_updatable fields — see
    # AgentDefinitionUpdateRequest's docstring in models/agent_definition.py
    # for why: a caller sending a partial dict (e.g. just {"enabled": true})
    # must not wipe out scheduler/heartbeat-owned runtime state (next_run_at,
    # running, running_since, ...) it doesn't know about.
    json_merge_updatable = {"schedule_config", "heartbeat_config"}
    relation_fields = {"capability_ids", "use_case_ids", "skill_ids"}

    sets, params = [], []
    for key, value in fields.items():
        if key in json_merge_updatable and value is not None:
            params.append(value)
            sets.append(f"{key} = {key} || ${len(params)}::jsonb")
        elif key in scalar_updatable and value is not None:
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        elif key == "name" and value is not None:
            params.append(_slugify(value))
            sets.append(f"slug = ${len(params)}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if fields.get("reports_to_agent_id") is not None:
                effective_use_case_ids = (
                    fields["use_case_ids"] if fields.get("use_case_ids") is not None else existing["use_case_ids"]
                )
                await _validate_reports_to(
                    conn, tenant_id=tenant_id, definition_id=definition_id,
                    reports_to_agent_id=fields["reports_to_agent_id"], use_case_ids=effective_use_case_ids,
                )

            if sets:
                params.append(datetime.now(timezone.utc))
                sets.append(f"updated_at = ${len(params)}")
                params.append(definition_id)
                await conn.execute(f"UPDATE agent_definitions SET {', '.join(sets)} WHERE id = ${len(params)}", *params)

            if "capability_ids" in fields and fields["capability_ids"] is not None:
                await conn.execute("DELETE FROM agent_definition_capabilities WHERE agent_definition_id = $1", definition_id)
                for cap_id in fields["capability_ids"]:
                    await conn.execute(
                        "INSERT INTO agent_definition_capabilities (agent_definition_id, capability_id) VALUES ($1, $2)",
                        definition_id, cap_id,
                    )
            if "use_case_ids" in fields and fields["use_case_ids"] is not None:
                await conn.execute("DELETE FROM agent_definition_use_cases WHERE agent_definition_id = $1", definition_id)
                for ws_id in fields["use_case_ids"]:
                    await conn.execute(
                        "INSERT INTO agent_definition_use_cases (agent_definition_id, workspace_id) VALUES ($1, $2)",
                        definition_id, ws_id,
                    )
            if "skill_ids" in fields and fields["skill_ids"] is not None:
                await conn.execute("DELETE FROM agent_definition_skills WHERE agent_definition_id = $1", definition_id)
                for skill_id in fields["skill_ids"]:
                    await conn.execute(
                        "INSERT INTO agent_definition_skills (agent_definition_id, skill_id) VALUES ($1, $2)",
                        definition_id, skill_id,
                    )

            changed = bool(sets) or any(f in fields and fields[f] is not None for f in relation_fields)
            if changed:
                next_version = existing["current_version"] + 1
                await conn.execute(
                    "UPDATE agent_definitions SET current_version = $1 WHERE id = $2", next_version, definition_id
                )
                row = await conn.fetchrow("SELECT * FROM agent_definitions WHERE id = $1", definition_id)
                snapshot_dict = await _row_to_dict(conn, row)
                await _write_version(
                    conn, agent_definition_id=definition_id, version=next_version,
                    snapshot=_snapshot_of(snapshot_dict), change_note=change_note, created_by=updated_by,
                )
            else:
                row = await conn.fetchrow("SELECT * FROM agent_definitions WHERE id = $1", definition_id)
                snapshot_dict = await _row_to_dict(conn, row)

    logger.info("AgentDefinition updated: id=%s fields=%s", definition_id, list(fields.keys()))
    return snapshot_dict


async def list_versions(definition_id: str) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT version, snapshot, change_note, created_by, created_at FROM agent_definition_versions "
            "WHERE agent_definition_id = $1 ORDER BY version DESC",
            definition_id,
        )
    return [
        {
            "version": r["version"], "snapshot": r["snapshot"], "change_note": r["change_note"],
            "created_by": r["created_by"], "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def get_version(definition_id: str, version: int) -> dict | None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT version, snapshot, change_note, created_by, created_at FROM agent_definition_versions "
            "WHERE agent_definition_id = $1 AND version = $2",
            definition_id, version,
        )
    if not r:
        return None
    return {
        "version": r["version"], "snapshot": r["snapshot"], "change_note": r["change_note"],
        "created_by": r["created_by"], "created_at": r["created_at"].isoformat(),
    }


async def get_execution_history(definition_id: str, *, limit: int = 20) -> tuple[list[dict], int] | None:
    """Read-only aggregation of existing run activity for the capabilities
    this definition references — via the same agent_runtime_service.list_runs
    the Agent Runtime API already serves. Does not create any new execution
    linkage; a definition with no linked capabilities simply has no history."""
    definition = await get_definition(definition_id)
    if definition is None:
        return None
    capability_names = []
    for cap_id in definition["capability_ids"]:
        cap = get_static_capability(cap_id)
        if cap:
            capability_names.append(cap["name"])

    if not capability_names:
        return [], 0

    all_runs: dict[str, dict] = {}
    for name in capability_names:
        runs, _ = await agent_runtime_service.list_runs(capability=name, limit=1000, offset=0)
        for run in runs:
            all_runs[run["id"]] = run

    ordered = sorted(all_runs.values(), key=lambda r: r.get("created_at", ""), reverse=True)
    return ordered[:limit], len(ordered)
