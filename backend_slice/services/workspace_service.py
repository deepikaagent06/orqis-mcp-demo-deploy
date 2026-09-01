"""Workspace catalog + template persistence — backed by Postgres. Replaces
the in-memory WORKSPACES/TEMPLATES dicts in routers/workspace.py.

`last_run` and `recent_runs` are deliberately NOT stored columns — they are
computed live from runs_service.list_runs_for_workspace() (the real
workflow_runs table) on every read, so this module never maintains a second,
divergent copy of run history. Storing our own counter here would have been
exactly the kind of duplicate-implementation the rest of this migration is
trying to eliminate.

Global catalog vs. tenant-specific state (deliberate split, confirmed
2026-08-05 — do not collapse the two without asking first):
  - `workspaces` (this module) is a single, tenant-less catalog: `name`,
    `description`, and `configuration` are shared across every tenant by
    design, same as `templates`. An admin editing a workspace's name or
    description changes what every tenant sees — that's intentional, not a
    tenant-isolation bug. Mutating these still requires `require_tenant_admin()`
    (see routers/workspace.py), but that's a role gate on a shared resource,
    not per-tenant ownership.
  - Anything that varies per tenant — whether a tenant has switched a
    workspace on, and its live running/idle/disabled status — is NOT stored
    here. It lives in `tenant_workspaces` (services/tenant_service.py),
    keyed by (tenant_id, workspace_slug), and is overlaid onto each catalog
    row at read time by `_tenant_enabled_map()` / `_with_run_stats()` below.
    See services/tenant_service.py's `tenant_workspaces` section and
    tests/test_workspace_tenant_scoping.py for the isolation this depends on.
  - `configuration` (JSONB, both on this table and on `tenant_workspaces`)
    is accepted and stored but currently reserved/unused — no code path
    reads it back (routers/workspace.py even strips it out of API
    responses). It exists for a future per-workspace or per-tenant config
    use case; until something actually reads it, changing it has no
    observable effect on any tenant.
"""
import json
import logging
import uuid
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id
from services import runs_service

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    description TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'available',
    -- `enabled`/`runtime_status` columns are legacy — the API now overlays
    -- the caller's own tenant_workspaces row on every read instead of
    -- reading these back (see module docstring). Left in place rather than
    -- dropped so existing rows/migrations aren't disturbed.
    enabled BOOLEAN NOT NULL DEFAULT false,
    owner VARCHAR(200) NOT NULL,
    business_impact VARCHAR(300) NOT NULL DEFAULT '',
    icon VARCHAR(50) NOT NULL DEFAULT 'Briefcase',
    health VARCHAR(20) NOT NULL DEFAULT 'healthy',
    runtime_status VARCHAR(20) NOT NULL DEFAULT 'idle',
    -- Reserved/unused — see module docstring.
    configuration JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS templates (
    id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    workspace_type VARCHAR(200) NOT NULL,
    steps_count INTEGER NOT NULL DEFAULT 0,
    capabilities JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workspaces_slug ON workspaces(slug);
"""

# Seed-once catalog, ported from the former in-memory WORKSPACES dict.
# business_impact is now a positioning tagline rather than a specific
# fabricated dollar figure presented as a live measured outcome — real
# revenue-impact tracking would require structured financial outcome data
# this system doesn't yet capture (see audit report).
_SEED_WORKSPACES: list[dict] = [
    {"id": "ws-2", "name": "Customer Winback", "slug": "customer-winback",
     "description": "Analyze churned customers, identify winback opportunities, and generate personalized retention campaigns.",
     "status": "active", "enabled": True, "owner": "Deepika S.",
     "business_impact": "Customer retention & churn prevention", "icon": "Users", "health": "healthy"},
    {"id": "ws-3", "name": "Revenue Leakage", "slug": "revenue-leakage",
     "description": "Detect revenue leakage patterns, quantify impact, and recommend recovery strategies.",
     "status": "active", "enabled": True, "owner": "James W.",
     "business_impact": "Revenue leakage detection & recovery", "icon": "TrendingUp", "health": "healthy"},
    {"id": "ws-4", "name": "CSAT Analysis", "slug": "csat-analysis",
     "description": "Analyze customer satisfaction scores, identify key drivers, and generate improvement recommendations.",
     "status": "available", "enabled": False, "owner": "Deepika S.",
     "business_impact": "Customer satisfaction improvement", "icon": "BarChart3", "health": "degraded"},
    {"id": "ws-5", "name": "Policy Compliance Review", "slug": "policy-compliance",
     "description": "Review organizational policies against regulatory frameworks and identify compliance requirements.",
     "status": "available", "enabled": False, "owner": "Priya M.",
     "business_impact": "Regulatory compliance & gap closure", "icon": "FileCheck", "health": "healthy"},
    {"id": "ws-6", "name": "AI Decision Audit", "slug": "ai-decision-audit",
     "description": "Audit AI-driven decisions for fairness, transparency, and regulatory compliance.",
     "status": "available", "enabled": False, "owner": "Deepika S.",
     "business_impact": "AI governance & fairness assurance", "icon": "Brain", "health": "healthy"},
    {"id": "ws-7", "name": "Customer Escalation Management", "slug": "customer-escalation-management",
     "description": "Detect cases that require escalation across Post-Resolution, Repeated Contact, "
                     "Policy-Driven, and SLA/Aging signals, and provide an accountable, evidence-backed "
                     "resolution path.",
     "status": "available", "enabled": False, "owner": "Deepika S.",
     "business_impact": "Escalation accountability & retention protection", "icon": "Siren", "health": "healthy"},
    {"id": "ws-8", "name": "Operational Risk", "slug": "operational-risk",
     "description": "Identify, assess, and monitor operational risk exposure across business processes — "
                     "process breakdowns, control gaps, vendor/third-party exposure, and incidents/near-misses "
                     "— each with an accountable remediation owner and due date.",
     "status": "available", "enabled": False, "owner": "Deepika S.",
     "business_impact": "Operational risk governance & remediation accountability", "icon": "ShieldAlert",
     "health": "healthy"},
]

# Marketplace canonical list (Phase 4) — `name` matches use_case_service.py's
# active catalog 1:1 so the frontend's enrichTemplates() same-name fallback
# can resolve a category even when `workspace_type` has no matching real
# workspace yet. `workspace_type` is the free-text field actually used to
# resolve a launchable workspace (see enrich-templates.ts resolveWorkspaceSlug);
# for the 4 with no built workflow yet it deliberately matches no real
# workspace name, so `workspaceSlug` resolves to null and the existing
# CreateWorkspaceWizard "no workspace matches yet" preview-only state applies
# — no new UI needed. Their steps_count/capabilities are honestly 0/[]
# rather than invented.
_SEED_TEMPLATES: list[dict] = [
    {"id": "tpl-7", "name": "Customer Retention",
     "description": "Pre-built workflow for customer retention campaigns with churn analysis, segmentation, offer generation, and approval gates.",
     "workspace_type": "Customer Winback", "steps_count": 6,
     "capabilities": ["Data Intelligence", "Customer Intelligence", "Offer Strategy", "Human Review Gate", "Policy Validation", "Decision Recorder"]},
    {"id": "tpl-8", "name": "Revenue Integrity",
     "description": "Pre-built workflow for revenue leakage detection with data analysis, pattern recognition, impact quantification, and recovery recommendations.",
     "workspace_type": "Revenue Leakage", "steps_count": 7,
     "capabilities": ["Data Intelligence", "Revenue Analysis", "Gap Detection", "Risk Assessment", "Recommendation Engine", "Human Review Gate", "Executive Intelligence"]},
    {"id": "tpl-9", "name": "Decision Governance",
     "description": "Pre-built workflow for auditing AI decisions with fairness analysis, transparency scoring, and regulatory compliance checks.",
     "workspace_type": "AI Decision Audit", "steps_count": 5,
     "capabilities": ["Data Intelligence", "Policy Validation", "Risk Assessment", "Audit Logger", "Executive Intelligence"]},
    {"id": "tpl-10", "name": "Policy Governance",
     "description": "Pre-built workflow for policy compliance review with document parsing, regulatory mapping, and compliance verification.",
     "workspace_type": "Policy Compliance", "steps_count": 6,
     "capabilities": ["Document Intelligence", "Policy Extraction", "Compliance Checker", "Gap Detection", "Human Approval Gate", "Decision Recorder"]},
    {"id": "tpl-11", "name": "Customer Escalation Management",
     "description": "Track, prioritize, and resolve escalated customer issues before they impact retention.",
     "workspace_type": "Customer Escalation Management", "steps_count": 0,
     "capabilities": []},
    {"id": "tpl-12", "name": "Operational Risk",
     "description": "Identify, assess, and monitor operational risk exposure across business processes.",
     "workspace_type": "Operational Risk", "steps_count": 0,
     "capabilities": []},
    {"id": "tpl-13", "name": "Regulatory Compliance",
     "description": "Pre-built workflow for regulatory compliance review with document parsing, regulatory mapping, and compliance verification — the same underlying workflow as Policy Governance, offered under its regulatory framing.",
     "workspace_type": "Policy Compliance", "steps_count": 6,
     "capabilities": ["Document Intelligence", "Policy Extraction", "Compliance Checker", "Gap Detection", "Human Approval Gate", "Decision Recorder"]},
    {"id": "tpl-14", "name": "Document Review",
     "description": "Parse, analyze, and validate documents against policy and regulatory requirements.",
     "workspace_type": "Document Review", "steps_count": 0,
     "capabilities": []},
]

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await _seed_if_empty(conn)
    _schema_ready = True
    logger.info("Workspace schema ensured (workspaces, templates)")


async def _seed_if_empty(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM workspaces")
    if not count:
        for w in _SEED_WORKSPACES:
            await conn.execute(
                "INSERT INTO workspaces (id, name, slug, description, status, enabled, owner, business_impact, icon, health) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                w["id"], w["name"], w["slug"], w["description"], w["status"], w["enabled"], w["owner"],
                w["business_impact"], w["icon"], w["health"],
            )
        logger.info("Workspaces seeded with %d entries", len(_SEED_WORKSPACES))

    tcount = await conn.fetchval("SELECT COUNT(*) FROM templates")
    if not tcount:
        for t in _SEED_TEMPLATES:
            await conn.execute(
                "INSERT INTO templates (id, name, description, workspace_type, steps_count, capabilities) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                t["id"], t["name"], t["description"], t["workspace_type"], t["steps_count"], t["capabilities"],
            )
        logger.info("Templates seeded with %d entries", len(_SEED_TEMPLATES))


async def ensure_workspace_registered(row: dict) -> None:
    """Idempotently inserts one catalog row if it's missing — for a use case
    added after the initial _seed_if_empty() bulk-seed already ran against a
    live/already-seeded database (mirrors the one-off backfill
    scripts/reconcile_use_case_registry.py performs by hand, as a reusable
    function instead). No-ops if a row with this id or slug already exists."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, enabled, owner, business_impact, icon, health) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ON CONFLICT (id) DO NOTHING",
            row["id"], row["name"], row["slug"], row["description"], row["status"], row["enabled"], row["owner"],
            row["business_impact"], row["icon"], row["health"],
        )


