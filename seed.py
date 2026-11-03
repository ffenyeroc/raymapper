#!/usr/bin/env python3
"""
seed.py — apply the schema and simulate reports from ~1000 devices, then run
aggregation so /geojson returns data immediately.

Usage:
    docker compose up -d            # PostGIS on 127.0.0.1:5433
    python seed.py [--rows 300000] [--days 7] [--devices 1000] [--cluster]

Reports are snapped to H3 cells (r3..r7) exactly like the API does; no precise
coordinates are stored. `--cluster` concentrates most points in a few metros,
which is what real collector data looks like (random points rarely share a cell).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import time

import h3
import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)
RESOLUTIONS = (3, 4, 5, 6, 7)

# lat, lon, spread (degrees)
METROS = [
    (33.749, -84.388, 0.35),   # Atlanta
    (39.739, -104.990, 0.35),  # Denver
    (30.267, -97.743, 0.35),   # Austin
    (41.878, -87.630, 0.30),   # Chicago
    (37.775, -122.419, 0.30),  # San Francisco
]

INSERT = """
INSERT INTO reports (device_hash, ts, h3_r3, h3_r4, h3_r5, h3_r6, h3_r7,
                     rssi_dbm, detection, network)
VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (device_hash, ts) DO NOTHING
"""


def apply_schema(conn) -> None:
    with open("schema.sql") as fh:
        schema = fh.read()
    with conn.cursor() as cur:
        cur.execute(schema)
    conn.commit()


def random_point(cluster: bool) -> tuple[float, float]:
    """Clustered mode stays on land (all metros are inside the US); the default
    scatters over the CONUS bounding box, which is ~58% ocean by area."""
    if cluster:
        lat, lon, spread = random.choice(METROS)
        return lat + random.gauss(0, spread), lon + random.gauss(0, spread)
    return random.uniform(24.0, 49.0), random.uniform(-125.0, -66.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=300_000)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--devices", type=int, default=1_000)
    parser.add_argument("--cluster", action="store_true")
    args = parser.parse_args()

    print(f"connecting to {DATABASE_URL}")
    with psycopg.connect(DATABASE_URL) as conn:
        apply_schema(conn)
        print("schema applied")

        now = time.time()
        inserted = 0
        batch = []

        def flush(cur) -> None:
            nonlocal batch, inserted
            if not batch:
                return
            cur.executemany(INSERT, batch)
            inserted += len(batch)
            batch = []

        with conn.cursor() as cur:
            for i in range(args.rows):
                lat, lon = random_point(args.cluster)
                ts = now - random.random() * args.days * 86400
                device = f"dev{i % args.devices}"
                device_hash = hashlib.sha256(device.encode()).hexdigest()[:16]
                detection = random.choice([None, None, None, None, None, "imsi_catcher"])
                batch.append(
                    (device_hash, ts, *(h3.latlng_to_cell(lat, lon, r) for r in RESOLUTIONS),
                     random.randint(-110, -70), detection, "LTE")
                )
                if len(batch) >= 5000:
                    flush(cur)
                    print(f"  ...{inserted} rows")
            flush(cur)
        conn.commit()
        print(f"inserted {inserted} raw reports across {args.devices} devices")

        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (args.days,))
            cur.execute("SELECT fold_detection_months();")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT res, count(*), sum(device_count), sum(report_count), "
                "sum(detection_count), count(*) FILTER (WHERE detection_device_count = 1) "
                "FROM daily_cells GROUP BY res ORDER BY res;"
            )
            rows = cur.fetchall()
            cur.execute("SELECT count(*) FROM detection_months;")
            months = cur.fetchone()[0]
        print("resolution | cells | devices | reports | detections | single-device cells")
        for res, cells, devs, reps, dets, single in rows:
            print(f"  r{res:<8} | {cells:>5} | {devs:>7} | {reps:>7} | {dets:>10} | {single}")
        print(f"detection_months rows: {months}")


if __name__ == "__main__":
    main()
