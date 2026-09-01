# orqis-mcp Glama DEMO image — one container, self-contained.
#
# Runs a local, isolated PostgreSQL instance INSIDE this container plus the
# unmodified orqis-mcp MCP server (streamable-http). It never talks to any
# external database: DATABASE_URL is generated at container startup by
# entrypoint.sh and always points at 127.0.0.1 — an externally supplied
# DATABASE_URL is intentionally ignored (see entrypoint.sh). No real ORQIS
# .env, secrets, or credentials are copied into this image — only the
# synthetic seed data in seed_demo.py.
#
# Copies only the backend_slice/ subset of ORQIS/backend proven necessary by
# the orqis-mcp five-tool import-graph investigation — not the full backend.
FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql postgresql-contrib curl gosu && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ORQIS_BACKEND_PATH for orqis-mcp's _bootstrap_backend_path() — this slice
# is the entire "backend checkout" orqis-mcp ever sees.
COPY backend_slice/ /app/backend_slice/

# The unmodified standalone orqis-mcp package.
COPY orqis-mcp/pyproject.toml orqis-mcp/README.md orqis-mcp/LICENSE /app/orqis-mcp/
COPY orqis-mcp/src /app/orqis-mcp/src

COPY requirements-minimal.txt /app/requirements-minimal.txt
RUN pip install --no-cache-dir -r /app/requirements-minimal.txt /app/orqis-mcp

COPY seed_demo.py /app/seed_demo.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV ORQIS_BACKEND_PATH=/app/backend_slice \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8001 \
    DEMO_TENANT_ID=demo-tenant-pullak \
    PGDATA=/var/lib/postgresql/data/orqis_demo \
    DEMO_PG_USER=orqis_demo \
    DEMO_PG_PASSWORD=orqis_demo_local_only \
    DEMO_PG_DB=orqis_mcp_demo

RUN adduser --system --uid 1001 orqismcp

# Internal only — no host port is published by default; a Glama/Docker
# deployment maps 8001 to the outside as needed. 5432 (Postgres) is never
# exposed and only ever bound to 127.0.0.1 inside this container (see
# entrypoint.sh) — nothing outside the container can reach it.
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f http://127.0.0.1:8001/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
