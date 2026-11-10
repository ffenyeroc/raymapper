#!/usr/bin/env bash
# Keep a Rayhunter device reporting while it is attached.
# The reader itself waits for ADB; this wrapper re-arms it if it ever exits
# (device unplugged, hotspot rebooted) so launchd's KeepAlive stays quiet.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
INTERVAL="${RAYMAP_UPLOAD_INTERVAL_S:-900}"
# .env wins; the fallback is the public endpoint, since this wrapper exists to keep
# a station reporting and a missing .env should not silently retry against localhost.
ENDPOINT="${RAYMAP_INGEST:-https://api.raymap.org/ingest}"

while true; do
  ./.venv/bin/python -u adb_reader.py --interval "$INTERVAL" --endpoint "$ENDPOINT" || true
  sleep 30
done
