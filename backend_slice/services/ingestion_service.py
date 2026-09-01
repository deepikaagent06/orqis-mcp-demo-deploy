"""Ingestion job persistence — backed by Postgres. Replaces the in-memory
INGESTION_STORE dict in routers/ingestion.py, which reset on every backend
restart and was also being read directly by routers/cxo.py (a second module
reaching into the dict) — that direct access is now a function call into
this module instead.

The job record is stored as JSONB (`data`) since its shape (pipeline_stages,
entities_extracted, chunks) is already a nested structure with no relational
need to normalize further — only the columns this module actually queries by
(status, created_at) are pulled out for indexing.

Note: chunking and entity extraction here are real deterministic algorithms
(recursive-character splitting, regex-based entity matching), not hardcoded
fake data — but they don't call a real embedding API, so `vector_count` and
`embedding_model` describe an embedding step that doesn't actually run
against OpenAI. That gap is disclosed in the production-readiness report
rather than silently left implied as real.
"""
import json
import logging
from datetime import datetime, timezone

import asyncpg

from database import get_pool
from middleware.tenant_context import get_tenant_id

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id VARCHAR(50) PRIMARY KEY,
    tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant',
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    file_name VARCHAR(500) NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status ON ingestion_jobs(status);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_created ON ingestion_jobs(created_at DESC);
"""

_MIGRATE_SQL = """
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(100) NOT NULL DEFAULT 'demo-tenant';
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_tenant ON ingestion_jobs(tenant_id);
"""

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
    logger.info("Ingestion schema ensured (ingestion_jobs)")


def _row_to_job(r: asyncpg.Record) -> dict:
    data = r["data"]
    return json.loads(data) if isinstance(data, str) else dict(data)


async def _seed_if_empty(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM ingestion_jobs")
    if count:
        return
    # Import here (not at module load) to avoid a circular import between
    # ingestion_service and routers.ingestion at process startup.
    from routers.ingestion import _SEED_JOBS
    for job in _SEED_JOBS:
        await conn.execute(
            "INSERT INTO ingestion_jobs (id, tenant_id, status, file_name, data, created_at, updated_at) "
            "VALUES ($1, 'demo-tenant', $2, $3, $4, $5, $6)",
            job["id"], job["status"], job["file_name"], job,
            datetime.fromisoformat(job["created_at"]), datetime.fromisoformat(job["updated_at"]),
        )
    logger.info("Ingestion jobs seeded with %d entries", len(_SEED_JOBS))


async def list_jobs() -> list[dict]:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM ingestion_jobs WHERE tenant_id = $1 ORDER BY created_at DESC", tenant_id
        )
    return [_row_to_job(r) for r in rows]


async def get_job(job_id: str) -> dict | None:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ingestion_jobs WHERE id = $1 AND tenant_id = $2", job_id, tenant_id
        )
    return _row_to_job(row) if row else None


async def save_job(job: dict) -> dict:
    """Insert or fully replace a job record."""
    await ensure_schema()
    tenant_id = get_tenant_id()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ingestion_jobs (id, tenant_id, status, file_name, data, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $6) "
            "ON CONFLICT (id) DO UPDATE SET status = $3, data = $5, updated_at = $6 "
            "WHERE ingestion_jobs.tenant_id = $2",
            job["id"], tenant_id, job["status"], job["file_name"], job, now,
        )
    return job


async def delete_job(job_id: str) -> bool:
    await ensure_schema()
    tenant_id = get_tenant_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM ingestion_jobs WHERE id = $1 AND tenant_id = $2", job_id, tenant_id
        )
    return result != "DELETE 0"
