#!/usr/bin/env python3
"""
adb_reader.py — pull Rayhunter status/detection data from the connected hotspot
over ADB and post it to the raymap map ingest.

This is a stopgap to exercise the collector -> map pipeline while a network
path to the device's :8080 REST API isn't available (e.g. before HoRNDIS is
installed). It reads the same on-device data the daemon writes:

  * /data/rayhunter-data/config.toml        -> device + fixed GPS coordinates
  * /data/rayhunter-data/qmdl/*.ndjson      -> analysis (detection events)
  * /data/rayhunter-data/qmdl/*-gps.ndjson  -> GPS fixes

Records are posted in the same shape the collector uses ({device, records}),
so the map ingest treats them identically. Location privacy is applied
server-side: the API snaps each report to a geohash-6 cell and discards the
coordinates, so nothing precise is stored.

Usage:
    python adb_reader.py --once                 # pull current state + post
    python adb_reader.py --interval 300         # loop every 5 min
    python adb_reader.py --dry-run              # show what would be posted
    python adb_reader.py --endpoint http://host:8000/ingest
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

DEVICE_CONFIG = "/data/rayhunter-data/config.toml"
DEVICE_QMDL = "/data/rayhunter-data/qmdl"


# --------------------------------------------------------------------------
# ADB helpers
# --------------------------------------------------------------------------
def adb_devices() -> List[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def adb_shell(serial: str, command: str) -> str:
    res = subprocess.run(
        ["adb", "-s", serial, "shell", command], capture_output=True, text=True
    )
    return res.stdout


def adb_cat(serial: str, path: str) -> str:
    return adb_shell(serial, f"cat {path}")
def adb_ls(serial: str, path: str) -> str:
    return adb_shell(serial, f"ls -la --color=never {path}")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------
def parse_config(text: str) -> Dict[str, Any]:
    """Parse a Rayhunter config.toml into a dict (best-effort)."""
    cfg: Dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip().replace("\r", "")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.isdigit():
            value = int(value)
        cfg[key] = value
    return cfg


def parse_gps_ndjson(text: str) -> List[Dict[str, Any]]:
    """Parse a *-gps.ndjson file into a list of {"lat","lon","system_time"}."""
    points = []
    for line in text.splitlines():
        line = line.strip().replace("\r", "")
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("lat") is not None and obj.get("lon") is not None:
            points.append(
                {
                    "lat": obj["lat"],
                    "lon": obj["lon"],
                    "system_time": obj.get("system_time"),
                }
            )
    return points


def parse_analysis_ndjson(text: str) -> List[Dict[str, Any]]:
    """Parse an analysis .ndjson; return the per-message records with the
    metadata header (analyzers/version) as the first element, if present."""
    records = []
    for line in text.splitlines():
        line = line.strip().replace("\r", "")
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def extract_detection(analysis: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Best-effort: does any analysis record carry a detection event?
    Returns a summary dict. Rayhunter events[] may be empty for most
    recordings; if a real event appears we surface its name."""
    total_events = 0
    first_event_name = None
    for rec in analysis:
        events = rec.get("events") or []
        total_events += len(events)
        if events and first_event_name is None:
            ev = events[0]
            if isinstance(ev, dict):
                for k in ("name", "analyzer", "event_type", "type"):
                    if ev.get(k):
                        first_event_name = str(ev[k])
                        break
                if first_event_name is None:
                    first_event_name = json.dumps(ev)[:80]
    return {
        "detected": total_events > 0,
        "event_count": total_events,
        "detection": first_event_name or ("detected" if total_events else None),
    }


def latest_gps_point(serial: str) -> Optional[Dict[str, Any]]:
    """Return the most recent GPS fix from the newest non-empty *-gps.ndjson."""
    ls = adb_ls(serial, DEVICE_QMDL)
    gps_files = []
    for line in ls.splitlines():
        try:
            name = _strip_ansi(line.split()[-1])
        except IndexError:
            continue
        if name.endswith("-gps.ndjson"):
            gps_files.append(name)
    gps_files.sort()
    for name in reversed(gps_files):
        pts = parse_gps_ndjson(adb_cat(serial, f"{DEVICE_QMDL}/{name}"))
        if pts:
            return pts[-1]
    return None


