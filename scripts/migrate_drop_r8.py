#!/usr/bin/env python3
"""
migrate_drop_r8.py — drop reports.h3_r8 (0.74 km, the finest cell at rest).

Nothing reads r8: the map's floor is r6 and the detection archive is keyed at r7,
so the column only stored the finest location we had. daily_cells rows at res 8
are deleted and the res check is tightened to 3..7.

Idempotent: exits without changes if h3_r8 is already gone.

Usage:  ./.venv/bin/python scripts/migrate_drop_r8.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            if not cur.execute("SELECT to_regclass('public.reports')").fetchone()[0]:
                print("no reports table — nothing to migrate")
                return 0
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'reports' AND column_name = 'h3_r8'"
            )
            if cur.fetchone() is None:
                print("h3_r8 already gone — nothing to do")
                return 0

            cur.execute("SELECT count(*) FROM daily_cells WHERE res = 8")
            stale = cur.fetchone()[0]
            print(f"daily_cells rows at res 8: {stale}")
            if args.dry_run:
                print("dry run — no changes")
                return 0

            cur.execute("DELETE FROM daily_cells WHERE res = 8")
            cur.execute("ALTER TABLE reports DROP COLUMN h3_r8")
            cur.execute(
                "ALTER TABLE daily_cells "
                "DROP CONSTRAINT IF EXISTS daily_cells_res_check"
            )
            cur.execute(
                "ALTER TABLE daily_cells ADD CONSTRAINT daily_cells_res_check "
                "CHECK (res BETWEEN 3 AND 7)"
            )
            # Recreate refresh_daily_cells() against the column set that is left.
            cur.execute(SCHEMA.read_text())
        conn.commit()
    print("dropped reports.h3_r8; schema reapplied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