async def _tenant_enabled_map(tenant_id: str) -> dict[str, bool]:
    """This tenant's own enablement flags, keyed by workspace slug — never
    another tenant's. Absent from the map (never enabled by this tenant)
    reads as disabled, not as whatever the shared catalog seed happened to
    default to."""
    from services.tenant_service import get_tenant_workspaces

    rows = await get_tenant_workspaces(tenant_id)
    return {r["workspace_slug"]: r["enabled"] for r in rows}


async def _with_run_stats(row: dict, tenant_enabled: bool) -> dict:
    # runs_service filters by the ambient request tenant_id itself, so this
    # is already scoped to the caller's own tenant.
    runs = await runs_service.list_runs_for_workspace(row["slug"])
    last_run = runs[0]["started_at"] if runs else None
    if not tenant_enabled:
        runtime_status = "disabled"
    elif runs and runs[0]["status"] == "running":
        runtime_status = "running"
    else:
        runtime_status = "idle"
    return {
        **row,
        "enabled": tenant_enabled,
        "runtime_status": runtime_status,
        "last_run": last_run,
        "recent_runs": len(runs),
    }


def _row_to_dict(r: asyncpg.Record) -> dict:
    config = r["configuration"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "id": r["id"], "name": r["name"], "slug": r["slug"], "description": r["description"],
        "status": r["status"], "enabled": r["enabled"], "owner": r["owner"],
        "business_impact": r["business_impact"], "icon": r["icon"], "health": r["health"],
        "runtime_status": r["runtime_status"], "configuration": config,
    }


