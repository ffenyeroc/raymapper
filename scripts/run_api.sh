#!/usr/bin/env bash
# Start the raymap API under launchd (or by hand).
#   - sources .env if present
#   - waits for Postgres so the first minutes are not error spam
#   - binds to 127.0.0.1 so /internal/* stays loopback-only by default
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PG_PORT="${PG_PORT:-5433}"
for _ in $(seq 1 60); do
  if nc -z 127.0.0.1 "$PG_PORT" 2>/dev/null; then break; fi
  sleep 1
done

exec ./.venv/bin/python -m uvicorn app_api:app \
  --host "${RAYMAP_BIND:-127.0.0.1}" --port "${PORT:-8000}" --app-dir .
