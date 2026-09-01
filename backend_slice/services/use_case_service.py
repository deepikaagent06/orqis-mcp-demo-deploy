"""Canonical Use Case catalog — the single source of truth for the business
capabilities ORQIS offers, and the source Marketplace's "Use Cases" tab
renders (Marketplace Phase 4). Backed by Postgres, seeded once, following
the same ensure_schema()/seed-once pattern as services/workspace_service.py.

This catalog is deliberately a separate concept from services/workspace_service.py's
`workspaces` table: a "use case" is a business capability a customer picks
(what onboarding, and Marketplace, show); a "workspace" is a
concrete implementation of one (what actually runs). A use case may or may
not have a workspace built for it yet, hence the nullable `workspace_slug`
link — it is not a foreign key, since workspace_service.py's catalog is
intentionally left untouched by this module.

`status` ('active' | 'archived') lets the catalog evolve without deleting
history: `list_use_cases()` only ever returns 'active' rows by default, so a
live, already-seeded database can retire a use case (archive it in place)
instead of deleting the row — which matters because `tenant_use_cases` rows
from real onboarded tenants reference these slugs and must keep resolving.
The seed list below is the catalog's steady state for a *fresh* database
only; retiring an already-seeded row is a one-time UPDATE against the live
DB, not something this module does automatically.

The one opt-in exception is `include_archived=True` (non-starter callers
only): the Marketplace Active/Disabled toggle writes to this same `status`
column (see update_use_case_status below), so a disabled Use Case must stay
visible in the Marketplace's own listing across a reload or it could never
be found again to re-enable. This does not change the default — every other
caller (this module's starter_only path, routers/onboarding.py, and
GET /api/use-cases with no `include=archived`) keeps seeing active-only
rows exactly as before.

Two use cases below (`policy-governance` and `regulatory-compliance`) point
at the same `workspace_slug` ("policy-compliance") on purpose: today a
single workspace implementation serves both business capabilities. That's a
real statement about current product state, not a bug.
"""
import logging

import asyncpg

