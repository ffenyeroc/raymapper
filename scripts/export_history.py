#!/usr/bin/env python3
"""
export_history.py — write CC BY 4.0 monthly detection snapshots.

The archive is keyed at H3 r7; snapshots are folded up to the map's floor (r6)
and written as line-delimited JSON, gzipped, plus an index:

    ui/data/history/YYYY-MM.ndjson.gz   one cell per line
    ui/data/history/index.json          totals + attribution + license

Served statically at /data/history/..., so the archive is downloadable without
the API. Re-run monthly (or after a retraction).

Usage:  ./.venv/bin/python scripts/export_history.py [--res 6]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import h3
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_api import ATTRIBUTION, fold_history  # noqa: E402  (pure helpers)

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)
OUT_DIR = ROOT / "ui" / "data" / "history"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--res", type=int, default=6, choices=range(3, 7))
    args = parser.parse_args()

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT month, cell, detection_count, detection_device_count, last_report "
                "FROM detection_months ORDER BY month"
            )
            rows = cur.fetchall()

    agg = fold_history(rows, args.res)
    by_month: dict = {}
    for (month, cell), (det, det_dev, last) in agg.items():
        by_month.setdefault(month, []).append(
            {
                "month": str(month),
                "h3": cell,
                "res": args.res,
                "detection_count": det,
                "detection_device_count": det_dev,
                "last_report": last.isoformat() if last else None,
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Remove stale month files: after a retraction or a database cleanup an old
    # snapshot must not keep serving data that no longer exists.
    for old in OUT_DIR.glob("*.ndjson.gz"):
        old.unlink()
    index = []
    for month in sorted(by_month):
        cells = sorted(by_month[month], key=lambda c: c["detection_count"], reverse=True)
        name = f"{month:%Y-%m}.ndjson.gz"
        path = OUT_DIR / name
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for cell in cells:
                fh.write(json.dumps(cell, separators=(",", ":")) + "\n")
        index.append(
            {
                "month": f"{month:%Y-%m}",
                "file": name,
                "cells": len(cells),
                "detections": sum(c["detection_count"] for c in cells),
            }
        )
        print(f"  {name}: {len(cells)} cells")

    (OUT_DIR / "index.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "res": args.res,
                "license": "CC BY 4.0",
                "attribution": ATTRIBUTION,
                "note": "Detection cells only; presence data is not retained beyond 90 days.",
                "months": index,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {len(index)} month(s) to {OUT_DIR}/index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
