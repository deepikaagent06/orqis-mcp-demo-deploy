"""Workflow run persistence — backed by Postgres, replacing the in-memory
RUNS list in routers/workspace.py. A run's status and step_results are
written incrementally as the pipeline executes (see pipeline_executor.py),
so a paused-for-review run, or a backend restart mid-run, never loses state.
"""
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id VARCHAR(100) PRIMARY KEY,
    workspace_id VARCHAR(100) NOT NULL,
    workspace_slug VARCHAR(100) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant',
    status VARCHAR(30) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    steps_total INTEGER NOT NULL DEFAULT 0,
    steps_completed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    cost NUMERIC(10,4) NOT NULL DEFAULT 0,
    nodes_snapshot JSONB NOT NULL DEFAULT '[]',
    step_results JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workspace ON workflow_runs(workspace_slug);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status);
"""

_MIGRATE_SQL = """
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant';
CREATE INDEX IF NOT EXISTS idx_workflow_runs_tenant ON workflow_runs(tenant_id);
"""

# Seed-once demo run history (today's hardcoded RUNS list) so Run History
# isn't empty on first boot. None of these have nodes_snapshot/step_results —
# same as the original in-memory entries, whose detail view already
# defaulted to an empty steps array.
_SEED_RUNS: list[dict] = [
    {"id": "run-005", "workspace_slug": "customer-winback", "status": "completed", "started_at": "2026-06-22T10:00:00Z", "completed_at": "2026-06-22T10:35:00Z", "steps_completed": 6, "steps_total": 6, "tokens_used": 31200, "cost": 0.94},
    {"id": "run-006", "workspace_slug": "customer-winback", "status": "completed", "started_at": "2026-06-21T16:00:00Z", "completed_at": "2026-06-21T16:28:00Z", "steps_completed": 6, "steps_total": 6, "tokens_used": 29800, "cost": 0.89},
    {"id": "run-007", "workspace_slug": "customer-winback", "status": "paused_for_review", "started_at": "2026-06-23T07:30:00Z", "completed_at": None, "steps_completed": 4, "steps_total": 6, "tokens_used": 22100, "cost": 0.66},
    {"id": "run-008", "workspace_slug": "revenue-leakage", "status": "completed", "started_at": "2026-06-22T13:00:00Z", "completed_at": "2026-06-22T13:45:00Z", "steps_completed": 7, "steps_total": 7, "tokens_used": 35600, "cost": 1.07},
    {"id": "run-009", "workspace_slug": "revenue-leakage", "status": "completed", "started_at": "2026-06-20T09:00:00Z", "completed_at": "2026-06-20T09:38:00Z", "steps_completed": 7, "steps_total": 7, "tokens_used": 33100, "cost": 0.99},
    {"id": "run-010", "workspace_slug": "csat-analysis", "status": "completed", "started_at": "2026-06-21T11:00:00Z", "completed_at": "2026-06-21T11:25:00Z", "steps_completed": 5, "steps_total": 5, "tokens_used": 21400, "cost": 0.64},
    {"id": "run-011", "workspace_slug": "policy-compliance", "status": "completed", "started_at": "2026-06-19T14:00:00Z", "completed_at": "2026-06-19T14:50:00Z", "steps_completed": 6, "steps_total": 6, "tokens_used": 28900, "cost": 0.87},
    {"id": "run-012", "workspace_slug": "ai-decision-audit", "status": "running", "started_at": "2026-06-23T06:00:00Z", "completed_at": None, "steps_completed": 2, "steps_total": 5, "tokens_used": 9800, "cost": 0.29},
]

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await conn.execute(_MIGRATE_SQL)
        await _seed_if_empty(conn)
    _schema_ready = True
    logger.info("Runs schema ensured (workflow_runs)")


async def _seed_if_empty(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM workflow_runs")
    if count:
        return
    for r in _SEED_RUNS:
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(id, workspace_id, workspace_slug, tenant_id, status, started_at, completed_at, steps_total, steps_completed, tokens_used, cost) "
            "VALUES ($1, $1, $2, 'demo-tenant', $3, $4, $5, $6, $7, $8, $9)",
            r["id"], r["workspace_slug"], r["status"], datetime.fromisoformat(r["started_at"]),
            datetime.fromisoformat(r["completed_at"]) if r["completed_at"] else None,
            r["steps_total"], r["steps_completed"], r["tokens_used"], r["cost"],
        )
    logger.info("Workflow runs seeded with %d entries", len(_SEED_RUNS))


def _row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"], "workspace_id": r["workspace_id"], "workspace_slug": r["workspace_slug"],
        "status": r["status"], "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        "steps_total": r["steps_total"], "steps_completed": r["steps_completed"],
        "tokens_used": r["tokens_used"], "cost": float(r["cost"]),
        "nodes_snapshot": list(r["nodes_snapshot"]), "step_results": list(r["step_results"]),
    }


async def create_run(*, run_id: str, workspace_id: str, workspace_slug: str, nodes_snapshot: list[dict]) -> dict:
    await ensure_schema()
    tenant_id = get_tenant_id()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs "
            "(id, workspace_id, workspace_slug, tenant_id, status, started_at, steps_total, steps_completed, tokens_used, cost, nodes_snapshot, step_results) "
            "VALUES ($1, $2, $3, $4, 'running', $5, $6, 0, 0, 0, $7, '[]')",
            run_id, workspace_id, workspace_slug, tenant_id, now, len(nodes_snapshot), nodes_snapshot,
        )
    return await get_run(run_id)


async def get_run(run_id: str) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM workflow_runs WHERE id = $1 AND tenant_id = $2", run_id, tenant_id
        )
    return _row_to_dict(row) if row else None


async def aggregate_stats() -> dict:
    """Real run totals for the Executive Dashboard — replaces the former
    hardcoded DASHBOARD_METRICS/COST_BY_WEEK/WORKFLOWS_BY_STATUS constants."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        totals = await conn.fetchrow(
            "SELECT COUNT(*) AS total_runs, "
            "COUNT(*) FILTER (WHERE status = 'running') AS active, "
            "COUNT(*) FILTER (WHERE status = 'completed') AS completed, "
            "COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
            "COALESCE(SUM(tokens_used), 0) AS total_tokens, "
            "COALESCE(SUM(cost), 0) AS total_cost "
            "FROM workflow_runs WHERE tenant_id = $1",
            tenant_id,
        )
        status_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM workflow_runs WHERE tenant_id = $1 GROUP BY status", tenant_id
        )
        week_rows = await conn.fetch(
            "SELECT to_char(date_trunc('week', started_at), 'YYYY-MM-DD') AS week, "
            "COALESCE(SUM(cost), 0) AS cost "
            "FROM workflow_runs WHERE tenant_id = $1 AND started_at > NOW() - INTERVAL '8 weeks' "
            "GROUP BY 1 ORDER BY 1",
            tenant_id,
        )
    total_finished = (totals["completed"] or 0) + (totals["failed"] or 0)
    return {
        "active_workflows": totals["active"] or 0,
        "total_runs": totals["total_runs"] or 0,
        "success_rate": round((totals["completed"] or 0) / total_finished * 100) if total_finished else 100,
        "total_cost": round(float(totals["total_cost"] or 0), 2),
        "total_tokens": int(totals["total_tokens"] or 0),
        "workflows_by_status": [{"name": r["status"].replace("_", " ").title(), "value": r["n"]} for r in status_rows],
        "cost_by_week": [{"week": r["week"], "cost": round(float(r["cost"]), 2)} for r in week_rows],
    }