from database import get_pool

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS use_cases (
    id VARCHAR(50) PRIMARY KEY,
    slug VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    category VARCHAR(50) NOT NULL DEFAULT 'business_capability',
    is_starter BOOLEAN NOT NULL DEFAULT false,
    workspace_slug VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_use_cases_slug ON use_cases(slug);
CREATE INDEX IF NOT EXISTS idx_use_cases_starter ON use_cases(is_starter);
"""

# The approved Marketplace canonical list (Phase 4). Descriptions carried
# over verbatim where a prior entry already covered the same capability
# (Customer Retention, Policy Governance, Revenue Integrity, Decision
# Governance, Regulatory Compliance); written fresh, without inventing
# metrics or a build status, for the 3 with no prior equivalent.
_SEED_USE_CASES: list[dict] = [
    {"id": "uc-1", "slug": "customer-retention", "name": "Customer Retention",
     "description": "Support customer recovery, win-back strategies, and retention workflows.",
     "category": "customer", "is_starter": True, "workspace_slug": "customer-winback"},
    {"id": "uc-2", "slug": "revenue-integrity", "name": "Revenue Integrity",
     "description": "Identify and analyze revenue leakage opportunities and operational risks.",
     "category": "revenue", "is_starter": True, "workspace_slug": "revenue-leakage"},
    {"id": "uc-3", "slug": "decision-governance", "name": "Decision Governance",
     "description": "Audit AI-driven decisions for fairness, transparency, and regulatory compliance.",
     "category": "governance", "is_starter": True, "workspace_slug": "ai-decision-audit"},
    {"id": "uc-4", "slug": "policy-governance", "name": "Policy Governance",
     "description": "Evaluate business operations against defined policies and governance rules.",
     "category": "governance", "is_starter": True, "workspace_slug": "policy-compliance"},
    {"id": "uc-5", "slug": "customer-escalation-management", "name": "Customer Escalation Management",
     "description": "Track, prioritize, and resolve escalated customer issues before they impact retention.",
     "category": "customer", "is_starter": True, "workspace_slug": "customer-escalation-management"},
    {"id": "uc-6", "slug": "operational-risk", "name": "Operational Risk",
     "description": "Identify, assess, and monitor operational risk exposure across business processes.",
     "category": "risk", "is_starter": True, "workspace_slug": None},
    {"id": "uc-7", "slug": "regulatory-compliance", "name": "Regulatory Compliance",
     "description": "Provide audit workflows, compliance reviews, and operational traceability against regulatory frameworks.",
     "category": "compliance", "is_starter": True, "workspace_slug": "policy-compliance"},
    {"id": "uc-8", "slug": "document-review", "name": "Document Review",
     "description": "Parse, analyze, and validate documents against policy and regulatory requirements.",
     "category": "compliance", "is_starter": True, "workspace_slug": None},
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
    logger.info("Use case schema ensured (use_cases)")


async def _seed_if_empty(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM use_cases")
    if count:
        return
    for uc in _SEED_USE_CASES:
        await conn.execute(
            "INSERT INTO use_cases (id, slug, name, description, category, is_starter, workspace_slug) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uc["id"], uc["slug"], uc["name"], uc["description"], uc["category"],
            uc["is_starter"], uc["workspace_slug"],
        )
    logger.info("Use cases seeded with %d entries", len(_SEED_USE_CASES))


def _row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"], "slug": r["slug"], "name": r["name"], "description": r["description"],
        "category": r["category"], "is_starter": r["is_starter"], "workspace_slug": r["workspace_slug"],
        "status": r["status"],
    }


async def list_use_cases(
    starter_only: bool = False, include_workflows: bool = False, include_archived: bool = False
) -> list[dict]:
    """Archived rows (retired use cases, kept for tenant_use_cases history —
    see module docstring) are excluded by default.

    include_workflows is additive, default False — existing callers (this
    router today, routers/onboarding.py's starter-only caller) see no
    behavior change. When True, each dict gains a "workflows": list[dict]
    key populated from workflow_definition_service.list_workflow_definitions
    (parent_use_case_id=row["id"]).

    include_archived is additive, default False, and only takes effect when
    starter_only is False — onboarding's starter subset always stays
    active-only. When True, archived rows are included too, so the
    Marketplace can keep a disabled Use Case visible (and thus
    re-enableable) after a reload. See module docstring."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if starter_only:
            rows = await conn.fetch(
                "SELECT * FROM use_cases WHERE status = 'active' AND is_starter = true ORDER BY created_at"
            )
        elif include_archived:
            rows = await conn.fetch("SELECT * FROM use_cases ORDER BY created_at")
        else:
            rows = await conn.fetch("SELECT * FROM use_cases WHERE status = 'active' ORDER BY created_at")
    items = [_row_to_dict(r) for r in rows]

    if include_workflows:
        from services import workflow_definition_service  # local import:
        # avoids a circular import, since workflow_definition_service
        # imports this module to validate parent_use_case_id.

        for item in items:
            item["workflows"] = await workflow_definition_service.list_workflow_definitions(
                parent_use_case_id=item["id"]
            )
    return items


async def get_use_case(slug: str) -> dict | None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM use_cases WHERE slug = $1", slug)
    return _row_to_dict(row) if row else None


async def update_use_case_status(slug: str, status: str) -> dict | None:
    """Marketplace Use Case Active/Disabled toggle target
    (PATCH /api/use-cases/{slug}). Flips only this catalog row's own
    `status` column — independent of workspace_slug and never touches
    services/workspace_service.py's `enabled` flag, which is a separate
    Runtime concept. Returns None if slug doesn't resolve."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE use_cases SET status = $1, updated_at = NOW() WHERE slug = $2 RETURNING *",
            status, slug,
        )
    return _row_to_dict(row) if row else None


async def ensure_workspace_slug(slug: str, workspace_slug: str) -> None:
    """Idempotently fills in a use case's workspace_slug if it is still NULL
    — for a workspace built after the initial _seed_if_empty() bulk-seed
    already ran against a live/already-seeded database. Never overwrites an
    already-set workspace_slug, so it can never silently rewire an existing
    use case -> workspace link."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE use_cases SET workspace_slug = $2, updated_at = NOW() "
            "WHERE slug = $1 AND workspace_slug IS NULL",
            slug, workspace_slug,
        )


async def get_use_case_by_id(use_case_id: str) -> dict | None:
    """The id-keyed counterpart to get_use_case(slug). Needed because
    workflow_definitions.parent_use_case_id and
    agent_definition_use_case_links.use_case_id both store the id, not the
    slug — get_use_case(slug) stays unchanged for its existing callers."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM use_cases WHERE id = $1", use_case_id)
    return _row_to_dict(row) if row else None
