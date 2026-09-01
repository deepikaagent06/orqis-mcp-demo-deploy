import json
import asyncpg
from functools import lru_cache

from config import get_settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """asyncpg returns json/jsonb columns as raw text by default — every
    service in this codebase reads/writes them as Python list/dict (e.g.
    workflow_runs.step_results, audit_events.metadata), so without this codec
    every JSONB column round-trips as a string instead of structured data."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text",
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        try:
            _pool = await asyncpg.create_pool(
                dsn=get_settings().database_url,
                min_size=2,
                max_size=10,
                init=_init_connection,
            )
        except Exception as exc:
            raise ConnectionError(f"Could not connect to Postgres via DATABASE_URL: {exc}") from exc
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def reset_pool():
    """Close the current pool and clear the global so the next call to
    get_pool() creates a fresh pool in the calling event loop. Used by the
    test suite since each test function runs in its own event loop
    (asyncio_default_test_loop_scope=function); without closing the old pool
    its connections linger and cause 'another operation is in progress' errors
    when the new pool reuses the same Postgres server-side connections."""
    global _pool
    old = _pool
    _pool = None
    if old is not None:
        try:
            await old.close()
        except Exception:
            pass