def _template_row_to_dict(r: asyncpg.Record) -> dict:
    caps = r["capabilities"]
    return {
        "id": r["id"], "name": r["name"], "description": r["description"], "workspace_type": r["workspace_type"],
        "steps_count": r["steps_count"], "capabilities": json.loads(caps) if isinstance(caps, str) else list(caps),
    }


# ── Workspaces ───────────────────────────────────────────────

async def list_workspaces() -> list[dict]:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM workspaces ORDER BY created_at")
    items = [_row_to_dict(r) for r in rows]
    enabled_map = await _tenant_enabled_map(tenant_id)
    return [await _with_run_stats(i, enabled_map.get(i["slug"], False)) for i in items]


async def get_workspace(workspace_id_or_slug: str) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM workspaces WHERE id = $1 OR slug = $1", workspace_id_or_slug
        )
    if not row:
        return None
    item = _row_to_dict(row)
    enabled_map = await _tenant_enabled_map(tenant_id)
    return await _with_run_stats(item, enabled_map.get(item["slug"], False))


async def update_workspace(workspace_id_or_slug: str, *, name=None, description=None, enabled=None, configuration=None) -> dict | None:
    """Catalog fields (name/description/configuration) remain a shared,
    global edit — unchanged, still admin-gated at the router. `enabled` is
    NOT a catalog field: it is this tenant's own tenant_workspaces flag, so
    it is written there, scoped to get_tenant_id(), and can never affect any
    other tenant's enablement of the same catalog workspace.
    """
    await ensure_schema()
    tenant_id = get_tenant_id()

    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if configuration is not None:
        fields["configuration"] = configuration

    pool = await get_pool()
    if fields:
        fields["updated_at"] = datetime.now(timezone.utc)
        set_clauses = [f"{k} = ${i + 2}" for i, k in enumerate(fields)]
        query = f"UPDATE workspaces SET {', '.join(set_clauses)} WHERE id = $1 OR slug = $1 RETURNING *"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, workspace_id_or_slug, *fields.values())
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workspaces WHERE id = $1 OR slug = $1", workspace_id_or_slug)
    if not row:
        return None

    if enabled is not None:
        from services.tenant_service import set_tenant_workspace_enabled
        await set_tenant_workspace_enabled(tenant_id, row["slug"], enabled)

    return await get_workspace(workspace_id_or_slug)


