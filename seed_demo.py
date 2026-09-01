"""One-shot, idempotent synthetic data seed for the orqis-mcp Glama demo.

Run once by entrypoint.sh after the in-container Postgres is up and before
the MCP server starts. Uses only the real backend_slice service functions
(ensure_schema()/create_*()/list_*()) — no hand-written SQL — so seeded rows
go through exactly the same validation and Knowledge Context Layer
projection real callers get. Every check-before-insert below makes re-running
this script against an already-seeded database a no-op, so a container
restart (same Postgres data dir) never creates duplicate rows.

All data here is synthetic ("Northwind Retail", fictional). No real ORQIS
customer, tenant, or company data is read or written by this script.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from database import get_pool
from middleware.tenant_context import reset_tenant_id, set_tenant_id
from services import (
    agent_definition_service,
    agent_memory_service,
    knowledge_context_service,
    knowledge_ledger_service,
    knowledge_temporal_service,
    runs_service,
    tenant_service,
    use_case_service,
    workspace_service,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_demo")

DEMO_TENANT_ID = os.environ.get("DEMO_TENANT_ID", "demo-tenant-pullak")

_ENTITY_TYPE = "policy"
_ENTITY_REF = "refund-window-policy"


async def _ensure_schemas() -> None:
    # Each call is independently idempotent (CREATE TABLE IF NOT EXISTS /
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS). knowledge_context_service's
    # ensure_schema() also ensures knowledge_ledger_service's schema
    # (dependency order), which is what get_temporal_knowledge needs for its
    # knowledge_citations reads to succeed on a brand-new database.
    await workspace_service.ensure_schema()
    await use_case_service.ensure_schema()
    await tenant_service.ensure_schema()
    await runs_service.ensure_schema()
    await agent_definition_service.ensure_schema()
    await agent_memory_service.ensure_schema()
    await knowledge_context_service.ensure_schema()
    logger.info("All required schemas ensured")


async def _seed_workspaces_and_use_cases() -> None:
    # workspace_service/use_case_service self-seed their shared, tenant-less
    # catalogs the first time list_*() runs against an empty table — nothing
    # to do here but trigger that path and enable a couple of workspaces for
    # our demo tenant so list_workspaces() shows enabled=True with run stats.
    await workspace_service.list_workspaces()
    await use_case_service.list_use_cases()

    enabled = await tenant_service.get_tenant_workspaces(DEMO_TENANT_ID)
    if not enabled:
        # enable_workspaces() requires a real UUID tenant_id (onboarded
        # tenants only); our synthetic string tenant_id needs
        # set_tenant_workspace_enabled(), which resolves a non-UUID
        # tenant_id via _resolve_tenant_uuid() instead.
        for slug in ("customer-winback", "revenue-leakage"):
            await tenant_service.set_tenant_workspace_enabled(DEMO_TENANT_ID, slug, True)
        logger.info("Enabled customer-winback + revenue-leakage for %s", DEMO_TENANT_ID)

    existing_runs = await runs_service.list_runs_for_workspace("customer-winback")
    if not existing_runs:
        run = await runs_service.create_run(
            run_id="run-demo-pullak-001", workspace_id="ws-2", workspace_slug="customer-winback",
            nodes_snapshot=[{"id": "n1", "label": "Data Intelligence"}, {"id": "n2", "label": "Customer Intelligence"}],
        )
        await runs_service.append_step_result(
            run["id"], {"step": "Data Intelligence", "summary": "Synthetic churn dataset parsed (240 accounts)."},
            tokens=1200, cost=0.04,
        )
        await runs_service.set_status(run["id"], "completed", completed=True)
        logger.info("Seeded one demo workflow_runs row for customer-winback")


async def _seed_agents() -> dict[str, dict]:
    existing = await agent_definition_service.list_definitions()
    by_name = {a["name"]: a for a in existing}

    if "Customer Winback Analyst (Demo)" not in by_name:
        by_name["Customer Winback Analyst (Demo)"] = await agent_definition_service.create_definition(
            name="Customer Winback Analyst (Demo)",
            description="Synthetic demo agent: analyzes churn signals for Northwind Retail and drafts winback offers.",
            instructions=(
                "You are a demo-only agent. Given a synthetic list of churned Northwind Retail accounts, "
                "identify the top winback candidates and propose a retention offer for each."
            ),
            capability_ids=["cap-cust-intel"],
            memory_enabled=True,
            temporal_knowledge_enabled=True,
            created_by="seed_demo",
        )
        logger.info("Created AgentDefinition: Customer Winback Analyst (Demo)")

    if "Revenue Leakage Investigator (Demo)" not in by_name:
        by_name["Revenue Leakage Investigator (Demo)"] = await agent_definition_service.create_definition(
            name="Revenue Leakage Investigator (Demo)",
            description="Synthetic demo agent: detects revenue leakage patterns in Northwind Retail's synthetic billing data.",
            instructions=(
                "You are a demo-only agent. Given synthetic Northwind Retail billing records, quantify revenue "
                "leakage and recommend a recovery action for each pattern found."
            ),
            capability_ids=["cap-rev-analysis"],
            memory_enabled=True,
            temporal_knowledge_enabled=False,
            created_by="seed_demo",
        )
        logger.info("Created AgentDefinition: Revenue Leakage Investigator (Demo)")

    return by_name


async def _seed_shared_memory(agents_by_name: dict[str, dict]) -> None:
    existing = await agent_memory_service.list_shared_memory()
    if existing:
        return

    winback = agents_by_name["Customer Winback Analyst (Demo)"]
    revenue = agents_by_name["Revenue Leakage Investigator (Demo)"]

    await agent_memory_service.write_semantic_memory(
        winback["id"],
        content=(
            "Synthetic learning: Northwind Retail customers who churn within 14 days of a support ticket "
            "respond best to a 20% loyalty discount rather than a free-shipping offer."
        ),
        scope="shared",
        metadata={"source": "seed_demo", "company": "Northwind Retail (synthetic)"},
        created_by="seed_demo",
    )
    await agent_memory_service.write_semantic_memory(
        revenue["id"],
        content=(
            "Synthetic learning: Northwind Retail's most common leakage pattern is duplicate discount codes "
            "applied at checkout — flag any order with more than one active promo code."
        ),
        scope="shared",
        metadata={"source": "seed_demo", "company": "Northwind Retail (synthetic)"},
        created_by="seed_demo",
    )
    logger.info("Seeded 2 shared agent_memory_entries rows")


async def _seed_temporal_knowledge() -> None:
    history = await knowledge_temporal_service.get_version_history(_ENTITY_TYPE, _ENTITY_REF)
    if history:
        return

    now = datetime.now(timezone.utc)
    entity_ref = {"entity_type": _ENTITY_TYPE, "entity_id": _ENTITY_REF, "name": "Refund Window Policy (Synthetic)"}

    v1 = await knowledge_ledger_service.create_entry(
        title="Refund Window Policy v1 (Synthetic)",
        content="Northwind Retail's standard refund window is 30 days from purchase date.",
        entity_refs=[entity_ref],
        uploaded_by="seed_demo",
        effective_from=now - timedelta(days=180),
    )
    v2 = await knowledge_ledger_service.create_entry(
        title="Refund Window Policy v2 (Synthetic)",
        content="Northwind Retail's refund window was extended to 45 days from purchase date for Premium-tier customers.",
        entity_refs=[entity_ref],
        uploaded_by="seed_demo",
        effective_from=now - timedelta(days=30),
    )
    await knowledge_ledger_service.mark_superseded(v1["id"], v2["id"])
    logger.info("Seeded 2-version temporal knowledge_ledger_entries timeline for %s/%s", _ENTITY_TYPE, _ENTITY_REF)


async def main() -> None:
    await _ensure_schemas()

    token = set_tenant_id(DEMO_TENANT_ID)
    try:
        await _seed_workspaces_and_use_cases()
        agents_by_name = await _seed_agents()
        await _seed_shared_memory(agents_by_name)
        await _seed_temporal_knowledge()
    finally:
        reset_tenant_id(token)

    pool = await get_pool()
    await pool.close()
    logger.info("Seed complete for tenant_id=%s", DEMO_TENANT_ID)


if __name__ == "__main__":
    asyncio.run(main())
