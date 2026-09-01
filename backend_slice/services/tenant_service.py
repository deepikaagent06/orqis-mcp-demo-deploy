"""Tenant/user/auth persistence — backed by Postgres (schema.sql: tenants,
users, tenant_workspaces, knowledge_documents, tenant_audit_records,
refresh_tokens). This used to be pure in-memory dicts that reset on every
backend restart, silently discarding every registered company and user even
though the schema and a live asyncpg pool already existed — that was the
single biggest production gap in the app, since every tenant-scoped feature
depends on this data surviving.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from database import get_pool

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name VARCHAR(200) NOT NULL,
    industry VARCHAR(50) NOT NULL DEFAULT 'other',
    country VARCHAR(100) NOT NULL,
    timezone VARCHAR(50) NOT NULL DEFAULT 'UTC',
    logo_url TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(200) NOT NULL,
    password_hash VARCHAR(255),
    role VARCHAR(20) NOT NULL DEFAULT 'viewer',
    auth_provider VARCHAR(20) NOT NULL DEFAULT 'email',
    is_active BOOLEAN NOT NULL DEFAULT true,
    avatar_url TEXT,
    clerk_user_id VARCHAR(255),
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, email)
);

-- Additive for databases created before clerk_user_id existed.
ALTER TABLE users ADD COLUMN IF NOT EXISTS clerk_user_id VARCHAR(255);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_clerk_user_id ON users(clerk_user_id) WHERE clerk_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tenant_use_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    use_case_slug VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, use_case_slug)
);
CREATE INDEX IF NOT EXISTS idx_tenant_use_cases_tenant ON tenant_use_cases(tenant_id);

-- Tenant-specific activation/runtime state for a catalog workspace
-- (services/workspace_service.py's `workspaces` table). Deliberate split,
-- confirmed 2026-08-05: `workspaces` holds shared catalog metadata
-- (name/description/configuration) that's the same for every tenant; this
-- table holds the one thing that legitimately varies per tenant — whether
-- *this* tenant has switched a given workspace_slug on. workspace_service's
-- list/get functions overlay this row onto each catalog row at read time
-- instead of reading workspaces.enabled, and update_workspace() writes
-- `enabled` here (scoped to get_tenant_id()), never to the shared catalog
-- row. See tests/test_workspace_tenant_scoping.py for the isolation this
-- guarantees, and workspace_service.py's module docstring for the full
-- picture.
CREATE TABLE IF NOT EXISTS tenant_workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_slug VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    configuration JSONB DEFAULT '{}',  -- reserved/unused, see workspace_service.py
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, workspace_slug)
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
    category VARCHAR(50) NOT NULL DEFAULT 'general',
    file_size_bytes BIGINT,
    storage_path TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'uploaded',
    uploaded_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_audit_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    event_type VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    workspace_slug VARCHAR(100),
    metadata JSONB DEFAULT '{}',
    ip_address VARCHAR(45),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked BOOLEAN NOT NULL DEFAULT false
);

-- Provider-neutral identity link (replaces the single-provider
-- users.clerk_user_id column for new sign-ins). One users row can have
-- multiple linked identities in the future; today only 'google' is issued.
CREATE TABLE IF NOT EXISTS user_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider VARCHAR(20) NOT NULL,
    provider_user_id VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    email_verified BOOLEAN NOT NULL DEFAULT false,
    raw_profile JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, provider_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_tenant_workspaces_tenant ON tenant_workspaces(tenant_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_tenant ON knowledge_documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON tenant_audit_records(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON tenant_audit_records(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_created ON tenant_audit_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_user_identities_user_id ON user_identities(user_id);
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
    logger.info("Tenant schema ensured (tenants, users, tenant_workspaces, knowledge_documents, tenant_audit_records, refresh_tokens, user_identities)")


def _tenant_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]), "company_name": r["company_name"], "industry": r["industry"],
        "country": r["country"], "timezone": r["timezone"], "logo_url": r["logo_url"],
        "status": r["status"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


def _user_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]), "tenant_id": str(r["tenant_id"]), "email": r["email"], "name": r["name"],
        "password_hash": r["password_hash"], "role": r["role"], "auth_provider": r["auth_provider"],
        "is_active": r["is_active"], "avatar_url": r["avatar_url"], "clerk_user_id": r["clerk_user_id"],
        "last_login": r["last_login"].isoformat() if r["last_login"] else None,
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


def _tenant_use_case_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]), "tenant_id": str(r["tenant_id"]), "use_case_slug": r["use_case_slug"],
        "enabled": r["enabled"], "created_at": r["created_at"].isoformat(),
    }


def _workspace_row_to_dict(r: asyncpg.Record) -> dict:
    import json
    config = r["configuration"]
    if isinstance(config, str):
        config = json.loads(config)
    return {
        "id": str(r["id"]), "tenant_id": str(r["tenant_id"]), "workspace_slug": r["workspace_slug"],
        "enabled": r["enabled"], "configuration": config, "created_at": r["created_at"].isoformat(),
    }


def _knowledge_row_to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": str(r["id"]), "tenant_id": str(r["tenant_id"]), "filename": r["filename"],
        "file_type": r["file_type"], "category": r["category"], "status": r["status"],
        "uploaded_by": str(r["uploaded_by"]) if r["uploaded_by"] else None,
        "created_at": r["created_at"].isoformat(),
    }


def _identity_row_to_dict(r: asyncpg.Record) -> dict:
    import json
    raw_profile = r["raw_profile"]
    if isinstance(raw_profile, str):
        raw_profile = json.loads(raw_profile)
    return {
        "id": str(r["id"]), "user_id": str(r["user_id"]), "provider": r["provider"],
        "provider_user_id": r["provider_user_id"], "email": r["email"], "email_verified": r["email_verified"],
        "raw_profile": raw_profile, "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


def _audit_row_to_dict(r: asyncpg.Record) -> dict:
    import json
    metadata = r["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return {
        "id": str(r["id"]), "tenant_id": str(r["tenant_id"]),
        "user_id": str(r["user_id"]) if r["user_id"] else None,
        "event_type": r["event_type"], "description": r["description"], "workspace_slug": r["workspace_slug"],
        "metadata": metadata, "created_at": r["created_at"].isoformat(),
    }


# ── Tenant CRUD ──────────────────────────────────────────────

async def create_tenant(
    company_name: str,
    industry: str,
    country: str,
    timezone_str: str = "UTC",
    logo_url: Optional[str] = None,
) -> dict:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenants (company_name, industry, country, timezone, logo_url) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            company_name, industry, country, timezone_str, logo_url,
        )
    logger.info("Tenant created: %s (%s)", company_name, row["id"])
    return _tenant_row_to_dict(row)


async def get_tenant(tenant_id: str) -> Optional[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", uuid.UUID(tenant_id))
    return _tenant_row_to_dict(row) if row else None


async def update_tenant(tenant_id: str, updates: dict) -> Optional[dict]:
    await ensure_schema()
    allowed = {"company_name", "industry", "country", "timezone", "logo_url", "status"}
    fields = {k: v for k, v in updates.items() if k in allowed and v is not None}
    if not fields:
        return await get_tenant(tenant_id)

    set_clauses = [f"{k} = ${i + 2}" for i, k in enumerate(fields)]
    set_clauses.append("updated_at = NOW()")
    query = f"UPDATE tenants SET {', '.join(set_clauses)} WHERE id = $1 RETURNING *"

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, uuid.UUID(tenant_id), *fields.values())
    return _tenant_row_to_dict(row) if row else None


async def list_tenants() -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM tenants ORDER BY created_at DESC")
    return [_tenant_row_to_dict(r) for r in rows]


# ── User CRUD ────────────────────────────────────────────────

async def create_user(
    tenant_id: str,
    email: str,
    name: str,
    role: str = "viewer",
    auth_provider: str = "email",
    password_hash: Optional[str] = None,
    avatar_url: Optional[str] = None,
    clerk_user_id: Optional[str] = None,
) -> dict:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (tenant_id, email, name, role, auth_provider, password_hash, avatar_url, clerk_user_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
            uuid.UUID(tenant_id), email, name, role, auth_provider, password_hash, avatar_url, clerk_user_id,
        )
    logger.info("User created: %s (%s) for tenant %s", email, row["id"], tenant_id)
    return _user_row_to_dict(row)


async def get_user_by_identity(provider: str, provider_user_id: str) -> Optional[dict]:
    """Resolve a provider-neutral identity (see user_identities) to its
    linked users row. Returns None if no user has linked this identity yet
    — callers must treat that as "authenticated, not onboarded"."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT u.* FROM users u JOIN user_identities i ON i.user_id = u.id "
            "WHERE i.provider = $1 AND i.provider_user_id = $2",
            provider, provider_user_id,
        )
    return _user_row_to_dict(row) if row else None


