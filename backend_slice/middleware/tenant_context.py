"""Ambient tenant context for the current request.

SessionAuthMiddleware (middleware/auth.py) resolves the caller's tenant_id
once per request and stores it here via a contextvar rather than passing it
as an explicit argument through every function call. Python contextvars are
copied into any task spawned from the current context (asyncio.create_task,
FastAPI BackgroundTasks, Starlette's StreamingResponse generators), so this
propagates correctly into services/pipeline_executor.py, background workflow
execution, and the platform/ modules (intent-engine, capability-router,
action-engine) without requiring any changes to those call sites.

Every database-backed service function that reads or writes tenant-scoped
data calls get_tenant_id() itself and filters by it — the boundary can't be
bypassed by a caller forgetting to pass a tenant_id, because there is no
tenant_id parameter to forget.
"""
import contextvars

from fastapi import HTTPException

# Pre-existing convention (see scripts/seed_northstar_commerce.py's module
# docstring): rows tagged tenant_id="demo-tenant" in agent_definitions and
# knowledge_base_articles are the shared Marketplace Registry catalog —
# system/seed-owned components meant to be visible to every tenant, not
# private data scoped to one caller. Read paths that back the Marketplace
# Components listing include this alongside the caller's own tenant_id;
# write paths (create/update/delete) never do, so a tenant can browse but
# never mutate registry content it doesn't own.
MARKETPLACE_TENANT_ID = "demo-tenant"

_tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)


def set_tenant_id(tenant_id: str) -> contextvars.Token:
    return _tenant_id_var.set(tenant_id)


def reset_tenant_id(token: contextvars.Token) -> None:
    _tenant_id_var.reset(token)


def get_tenant_id() -> str:
    """Return the tenant_id for the currently executing request.

    Raises 401 if called outside a request that passed through
    SessionAuthMiddleware (e.g. a public path, or a code path invoked without
    an HTTP request context) — there is no default tenant to silently fall
    back to.
    """
    tenant_id = _tenant_id_var.get()
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return tenant_id
