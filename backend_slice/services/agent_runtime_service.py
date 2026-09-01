"""Agent Runtime view — derives "AI agent" activity from the real execution
record (workflow_runs.step_results via runs_service, plus ingestion_jobs via
ingestion_service) instead of maintaining a second, parallel, hand-seeded
list of fake runs.

The 6 agent roles below (Knowledge/Policy/Runtime/Audit/Executive/Workflow)
are a fixed internal catalog — like services/capability_catalog.py — not
tenant data, so they stay as a code-level constant. What used to be fake is
the *activity* attributed to them (current_runs, total_runs, success_rate,
tokens_used_total, and the entire run history) — that's now computed live
from the same Postgres rows the Run History and Knowledge Engine pages read.
"""
from datetime import datetime, timezone

from services import ingestion_service, runs_service
from services.capability_catalog import get_capability

AGENTS: list[dict] = [
    {"id": "agent_knowledge", "name": "Knowledge Agent", "type": "knowledge",
     "description": "Processes documents through the ingestion pipeline — parse, chunk, extract metadata, index",
     "capabilities": ["ingest", "chunk", "extract_metadata", "index"]},
    {"id": "agent_policy", "name": "Policy Agent", "type": "policy",
     "description": "Validates outputs against compliance rules and organizational policies",
     "capabilities": ["validate", "compliance_check"]},
    {"id": "agent_runtime", "name": "Runtime Agent", "type": "runtime",
     "description": "Executes core and specialist capability steps in workflow runs",
     "capabilities": ["execute_step"]},
    {"id": "agent_audit", "name": "Audit Agent", "type": "audit",
     "description": "Records decisions, generates audit trails, and tracks evidence",
     "capabilities": ["record_decision", "generate_trail", "track_evidence"]},
    {"id": "agent_executive", "name": "Executive Agent", "type": "executive",
     "description": "Generates executive summaries and dashboard intelligence",
     "capabilities": ["summarize", "aggregate"]},
    {"id": "agent_workflow", "name": "Workflow Agent", "type": "workflow",
     "description": "Manages human-review gates and step sequencing",
     "capabilities": ["sequence", "gate"]},
]
AGENTS_BY_ID = {a["id"]: a for a in AGENTS}
AGENTS_BY_TYPE = {a["type"]: a for a in AGENTS}

# Maps each real capability_id (services/capability_catalog.py) to the agent
# role that owns it, so a real workflow step result can be attributed to one
# of the 6 roles above without a second hardcoded data set.
_CAPABILITY_TO_AGENT_TYPE: dict[str, str] = {
    "cap-doc-intel": "knowledge", "cap-data-intel": "knowledge",
    "cap-policy-extract": "policy", "cap-control-map": "policy", "cap-compliance-check": "policy", "cap-policy-valid": "policy",
    "cap-gap-detect": "runtime", "cap-risk-assess": "runtime", "cap-recommend": "runtime",
    "cap-cust-intel": "runtime", "cap-rev-analysis": "runtime", "cap-csat-analyzer": "runtime", "cap-offer-strategy": "runtime",
    "cap-audit-log": "audit", "cap-decision-rec": "audit",
    "cap-exec-intel": "executive",
    "cap-human-approve": "workflow", "cap-human-review": "workflow", "cap-escalation": "workflow",
}


def _agent_type_for_capability(capability_id: str) -> str:
    return _CAPABILITY_TO_AGENT_TYPE.get(capability_id, "runtime")