async def get_identity_provider_user_id(user_id: str, provider: str) -> Optional[str]:
    """The provider-side id (e.g. Google 'sub') linked to this user — used
    to re-issue a session token that resolves the same way on the next
    request. Returns None if this user has no identity for that provider."""
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT provider_user_id FROM user_identities WHERE user_id = $1 AND provider = $2 LIMIT 1",
            uuid.UUID(user_id), provider,
        )
    return row["provider_user_id"] if row else None


async def link_identity(
    user_id: str,
    provider: str,
    provider_user_id: str,
    email: str,
    email_verified: bool = True,
    raw_profile: Optional[dict] = None,
) -> dict:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO user_identities (user_id, provider, provider_user_id, email, email_verified, raw_profile) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            uuid.UUID(user_id), provider, provider_user_id, email, email_verified, raw_profile or {},
        )
    return _identity_row_to_dict(row)


async def get_user_by_email(email: str, tenant_id: Optional[str] = None) -> Optional[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if tenant_id:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE email = $1 AND tenant_id = $2", email, uuid.UUID(tenant_id)
            )
        else:
            row = await conn.fetchrow("SELECT * FROM users WHERE email = $1 ORDER BY created_at LIMIT 1", email)
    return _user_row_to_dict(row) if row else None


async def get_user(user_id: str) -> Optional[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", uuid.UUID(user_id))
    return _user_row_to_dict(row) if row else None


async def update_user_login(user_id: str) -> None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_login = NOW() WHERE id = $1", uuid.UUID(user_id))


async def list_users(tenant_id: str) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users WHERE tenant_id = $1 ORDER BY created_at", uuid.UUID(tenant_id))
    return [_user_row_to_dict(r) for r in rows]


# ── Tenant Workspaces ────────────────────────────────────────

# Fixed namespace for deriving a stable UUID from a non-UUID tenant_id (see
# _resolve_tenant_uuid). Arbitrary but must never change — changing it would
# silently orphan every previously-derived demo/legacy tenant row.
_LEGACY_TENANT_NAMESPACE = uuid.UUID("c3f6a4b2-2f36-4f0a-8f0a-6a4b2c3f6a4b")


async def _resolve_tenant_uuid(tenant_id: str) -> uuid.UUID:
    """Resolve a tenant_id to the UUID `tenants.id` this table's FK requires.

    Real onboarded tenants (created via create_tenant() /
    create_tenant_with_admin_user()) already carry a genuine UUID and are
    returned unchanged. Some ambient tenant identifiers are plain strings
    instead — e.g. middleware/auth.py's dev-mode DEMO_TENANT_ID and
    middleware/tenant_context.py's MARKETPLACE_TENANT_ID, both "demo-tenant",
    the same string-tenant_id convention used by every other tenant-scoped
    table in this app (VARCHAR(100) DEFAULT 'demo-tenant'). Those aren't rows
    in `tenants`, so uuid.UUID() on them raises ValueError. For those,
    deterministically derive a stable UUID (uuid5 — the same string always
    maps to the same UUID) and lazily ensure a matching `tenants` row exists
    so this table's FK is satisfiable, without inventing a second tenant ID
    system or touching the schema.
    """
    try:
        return uuid.UUID(tenant_id)
    except ValueError:
        pass

    resolved = uuid.uuid5(_LEGACY_TENANT_NAMESPACE, tenant_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, company_name, industry, country) VALUES ($1, $2, 'other', 'Unknown') "
            "ON CONFLICT (id) DO NOTHING",
            resolved, tenant_id,
        )
    return resolved


