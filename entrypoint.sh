#!/bin/bash
# Container entrypoint: start an isolated, local-only PostgreSQL instance,
# create the demo role/database if missing, run the idempotent synthetic
# seed, then exec the orqis-mcp server (streamable-http) as a non-root user.
#
# Isolation guarantee (see README.md "Security / isolation" section):
# DATABASE_URL is ALWAYS computed here from DEMO_PG_* and 127.0.0.1 — any
# DATABASE_URL passed into the container from outside is ignored/overwritten
# below, so this image can never be pointed at an external (or the real
# ORQIS) Postgres instance.
set -euo pipefail

PG_BIN="$(dirname "$(find /usr/lib/postgresql -maxdepth 3 -name pg_ctl 2>/dev/null | head -1)")"
export PATH="${PG_BIN}:${PATH}"

mkdir -p "$(dirname "$PGDATA")"
chown -R postgres:postgres "$(dirname "$PGDATA")"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  echo "[entrypoint] Initializing local demo Postgres data directory at ${PGDATA}"
  echo "${DEMO_PG_PASSWORD}" > /tmp/pg_su_pwfile
  gosu postgres initdb -D "$PGDATA" -U postgres --auth-local=peer --auth-host=scram-sha-256 --pwfile=/tmp/pg_su_pwfile
  rm -f /tmp/pg_su_pwfile
  # Never listen on anything but loopback -- this Postgres is reachable only
  # from inside this container, on its own private network namespace.
  echo "listen_addresses = '127.0.0.1'" >> "${PGDATA}/postgresql.conf"
fi

echo "[entrypoint] Starting local demo Postgres"
gosu postgres pg_ctl -D "$PGDATA" -l /tmp/postgres.log -w start

echo "[entrypoint] Ensuring demo role/database exist (idempotent)"
gosu postgres psql -v ON_ERROR_STOP=1 --username postgres <<-EOSQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DEMO_PG_USER}') THEN
    CREATE ROLE ${DEMO_PG_USER} LOGIN PASSWORD '${DEMO_PG_PASSWORD}';
  END IF;
END
\$\$;
EOSQL

DB_EXISTS="$(gosu postgres psql --username postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${DEMO_PG_DB}'")"
if [ "$DB_EXISTS" != "1" ]; then
  gosu postgres psql -v ON_ERROR_STOP=1 --username postgres -c "CREATE DATABASE ${DEMO_PG_DB} OWNER ${DEMO_PG_USER}"
fi

# Always internally generated -- see header comment. Deliberately overrides
# anything set in the container's inherited environment.
export DATABASE_URL="postgresql://${DEMO_PG_USER}:${DEMO_PG_PASSWORD}@127.0.0.1:5432/${DEMO_PG_DB}"

echo "[entrypoint] Running synthetic demo seed (idempotent)"
PYTHONPATH=/app/backend_slice python /app/seed_demo.py

echo "[entrypoint] Starting orqis-mcp (streamable-http on ${MCP_HOST}:${MCP_PORT})"
exec gosu orqismcp orqis-mcp