async def today_stats() -> dict:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_used), 0) AS tokens "
            "FROM workflow_runs WHERE tenant_id = $1 AND started_at >= date_trunc('day', NOW())",
            tenant_id,
        )
    return {"total_runs_today": int(row["n"] or 0), "tokens_used_today": int(row["tokens"] or 0)}


async def sum_tokens_for_workspace(workspace_id: str) -> int:
    """Cumulative tokens_used across every run tied to this workspace_id,
    tenant-scoped. For an Agent Runtime execution (services/
    agent_runtime_executor.py), workspace_id is the AgentDefinition's own id —
    globally unique (f"agentdef-{uuid4().hex[:10]}"), so this sum is exactly
    that agent's own total usage, never another agent's or a graph-based
    workflow's. Used to enforce AgentDefinition.default_token_budget as a
    cumulative total across all of that agent's executions — including CXO
    delegations, which reuse this exact same execute() path under the
    delegated agent's own id (services/cxo_command_service.py never runs a
    delegated agent under any other identity). Reads the same tokens_used
    column append_step_result() already writes — no separate usage ledger."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM workflow_runs WHERE workspace_id = $1 AND tenant_id = $2",
            workspace_id, tenant_id,
        )
    return int(total or 0)


async def active_run_count(workspace_slug: str) -> int:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM workflow_runs WHERE workspace_slug = $1 AND tenant_id = $2 AND status = 'running'",
            workspace_slug, tenant_id,
        )
    return int(count or 0)


async def list_all_runs() -> list[dict]:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM workflow_runs WHERE tenant_id = $1 ORDER BY started_at DESC", tenant_id
        )
    return [_row_to_dict(r) for r in rows]


async def list_runs_for_workspace(workspace_slug: str) -> list[dict]:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM workflow_runs WHERE workspace_slug = $1 AND tenant_id = $2 ORDER BY started_at DESC",
            workspace_slug, tenant_id,
        )
    return [_row_to_dict(r) for r in rows]


async def set_status(run_id: str, status: str, *, completed: bool = False) -> None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if completed:
            await conn.execute(
                "UPDATE workflow_runs SET status = $1, completed_at = $2 WHERE id = $3 AND tenant_id = $4",
                status, datetime.now(timezone.utc), run_id, tenant_id,
            )
        else:
            await conn.execute(
                "UPDATE workflow_runs SET status = $1 WHERE id = $2 AND tenant_id = $3",
                status, run_id, tenant_id,
            )


async def append_step_result(run_id: str, step_result: dict, *, tokens: int, cost: float) -> None:
    """Read-modify-write under a row lock — execution is single-threaded per
    run, but the lock keeps this correct if that ever changes."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT step_results, tokens_used, cost FROM workflow_runs WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                run_id, tenant_id,
            )
            results = list(row["step_results"]) + [step_result]
            new_tokens = row["tokens_used"] + tokens
            new_cost = float(row["cost"]) + cost
            await conn.execute(
                "UPDATE workflow_runs SET step_results = $1, steps_completed = $2, tokens_used = $3, cost = $4 WHERE id = $5 AND tenant_id = $6",
                results, len(results), new_tokens, round(new_cost, 4), run_id, tenant_id,
            )