async def _derive_runs() -> list[dict]:
    """One synthesized AgentRun per real step_result (workflow execution)
    plus one per ingestion job — each sourced directly from a real row, not
    invented. Steps are single-element since that's the real granularity we
    have; the former seed data's fake parse_document/chunk_content/... sub
    steps had no backing execution to report."""
    runs: list[dict] = []

    all_runs = await runs_service.list_all_runs()
    for run in all_runs:
        for step in run.get("step_results", []):
            cap_id = step.get("capability_id", "")
            agent_type = _agent_type_for_capability(cap_id)
            agent = AGENTS_BY_TYPE[agent_type]
            status = "completed" if step.get("status") == "completed" else "failed"
            runs.append({
                "id": f"step-{run['id']}-{step.get('node_id', '')}",
                "agent_id": agent["id"], "agent_type": agent_type, "agent_name": agent["name"],
                "status": status, "trigger": "workflow_run", "workspace": run["workspace_slug"],
                "capability": step.get("capability_name"), "description": f"{step.get('capability_name')} — {run['workspace_slug']}",
                "progress_percent": 100.0 if status == "completed" else 0.0,
                "tokens_used": step.get("tokens", 0), "token_budget": step.get("tokens", 0),
                "duration_ms": 0, "steps": [], "result": step.get("output_text"), "error": step.get("error"),
                "priority": "normal", "created_at": step.get("started_at") or run["started_at"],
                "started_at": step.get("started_at"), "completed_at": step.get("completed_at"),
            })
        if run["status"] == "running":
            runs.append({
                "id": f"run-{run['id']}", "agent_id": "agent_runtime", "agent_type": "runtime", "agent_name": "Runtime Agent",
                "status": "running", "trigger": "workflow_run", "workspace": run["workspace_slug"],
                "capability": None, "description": f"Executing workflow — {run['workspace_slug']}",
                "progress_percent": (run["steps_completed"] / run["steps_total"] * 100) if run["steps_total"] else 0.0,
                "tokens_used": run["tokens_used"], "token_budget": 4096, "duration_ms": 0, "steps": [],
                "result": None, "error": None, "priority": "normal", "created_at": run["started_at"],
                "started_at": run["started_at"], "completed_at": None,
            })

    jobs = await ingestion_service.list_jobs()
    knowledge_agent = AGENTS_BY_TYPE["knowledge"]
    for job in jobs:
        status_map = {"ready": "completed", "failed": "failed"}
        status = status_map.get(job["status"], "running" if job["status"] != "queued" else "queued")
        runs.append({
            "id": f"ingest-{job['id']}", "agent_id": knowledge_agent["id"], "agent_type": "knowledge",
            "agent_name": knowledge_agent["name"], "status": status, "trigger": "document_upload",
            "workspace": None, "capability": "ingest", "description": f"Processing document: {job['file_name']}",
            "progress_percent": 100.0 if status == "completed" else (0.0 if status == "queued" else 50.0),
            "tokens_used": 0, "token_budget": 4096, "duration_ms": 0, "steps": [],
            "result": job.get("summary_generated"), "error": job.get("error_message"), "priority": "normal",
            "created_at": job["created_at"], "started_at": job["created_at"], "completed_at": job.get("completed_at"),
        })

    runs.sort(key=lambda r: r["created_at"], reverse=True)
    return runs


async def list_agents() -> list[dict]:
    runs = await _derive_runs()
    result = []
    for agent in AGENTS:
        agent_runs = [r for r in runs if r["agent_id"] == agent["id"]]
        completed = [r for r in agent_runs if r["status"] == "completed"]
        failed = [r for r in agent_runs if r["status"] == "failed"]
        active = [r for r in agent_runs if r["status"] in ("running", "queued")]
        total = len(completed) + len(failed)
        last_active = agent_runs[0]["created_at"] if agent_runs else None
        result.append({
            **agent,
            "status": "active" if active else "idle",
            "current_runs": len(active),
            "total_runs": len(agent_runs),
            "success_rate": round(len(completed) / total, 3) if total else 0.0,
            "avg_duration_ms": 0,
            "tokens_used_total": sum(r["tokens_used"] for r in agent_runs),
            "last_active_at": last_active,
            "created_at": "2026-01-01T00:00:00Z",
        })
    return result


async def get_agent(agent_id: str) -> dict | None:
    agents = await list_agents()
    return next((a for a in agents if a["id"] == agent_id), None)


async def list_runs(*, status=None, agent_type=None, capability=None, workspace=None, limit=20, offset=0) -> tuple[list[dict], int]:
    runs = await _derive_runs()
    if status:
        runs = [r for r in runs if r["status"] == status]
    if agent_type:
        runs = [r for r in runs if r["agent_type"] == agent_type]
    if capability:
        runs = [r for r in runs if r["capability"] == capability]
    if workspace:
        runs = [r for r in runs if r["workspace"] == workspace]
    total = len(runs)
    return runs[offset : offset + limit], total


async def get_run(run_id: str) -> dict | None:
    runs = await _derive_runs()
    return next((r for r in runs if r["id"] == run_id), None)


async def get_stats() -> dict:
    runs = await _derive_runs()
    agents = await list_agents()
    completed = [r for r in runs if r["status"] == "completed"]
    failed = [r for r in runs if r["status"] == "failed"]
    running = [r for r in runs if r["status"] == "running"]
    queued = [r for r in runs if r["status"] == "queued"]
    total = len(completed) + len(failed)
    return {
        "total_agents": len(agents),
        "active_agents": len([a for a in agents if a["status"] == "active"]),
        "total_runs": len(runs),
        "running_runs": len(running),
        "queued_runs": len(queued),
        "completed_runs": len(completed),
        "failed_runs": len(failed),
        "success_rate": len(completed) / total if total else 0.0,
        "avg_duration_ms": 0.0,
        "total_tokens_used": sum(r["tokens_used"] for r in runs),
    }