def latest_analysis(serial: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return (epoch, records) of the newest non-gps *.ndjson analysis file."""
    ls = adb_ls(serial, DEVICE_QMDL)
    ndjson_files = []
    for line in ls.splitlines():
        try:
            name = _strip_ansi(line.split()[-1])
        except IndexError:
            continue
        if name.endswith(".ndjson") and not name.endswith("-gps.ndjson"):
            ndjson_files.append(name)
    ndjson_files.sort()
    if not ndjson_files:
        return "", []
    name = ndjson_files[-1]
    return name, parse_analysis_ndjson(adb_cat(serial, f"{DEVICE_QMDL}/{name}"))


# --------------------------------------------------------------------------
# Record building
# --------------------------------------------------------------------------
def build_records(
    serial: str, config: Dict[str, Any],
    lat_override: Optional[float] = None,
    lon_override: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Compose a single status record for the device's current state.
    Coordinates resolve in priority: device GPS fix > --lat/--lon override >
    fixed coords from config. Returns None if no coordinates are available."""
    device_hash = hashlib.sha256(serial.encode()).hexdigest()[:16]

    # Coordinates: prefer a real GPS fix, else override, else fixed config.
    lat, lon, gps_source = None, None, "none"
    gps = latest_gps_point(serial)
    if gps:
        lat, lon, gps_source = gps["lat"], gps["lon"], "device"
    elif lat_override is not None and lon_override is not None:
        lat, lon, gps_source = lat_override, lon_override, "fixed-manual"
    elif config.get("gps_mode") == 1:  # Fixed
        lat = config.get("gps_fixed_latitude")
        lon = config.get("gps_fixed_longitude")
        gps_source = "fixed"

    if lat is not None:
        lat = float(lat)
    if lon is not None:
        lon = float(lon)

    epoch, analysis = latest_analysis(serial)
    det = extract_detection(analysis)

    ts = int(time.time())
    if gps and gps.get("system_time"):
        ts = int(gps["system_time"])

    # If we have no usable coordinates, there's nothing to place on the map —
    # skip so we never insert coordinate-less rows (which break aggregation).
    if lat is None or lon is None:
        return None

    record = {
        "device_hash": device_hash,
        "ts": ts,
        "lat": lat,
        "lon": lon,
        "gps_source": gps_source,
        "rssi_dbm": None,          # not exposed in the analysis NDJSON
        "detection": det["detection"],
        "network": None,
        # Non-schema summary fields (ignored by ingest insert).
        "session": epoch,
        "session_messages": len(analysis)
        - (1 if analysis and "analyzers" in analysis[0] else 0),
        "session_events": det["event_count"],
        "config": {
            "device": config.get("device"),
            "gps_mode": config.get("gps_mode"),
            "rayhunter_port": config.get("port"),
        },
    }
    return {"device": device_hash, "count": 1, "records": [record]}


def post_batch(endpoint: str, batch: Dict[str, Any]) -> bool:
    payload = gzip.compress(json.dumps(batch).encode())
    try:
        resp = requests.post(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            timeout=15,
        )
        ok = resp.status_code == 200
        if not ok:
            print(f"ingest HTTP {resp.status_code}: {resp.text[:200]}")
        return ok
    except requests.RequestException as exc:
        print(f"ingest error: {exc}")
        return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="ADB reader for Rayhunter hotspots")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("RAYMAP_INGEST", "http://localhost:8000/ingest"),
    )
    parser.add_argument(
        "--interval", type=int, default=0, help="loop every N seconds (0 = run once)"
    )
    parser.add_argument(
        "--once", action="store_true", help="pull current status once and exit (default)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--serial", default=None)
    parser.add_argument("--lat", type=float, default=None,
                        help="fixed latitude used if the device has no GPS fix")
    parser.add_argument("--lon", type=float, default=None,
                        help="fixed longitude used if the device has no GPS fix")
    args = parser.parse_args()

    serials = adb_devices()
    serial = args.serial or (serials[0] if serials else None)

    # In loop mode, wait for a device to appear instead of exiting.
    if not serial:
        if args.interval > 0:
            print("waiting for an ADB device to connect...")
            while not serial:
                time.sleep(10)
                serials = adb_devices()
                serial = args.serial or (serials[0] if serials else None)
        else:
            print("No ADB device connected (adb devices found none).")
            sys.exit(1)

    print(f"using device serial: {serial}")

    def run_once() -> None:
        config = parse_config(adb_cat(serial, DEVICE_CONFIG))
        batch = build_records(serial, config, args.lat, args.lon)
        if batch is None:
            print(f"[{time.strftime('%H:%M:%S')}] no coordinates available — skipping post")
            return
        rec = batch["records"][0]
        print(
            f"[{time.strftime('%H:%M:%S')}] session={rec.get('session')} "
            f"lat={rec.get('lat')} lon={rec.get('lon')} "
            f"detection={rec.get('detection')} events={rec.get('session_events')}"
        )
        if args.dry_run:
            print("--dry-run: would post ->", json.dumps(rec)[:300])
        else:
            print("posted:", "OK" if post_batch(args.endpoint, batch) else "FAILED")

    if args.interval > 0:
        print(f"looping every {args.interval}s — Ctrl-C to stop")
        while True:
            try:
                run_once()
            except Exception as exc:  # keep the loop alive across device hiccups
                print(f"error: {exc}")
            time.sleep(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()