async def append_step_result_with_budget_check(
    run_id: str, workspace_id: str, step_result: dict, *, tokens: int, cost: float, token_budget: int,
) -> tuple[int, bool]:
    """Same effect as append_step_result, but computes whether this append
    pushes the AgentDefinition's cumulative usage (workspace_id) over
    token_budget from a fresh, lock-serialized re-sum of workflow_runs —
    never from a pre-call snapshot taken before the LLM call.

    Why this exists: services/agent_runtime_executor.py's budget check used
    to compute tokens_used_after as tokens_used_before + this_call's tokens,
    where tokens_used_before was read once, before the (slow) LLM call. Two
    concurrent executions of the same AgentDefinition can each read the same
    stale tokens_used_before, each independently compute an under-budget
    total from it, and each finalize normally — even though their combined
    committed usage exceeds the budget once both land. Re-summing under a
    Postgres advisory transaction lock keyed by workspace_id closes that
    race: whichever call commits second always sees the other's already-
    committed usage. The lock is scoped to this function's own short
    transaction (a re-sum plus one row update) — it is never held across the
    LLM call itself, so it doesn't tie up a pool connection during network
    I/O, and executions of *different* AgentDefinitions never contend with
    each other.

    step_result["budget_exceeded"]/["tokens_used_cumulative"] are filled in
    here, inside the same transaction that persists them, so the returned
    values always match exactly what was written."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock key includes tenant_id, not just workspace_id, so a
            # hashtext collision can never make one tenant's execution wait
            # on a lock actually held on another tenant's behalf.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", f"{tenant_id}:{workspace_id}")
            workspace_total = await conn.fetchval(
                "SELECT COALESCE(SUM(tokens_used), 0) FROM workflow_runs WHERE workspace_id = $1 AND tenant_id = $2",
                workspace_id, tenant_id,
            )
            tokens_used_after = int(workspace_total or 0) + tokens
            budget_exceeded = bool(token_budget) and tokens_used_after > token_budget
            step_result = {**step_result, "budget_exceeded": budget_exceeded, "tokens_used_cumulative": tokens_used_after}

            row = await conn.fetchrow(
                "SELECT step_results, tokens_used, cost FROM workflow_runs WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                run_id, tenant_id,
            )
            results = list(row["step_results"]) + [step_result]
            new_tokens = row["tokens_used"] + tokens
            new_cost = float(row["cost"]) + cost
            await conn.execute(
                "UPDATE workflow_runs SET step_results = $1, steps_completed = $2, tokens_used = $3, cost = $4 "
                "WHERE id = $5 AND tenant_id = $6",
                results, len(results), new_tokens, round(new_cost, 4), run_id, tenant_id,
            )
    return tokens_used_after, budget_exceeded


async def rewind_for_changes_requested(run_id: str, *, keep_step_count: int) -> None:
    """Drop step_results back to the point before the step under review, so a
    regenerated attempt replaces the old one instead of appending a duplicate."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT step_results FROM workflow_runs WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                run_id, tenant_id,
            )
            results = list(row["step_results"])[:keep_step_count]
            await conn.execute(
                "UPDATE workflow_runs SET step_results = $1, steps_completed = $2, status = 'running' WHERE id = $3 AND tenant_id = $4",
                results, len(results), run_id, tenant_id,
            )
