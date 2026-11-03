#!/usr/bin/env python3
"""
migrate_to_hex.py — one-time migration from geohash cells to H3 (res 3..8).

What happens:
  * `reports`      gains h3_r3..h3_r8, backfilled from the geohash cell centroid,
                    then the geohash column is dropped (no lat/lon is recovered).
  * `daily_cells`  is DERIVED data: it is dropped and rebuilt from `reports` by
                    refresh_daily_cells(). Presence history older than the 72 h
                    raw-retention window is therefore not carried over.
  * `detection_months` (the indefinite archive) is re-keyed from geohash-6 to
                    H3 r7 and preserved (sum counts, max devices, max last_ts).

Idempotent: if `reports` is already H3-shaped the script exits without changes.

Usage:  ./.venv/bin/python scripts/migrate_to_hex.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import h3
import psycopg

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)
RESOLUTIONS = (3, 4, 5, 6, 7, 8)
ARCHIVE_RES = 7
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def columns(cur, table: str) -> set:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {row[0] for row in cur.fetchall()}


def h3_columns(lat: float, lng: float) -> dict:
    return {f"h3_r{r}": h3.latlng_to_cell(lat, lng, r) for r in RESOLUTIONS}


def migrate_reports(cur, dry_run: bool) -> int:
    cols = columns(cur, "reports")
    if "cell" not in cols:
        print("reports: already H3-shaped, nothing to do")
        return 0

    for r in RESOLUTIONS:
        if f"h3_r{r}" not in cols:
            cur.execute(f"ALTER TABLE reports ADD COLUMN h3_r{r} text")

    cur.execute(
        "SELECT DISTINCT cell, ST_Y(ST_PointFromGeoHash(cell, 4326)), "
        "ST_X(ST_PointFromGeoHash(cell, 4326)) FROM reports"
    )
    cells = cur.fetchall()
    print(f"reports: backfilling {len(cells)} distinct geohash cells from their centroids")
    if dry_run:
        return len(cells)

    for cell, lat, lng in cells:
        values = h3_columns(float(lat), float(lng))
        cur.execute(
            "UPDATE reports SET "
            + ", ".join(f"h3_r{r} = %(h3_r{r})s" for r in RESOLUTIONS)
            + " WHERE cell = %(cell)s",
            {**values, "cell": cell},
        )

    for r in RESOLUTIONS:
        cur.execute(f"ALTER TABLE reports ALTER COLUMN h3_r{r} SET NOT NULL")
    cur.execute("ALTER TABLE reports DROP COLUMN cell")
    print("reports: H3 columns populated; geohash column dropped")
    return len(cells)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            migrate_reports(cur, args.dry_run)

            # The archive must be stashed before its table is dropped.
            archived = None
            cols = columns(cur, "detection_months")
            if cols:
                cur.execute("SELECT cell FROM detection_months LIMIT 1")
                first = cur.fetchone()
                if first is not None and len(first[0]) != 15:
                    cur.execute(
                        "SELECT month, cell, detection_count, detection_device_count, last_report "
                        "FROM detection_months"
                    )
                    old = cur.fetchall()
                    cur.execute(
                        "SELECT DISTINCT cell, ST_Y(ST_PointFromGeoHash(cell, 4326)), "
                        "ST_X(ST_PointFromGeoHash(cell, 4326)) FROM detection_months"
                    )
                    centroids = {
                        c: h3.latlng_to_cell(float(lat), float(lng), ARCHIVE_RES)
                        for c, lat, lng in cur.fetchall()
                    }
                    merged = {}
                    for month, cell, det, det_dev, last in old:
                        key = (month, centroids[cell])
                        d, v, t = merged.get(key, (0, 0, None))
                        stamps = [x for x in (t, last) if x is not None]
                        merged[key] = (d + det, max(v, det_dev), max(stamps) if stamps else None)
                    archived = merged
                    print(f"detection_months: stashed {len(old)} rows -> {len(merged)} H3 rows")
                    if not args.dry_run:
                        cur.execute("DROP TABLE detection_months")

            if not args.dry_run:
                # daily_cells is derived; drop so the new (day, res, cell) table is created.
                cur.execute("DROP TABLE IF EXISTS daily_cells")
                cur.execute(SCHEMA.read_text())

                if archived:
                    for (month, cell), (det, det_dev, last) in archived.items():
                        cur.execute(
                            "INSERT INTO detection_months "
                            "(month, cell, detection_count, detection_device_count, last_report) "
                            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (month, cell) DO UPDATE SET "
                            "detection_count = GREATEST(detection_months.detection_count, EXCLUDED.detection_count), "
                            "detection_device_count = GREATEST(detection_months.detection_device_count, EXCLUDED.detection_device_count), "
                            "last_report = GREATEST(detection_months.last_report, EXCLUDED.last_report)",
                            (month, cell, det, det_dev, last),
                        )
                    print(f"detection_months: restored {len(archived)} rows")

                cur.execute("SELECT refresh_daily_cells(30);")
                cur.execute("SELECT fold_detection_months();")
                cur.execute("SELECT count(*) FROM reports")
                reports = cur.fetchone()[0]
                cur.execute("SELECT count(*), count(DISTINCT res) FROM daily_cells")
                cells, res_count = cur.fetchone()
                cur.execute("SELECT count(*) FROM detection_months")
                months = cur.fetchone()[0]
                print(
                    f"done: {reports} reports, {cells} daily cells across {res_count} resolutions, "
                    f"{months} detection-month rows"
                )
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
