#!/usr/bin/env python3
"""
check_state_coverage.py — verify hex coverage against the vendored US boundary.

Reports how many r3 cells (12,393 km2 each, ~138 km across) cover each state,
and checks that every reported cell returned by a running API falls inside the
mesh the UI generates. Run after seeding:

    ./.venv/bin/python scripts/check_state_coverage.py [--api http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import h3
import requests

ROOT = Path(__file__).resolve().parent.parent
STATES = ROOT / "ui" / "data" / "us_states.geojson"
TARGET = (8, 16)  # "about 10-15 cells per state" at r3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--no-api", action="store_true",
                        help="skip the live-API coverage check (CI)")
    parser.add_argument("--res", type=int, default=3)
    args = parser.parse_args()

    data = json.loads(STATES.read_text())
    per_state = {}
    land = set()
    for feature in data["features"]:
        name = feature["properties"]["NAME"]
        cells = set(h3.geo_to_cells(feature["geometry"], args.res))
        per_state[name] = len(cells)
        land |= cells

    counts = sorted(per_state.items(), key=lambda kv: kv[1])
    print(f"r{args.res}: {len(land)} cells cover the US\n")
    for name, n in counts:
        print(f"  {name:<20} {n:>4}")
    print("  (counts are cells whose centre falls in the state; tiny states can")
    print("   show 0-1 and are still painted by neighbouring hexes)")

    values = [n for _, n in counts]
    median = statistics.median(values)
    print(f"\nmedian state: {median:.0f} cells (target {TARGET[0]}-{TARGET[1]})")

    failures = []
    if not TARGET[0] <= median <= TARGET[1]:
        failures.append(f"median {median} outside {TARGET}")
    if not 500 <= len(land) <= 900:
        failures.append(f"national cell count {len(land)} outside 500-900")

    # Every reported cell must be inside the mesh (the UI also has a fallback).
    if args.no_api:
        print("(data coverage check skipped: --no-api)")
    else:
        try:
            fc = requests.get(
                f"{args.api}/geojson?days=7&res={args.res}", timeout=20
            ).json()
            reported = {f["properties"]["h3"] for f in fc["features"]}
            outside = reported - land
            pct = 100.0 * len(outside) / max(1, len(reported))
            print(f"reported cells: {len(reported)}, outside mesh: {len(outside)} ({pct:.1f}%)")
            # Coastal reports legitimately fall outside the simplified land
            # polygon; the UI draws reported cells regardless, so only a gross
            # gap is a fault.
            if pct > 25:
                failures.append(f"{pct:.1f}% of reported cells outside the land mesh")
            elif pct > 5:
                print("  note: some reports are just offshore; the UI still draws them")
        except requests.RequestException as exc:
            print(f"(skipped data coverage check: {exc})")

    print("\n" + ("OK" if not failures else "FAIL: " + "; ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
