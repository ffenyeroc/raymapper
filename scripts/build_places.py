#!/usr/bin/env python3
"""
build_places.py — build the offline place/ZIP search index.

Source: U.S. Census Bureau 2023 Gazetteer files (public domain):
  2023_Gaz_place_national.zip   incorporated places + CDPs (name, state, lat/lon)
  2023_Gaz_zcta_national.zip    ZCTA centroids (ZIP -> lat/lon)

Output: ui/data/places.json.gz
  { "meta": {...},
    "zips":   { "30303": [lat, lon] },
    "places": { "atlanta,ga": [lat, lon, "Atlanta"] } }

Keys are lower-case "name,st" with Census suffixes ("city", "CDP", ...) stripped.
Queries never leave the browser: the index is served as a static file.

Usage:
  ./.venv/bin/python scripts/build_places.py --download
  ./.venv/bin/python scripts/build_places.py --place-txt /tmp/p.txt --zcta-txt /tmp/z.txt
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ui" / "data" / "places.json.gz"
BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer"
SUFFIXES = (
    " city", " town", " village", " borough", " cdp", " township",
    " municipality", " urbana", " (balance)", " balance",
)


def strip_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name.strip()


def read_rows(text: str):
    lines = text.splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    for line in lines[1:]:
        if not line.strip():
            continue
        yield dict(zip(header, (v.strip() for v in line.split("\t"))))


def fetch(zip_name: str) -> str:
    url = f"{BASE}/{zip_name}"
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".txt"))
        return archive.read(name).decode("utf-8", "replace")


def build(place_text: str, zcta_text: str) -> dict:
    zips: dict = {}
    for row in read_rows(zcta_text):
        code = row.get("GEOID", "")
        try:
            lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
        except (KeyError, ValueError):
            continue
        if len(code) == 5:
            zips[code] = [round(lat, 4), round(lon, 4)]

    places: dict = {}
    for row in read_rows(place_text):
        name, state = row.get("NAME", ""), row.get("USPS", "")
        try:
            lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
        except (KeyError, ValueError):
            continue
        clean = strip_suffix(name)
        if not clean or "," in clean or not state:
            continue
        key = f"{clean.lower()},{state.lower()}"
        try:
            land = int(row.get("ALAND") or 0)
        except ValueError:
            land = 0
        # Keep the largest land area for duplicate name+state keys.
        existing = places.get(key)
        if existing and existing[3] >= land:
            continue
        places[key] = [round(lat, 4), round(lon, 4), clean, land]

    return {
        "meta": {
            "source": "U.S. Census Bureau 2023 Gazetteer (public domain)",
            "places": len(places),
            "zips": len(zips),
        },
        "zips": zips,
        "places": {k: [v[0], v[1], v[2]] for k, v in places.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--place-txt")
    parser.add_argument("--zcta-txt")
    args = parser.parse_args()

    if args.download:
        place_text = fetch("2023_Gaz_place_national.zip")
        zcta_text = fetch("2023_Gaz_zcta_national.zip")
    elif args.place_txt and args.zcta_txt:
        place_text = Path(args.place_txt).read_text(encoding="utf-8", errors="replace")
        zcta_text = Path(args.zcta_txt).read_text(encoding="utf-8", errors="replace")
    else:
        parser.error("use --download or both --place-txt and --zcta-txt")
        return 2

    data = build(place_text, zcta_text)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":")).encode()
    with gzip.open(OUT, "wb", compresslevel=9) as fh:
        fh.write(raw)
    print(f"{data['meta']['places']} places, {data['meta']['zips']} ZIPs")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB gz, {len(raw) / 1048576:.1f} MB raw)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