async def enable_workspaces(tenant_id: str, workspace_slugs: list[str]) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    records = []
    async with pool.acquire() as conn:
        for slug in workspace_slugs:
            row = await conn.fetchrow(
                "INSERT INTO tenant_workspaces (tenant_id, workspace_slug, enabled) VALUES ($1, $2, true) "
                "ON CONFLICT (tenant_id, workspace_slug) DO UPDATE SET enabled = true RETURNING *",
                uuid.UUID(tenant_id), slug,
            )
            records.append(_workspace_row_to_dict(row))
    return records


async def get_tenant_workspaces(tenant_id: str) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    tenant_uuid = await _resolve_tenant_uuid(tenant_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tenant_workspaces WHERE tenant_id = $1 ORDER BY created_at", tenant_uuid
        )
    return [_workspace_row_to_dict(r) for r in rows]


async def set_tenant_workspace_enabled(tenant_id: str, workspace_slug: str, enabled: bool) -> dict:
    """Upsert this tenant's own enablement flag for one catalog workspace.

    Scoped strictly to `tenant_id` — never touches another tenant's row, so
    toggling a workspace on/off for one tenant can never affect any other
    tenant's `tenant_workspaces` entry for the same catalog slug.
    """
    await ensure_schema()
    pool = await get_pool()
    tenant_uuid = await _resolve_tenant_uuid(tenant_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenant_workspaces (tenant_id, workspace_slug, enabled) VALUES ($1, $2, $3) "
            "ON CONFLICT (tenant_id, workspace_slug) DO UPDATE SET enabled = $3 RETURNING *",
            tenant_uuid, workspace_slug, enabled,
        )
    return _workspace_row_to_dict(row)