async def set_runtime_status(workspace_id_or_slug: str, runtime_status: str) -> None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE workspaces SET runtime_status = $2 WHERE id = $1 OR slug = $1",
            workspace_id_or_slug, runtime_status,
        )


# ── Templates ────────────────────────────────────────────────

async def list_templates() -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM templates ORDER BY created_at")
    return [_template_row_to_dict(r) for r in rows]


async def get_template(template_id: str) -> dict | None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM templates WHERE id = $1", template_id)
    return _template_row_to_dict(row) if row else None


async def create_template(
    *, name: str, description: str, workspace_type: str,
    capabilities: list[str] | None = None, steps_count: int = 0,
) -> dict:
    await ensure_schema()
    template_id = f"tpl-{uuid.uuid4().hex[:8]}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO templates (id, name, description, workspace_type, steps_count, capabilities) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            template_id, name, description, workspace_type, steps_count, json.dumps(capabilities or []),
        )
    return _template_row_to_dict(row)


async def update_template(template_id: str, *, name=None, description=None) -> dict | None:
    await ensure_schema()
    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if not fields:
        return await get_template(template_id)

    set_clauses = [f"{k} = ${i + 2}" for i, k in enumerate(fields)]
    query = f"UPDATE templates SET {', '.join(set_clauses)} WHERE id = $1 RETURNING *"

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, template_id, *fields.values())
    return _template_row_to_dict(row) if row else None


async def delete_template(template_id: str) -> bool:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM templates WHERE id = $1", template_id)
    return result != "DELETE 0"
