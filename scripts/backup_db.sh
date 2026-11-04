#!/usr/bin/env bash
# Nightly logical backup of the raymap database (launchd runs it at 03:30).
# Keeps the newest $RAYMAP_BACKUP_KEEP dumps; restores with --clean so a drill
# or disaster recovery can run against a non-empty database.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

BACKUP_DIR="${RAYMAP_BACKUP_DIR:-$HOME/raymap-backups}"
KEEP="${RAYMAP_BACKUP_KEEP:-14}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/raymap-$STAMP.sql.gz"

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx raymap_db; then
  # Local dev: the database lives in the compose container.
  if ! docker exec raymap_db pg_isready -U raymap -d raymap >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) database not reachable; backup skipped" >&2
    exit 1
  fi
  docker exec raymap_db pg_dump -U raymap -d raymap --clean --if-exists | gzip -9 > "$OUT"
else
  # VPS: PostgreSQL runs on the host, DATABASE_URL points at it.
  : "${DATABASE_URL:?DATABASE_URL must be set when there is no raymap_db container}"
  pg_dump "$DATABASE_URL" --clean --if-exists | gzip -9 > "$OUT"
fi
echo "$(date -u +%FT%TZ) wrote $OUT ($(du -h "$OUT" | cut -f1))"

ls -1t "$BACKUP_DIR"/raymap-*.sql.gz | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
  echo "pruned $old"
done
