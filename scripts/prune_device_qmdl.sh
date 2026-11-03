#!/usr/bin/env bash
# Delete old raw qmdl captures on the device, keeping the newest N sessions.
# Only *.qmdl.gz (raw captures, never uploaded) are removed; the small *.ndjson
# analysis files and manifest.toml stay, so detections are not lost.
#   ./scripts/prune_device_qmdl.sh                 # dry run, keep 5
#   ./scripts/prune_device_qmdl.sh --keep 3 --apply
set -euo pipefail

KEEP=5
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "usage: $0 [--keep N] [--apply]" >&2; exit 2 ;;
  esac
done

DIR=/data/rayhunter-data/qmdl
SERIAL=$(adb devices | awk 'NR>1 && $2=="device" {print $1; exit}')
if [ -z "$SERIAL" ]; then echo "no ADB device attached" >&2; exit 1; fi
dev() { adb -s "$SERIAL" shell "$1" | tr -d '\r'; }

echo "before: $(dev 'df -h /data | tail -1')"
LIST=$(dev "ls $DIR/*.qmdl.gz 2>/dev/null | sed 's#.*/##' | sort -rn")
TOTAL=$(printf '%s\n' "$LIST" | awk 'NF' | wc -l | tr -d ' ')
DOOMED=$(printf '%s\n' "$LIST" | awk 'NF' | tail -n +"$((KEEP + 1))")
echo "$TOTAL capture(s) on device; keeping the newest $KEEP"

for f in $DOOMED; do
  size=$(dev "du -h $DIR/$f | cut -f1")
  if [ "$APPLY" = 1 ]; then
    dev "rm -f $DIR/$f"
    echo "  removed $f ($size)"
  else
    echo "  would remove $f ($size)"
  fi
done

[ "$APPLY" = 1 ] || echo "(dry run — pass --apply to delete)"
echo "after: $(dev 'df -h /data | tail -1')"
