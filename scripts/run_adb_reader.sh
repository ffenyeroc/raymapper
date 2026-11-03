#!/usr/bin/env bash
# Keep a Rayhunter device reporting while it is attached.
# The reader itself waits for ADB; this wrapper re-arms it if it ever exits
# (device unplugged, hotspot rebooted) so launchd's KeepAlive stays quiet.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
INTERVAL="${RAYMAP_UPLOAD_INTERVAL_S:-900}"
ENDPOINT="${RAYMAP_INGEST:-http://127.0.0.1:8000/ingest}"

while true; do
  ./.venv/bin/python -u adb_reader.py --interval "$INTERVAL" --endpoint "$ENDPOINT" || true
  sleep 30
done