# ── Tenant Use Cases (selections against services.use_case_service's ────
# canonical catalog) ──────────────────────────────────────────

async def enable_tenant_use_cases(tenant_id: str, use_case_slugs: list[str]) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    records = []
    async with pool.acquire() as conn:
        for slug in use_case_slugs:
            row = await conn.fetchrow(
                "INSERT INTO tenant_use_cases (tenant_id, use_case_slug, enabled) VALUES ($1, $2, true) "
                "ON CONFLICT (tenant_id, use_case_slug) DO UPDATE SET enabled = true RETURNING *",
                uuid.UUID(tenant_id), slug,
            )
            records.append(_tenant_use_case_row_to_dict(row))
    return records


async def get_tenant_use_cases(tenant_id: str) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tenant_use_cases WHERE tenant_id = $1 ORDER BY created_at", uuid.UUID(tenant_id)
        )
    return [_tenant_use_case_row_to_dict(r) for r in rows]


# ── Knowledge Documents ──────────────────────────────────────

async def add_knowledge_document(
    tenant_id: str,
    filename: str,
    file_type: str,
    category: str = "general",
    uploaded_by: Optional[str] = None,
) -> dict:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO knowledge_documents (tenant_id, filename, file_type, category, uploaded_by) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            uuid.UUID(tenant_id), filename, file_type, category,
            uuid.UUID(uploaded_by) if uploaded_by else None,
        )
    return _knowledge_row_to_dict(row)


async def get_knowledge_documents(tenant_id: str) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM knowledge_documents WHERE tenant_id = $1 ORDER BY created_at DESC", uuid.UUID(tenant_id)
        )
    return [_knowledge_row_to_dict(r) for r in rows]


# ── Audit Records ────────────────────────────────────────────

