#!/usr/bin/env bash
# One entry point for the whole stack (TECHNICAL §5). Commands are added stage by stage.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Keys and passwords come from .env, which is git-ignored.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PG_BIN="${PG_BIN:-/usr/local/opt/postgresql@16/bin}"
PSQL="$PG_BIN/psql"
DB_NAME="${DB_NAME:-support}"
TOOLBOX_DB_USER="${TOOLBOX_DB_USER:-support_toolbox}"

die() { echo "error: $*" >&2; exit 1; }

cmd_reset() {
  [[ -n "${TOOLBOX_DB_PASSWORD:-}" ]] || die "TOOLBOX_DB_PASSWORD is not set; add it to .env"
  "$PG_BIN/pg_isready" -q || die "Postgres is not running (brew services start postgresql@16)"

  # Database and role survive resets; create them only if missing.
  if ! "$PSQL" -d postgres -tAc "select 1 from pg_database where datname = '$DB_NAME'" | grep -q 1; then
    "$PG_BIN/createdb" "$DB_NAME"
  fi
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -q \
    -v role="$TOOLBOX_DB_USER" -v pw="$TOOLBOX_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'role', :'pw') \gexec
SQL
  "$PSQL" -d postgres -v ON_ERROR_STOP=1 -q \
    -c "GRANT CONNECT ON DATABASE \"$DB_NAME\" TO \"$TOOLBOX_DB_USER\";"

  # Tables are dropped and recreated, so ids always start at 1 (EVALS §2).
  "$PSQL" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q -f db/seed.sql
  echo "reset: $DB_NAME seeded"
}

# Foreground for now; stage 4+ turns this into a health-checked background start.
cmd_toolbox() {
  [[ -n "${TOOLBOX_DB_PASSWORD:-}" ]] || die "TOOLBOX_DB_PASSWORD is not set; add it to .env"
  exec npx -y @toolbox-sdk/server --config mcp_toolbox/tools.yaml \
    --enable-api --address 127.0.0.1 --port 5000
}

case "${1:-}" in
  reset) cmd_reset ;;
  toolbox) cmd_toolbox ;;
  *) echo "usage: $0 reset|toolbox" >&2; exit 2 ;;
esac
