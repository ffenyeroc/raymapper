#!/usr/bin/env python3
"""
seed.py — apply the schema and simulate reports from ~1000 devices, then run
aggregation so /geojson returns data immediately.

Usage:
    docker compose up -d            # PostGIS on 127.0.0.1:5433
    python seed.py [--rows 300000] [--days 7] [--devices 1000]

Reports are snapped to geohash-6 cells exactly like the API does; no precise
coordinates are stored.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import time

import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)
CELL_PRECISION = 6

INSERT = """
INSERT INTO reports (device_hash, ts, cell, rssi_dbm, detection, network)
SELECT %s, to_timestamp(%s),
       ST_GeoHash(ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s),
       %s, %s, %s
ON CONFLICT (device_hash, ts) DO NOTHING
"""


def apply_schema(conn) -> None:
    with open("schema.sql") as fh:
        schema = fh.read()
    with conn.cursor() as cur:
        cur.execute(schema)
    conn.commit()


def random_us_point() -> tuple[float, float]:
    return random.uniform(24.0, 49.0), random.uniform(-125.0, -66.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=300_000)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--devices", type=int, default=1_000)
    args = parser.parse_args()

    print(f"connecting to {DATABASE_URL}")
    with psycopg.connect(DATABASE_URL) as conn:
        apply_schema(conn)
        print("schema applied")

        now = time.time()
        batch = []

        def flush(rows, cur) -> None:
            cur.executemany(INSERT, rows)

        inserted = 0
        with conn.cursor() as cur:
            for i in range(args.rows):
                lat, lon = random_us_point()
                ts = now - random.random() * args.days * 86400
                device = f"dev{i % args.devices}"
                device_hash = hashlib.sha256(device.encode()).hexdigest()[:16]
                detection = random.choice([None, None, None, None, None, "imsi_catcher"])
                batch.append(
                    (device_hash, ts, lon, lat, CELL_PRECISION,
                     random.randint(-110, -70), detection, "LTE")
                )
                if len(batch) >= 5000:
                    flush(batch, cur)
                    inserted += len(batch)
                    batch = []
                    print(f"  ...{inserted} rows")
            if batch:
                flush(batch, cur)
                inserted += len(batch)
        conn.commit()
        print(f"inserted {inserted} raw reports across {args.devices} devices")

        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (args.days,))
            cur.execute("SELECT fold_detection_months();")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), coalesce(sum(device_count),0), "
                "coalesce(sum(report_count),0), coalesce(sum(detection_count),0), "
                "count(*) FILTER (WHERE detection_device_count = 1) FROM daily_cells;"
            )
            cells, devs, reported, detections, single = cur.fetchone()
            cur.execute("SELECT count(*) FROM detection_months;")
            months = cur.fetchone()[0]
        print(f"aggregated into {cells} daily_cells "
              f"({devs} device-reporting-days, {reported} reports, {detections} detections, "
              f"{single} single-device cells); {months} detection_months rows")


if __name__ == "__main__":
    main()