async def create_audit_record(
    tenant_id: str,
    event_type: str,
    description: str,
    user_id: Optional[str] = None,
    workspace_slug: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenant_audit_records (tenant_id, user_id, event_type, description, workspace_slug, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
            uuid.UUID(tenant_id), uuid.UUID(user_id) if user_id else None, event_type, description,
            workspace_slug, metadata or {},
        )
    return _audit_row_to_dict(row)


async def get_audit_records(tenant_id: str, limit: int = 50) -> list[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tenant_audit_records WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2",
            uuid.UUID(tenant_id), limit,
        )
    return [_audit_row_to_dict(r) for r in rows]


# ── Refresh Tokens ───────────────────────────────────────────

async def store_refresh_token(user_id: str, token_hash: str, expires_at: str) -> None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (token_hash) DO NOTHING",
            uuid.UUID(user_id), token_hash, datetime.fromisoformat(expires_at),
        )


async def get_refresh_token(token_hash: str) -> Optional[dict]:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM refresh_tokens WHERE token_hash = $1", token_hash)
    if not row:
        return None
    return {
        "user_id": str(row["user_id"]), "token_hash": row["token_hash"],
        "expires_at": row["expires_at"].isoformat(), "revoked": row["revoked"],
    }


async def revoke_refresh_token(token_hash: str) -> None:
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE refresh_tokens SET revoked = true WHERE token_hash = $1", token_hash)


# ── Onboarding: atomic tenant + admin user creation ───────────

async def create_tenant_with_admin_user(
    *,
    company_name: str,
    industry: str,
    country: str,
    timezone_str: str,
    admin_email: str,
    admin_name: str,
    provider: str,
    provider_user_id: str,
    use_case_slugs: list[str],
    workspace_slugs: list[str],
) -> tuple[dict, dict]:
    """Create a tenant, link the first (admin) user to it via a
    provider-neutral identity (user_identities), enable the chosen use
    cases/workspaces, and write an audit record — all in one transaction,
    so a failure partway through (e.g. a bad slug) can't leave an orphan
    tenant with no linked user.
    """
    import json

    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            tenant_row = await conn.fetchrow(
                "INSERT INTO tenants (company_name, industry, country, timezone) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                company_name, industry, country, timezone_str,
            )
            user_row = await conn.fetchrow(
                "INSERT INTO users (tenant_id, email, name, role, auth_provider) "
                "VALUES ($1, $2, $3, 'admin', $4) RETURNING *",
                tenant_row["id"], admin_email, admin_name, provider,
            )
            await conn.execute(
                "INSERT INTO user_identities (user_id, provider, provider_user_id, email, email_verified) "
                "VALUES ($1, $2, $3, $4, true)",
                user_row["id"], provider, provider_user_id, admin_email,
            )

            for slug in use_case_slugs:
                await conn.execute(
                    "INSERT INTO tenant_use_cases (tenant_id, use_case_slug, enabled) VALUES ($1, $2, true) "
                    "ON CONFLICT (tenant_id, use_case_slug) DO UPDATE SET enabled = true",
                    tenant_row["id"], slug,
                )
            for slug in workspace_slugs:
                await conn.execute(
                    "INSERT INTO tenant_workspaces (tenant_id, workspace_slug, enabled) VALUES ($1, $2, true) "
                    "ON CONFLICT (tenant_id, workspace_slug) DO UPDATE SET enabled = true",
                    tenant_row["id"], slug,
                )

            await conn.execute(
                "INSERT INTO tenant_audit_records (tenant_id, user_id, event_type, description, metadata) "
                "VALUES ($1, $2, $3, $4, $5)",
                tenant_row["id"], user_row["id"], "company_onboarded",
                f"Company '{company_name}' onboarded by {admin_email}",
                json.dumps({"industry": industry, "country": country, "use_cases": use_case_slugs, "workspaces": workspace_slugs}),
            )

    logger.info("Tenant onboarded via %s link: %s (%s) -> user %s", provider, company_name, tenant_row["id"], user_row["id"])
    return _tenant_row_to_dict(tenant_row), _user_row_to_dict(user_row)
