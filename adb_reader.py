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
    python adb_reader.py --interval 900         # loop every 15 min (standard cadence)
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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

DEVICE_CONFIG = "/data/rayhunter-data/config.toml"
DEVICE_QMDL = "/data/rayhunter-data/qmdl"
DEVICE_MANIFEST = f"{DEVICE_QMDL}/manifest.toml"
# This client reads Rayhunter's config/manifest/analyzer layout directly, so an
# upgrade that moves those files breaks it silently. Warn instead.
PINNED_RAYHUNTER = "0.12.0"

__version__ = "1.0.0"


# --------------------------------------------------------------------------
# Privacy helpers
# --------------------------------------------------------------------------
def _device_id_path() -> Path:
    base = os.environ.get("RAYMAP_STATE_DIR")
    if base:
        return Path(base) / "device_id"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "raymap" / "device_id"
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home())
        return Path(root) / "raymap" / "device_id"
    return Path.home() / ".raymap" / "device_id"


def device_hash_for_install() -> str:
    """Stable pseudonym for this install.

    A random UUID is generated once and only its hash is sent. The ADB serial is
    used locally to pick the device and is never hashed or transmitted.
    """
    path = _device_id_path()
    raw = path.read_text().strip() if path.exists() else ""
    if not raw:
        raw = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def clamp_ts(device_ts: Any, now: int, tolerance: int = 3600) -> int:
    """Trust the device clock only when it agrees with ours (within 1 hour)."""
    try:
        value = int(float(device_ts))
    except (TypeError, ValueError):
        return now
    return value if abs(value - now) <= tolerance else now


def redact(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of a record with coordinates removed (for logs)."""
    out = dict(record)
    if out.get("lat") is not None:
        out["lat"] = "<redacted>"
    if out.get("lon") is not None:
        out["lon"] = "<redacted>"
    return out


# --------------------------------------------------------------------------
# Proof of work — keep in sync with raymap-map/app_api.py pow_verify()
# --------------------------------------------------------------------------
POW_BASE_BITS = 12
POW_BUCKET_S = 300


def _leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        bits += 8 - byte.bit_length()
        break
    return bits


def solve_pow(raw_body: bytes, device_hash: str, n_records: int) -> str:
    bits = POW_BASE_BITS + (max(1, n_records) - 1).bit_length()
    digest = hashlib.sha256(raw_body).hexdigest()
    bucket = int(time.time()) // POW_BUCKET_S
    prefix = f"{device_hash}|{bucket}|{digest}|"
    nonce = 0
    while True:
        if _leading_zero_bits(hashlib.sha256((prefix + str(nonce)).encode()).digest()) >= bits:
            return f"{bits}:{nonce}"
        nonce += 1


# --------------------------------------------------------------------------
# ADB helpers
# --------------------------------------------------------------------------
def adb_bin() -> str:
    """The adb next to the executable when frozen (installers bundle it), else PATH."""
    name = "adb.exe" if sys.platform == "win32" else "adb"
    bundled = Path(sys.executable).parent / name
    return str(bundled) if bundled.exists() else name


# A windowed build must not flash a console every time adb runs.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def adb_devices() -> List[str]:
    out = subprocess.run(
        [adb_bin(), "devices"], capture_output=True, text=True, **_NO_WINDOW
    ).stdout
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def adb_shell(serial: str, command: str) -> str:
    res = subprocess.run(
        [adb_bin(), "-s", serial, "shell", command],
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )
    return res.stdout


def adb_cat(serial: str, path: str) -> str:
    return adb_shell(serial, f"cat {path}")


def adb_ls(serial: str, path: str) -> str:
    return adb_shell(serial, f"ls -la --color=never {path}")


def manifest_version(text: str) -> Optional[str]:
    """rayhunter_version of the newest session in manifest.toml, or None.

    Entries are appended per recording session, so the last one is what is
    currently running.
    """
    versions = re.findall(r'rayhunter_version\s*=\s*"([^"]+)"', text)
    return versions[-1] if versions else None


def preflight(serial: str) -> None:
    """Warn (do not fail) when the device is not the version this client targets."""
    version = manifest_version(adb_cat(serial, DEVICE_MANIFEST))
    if version is None:
        print(f"preflight: no Rayhunter version in {DEVICE_MANIFEST} — paths may have moved")
    elif version != PINNED_RAYHUNTER:
        print(
            f"preflight: Rayhunter {version}, this client is written for {PINNED_RAYHUNTER}"
            " — check that the config/manifest paths still exist"
        )
    else:
        print(f"preflight: Rayhunter {version}")


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
    # Pseudonym is per-install, never derived from the hardware serial.
    device_hash = device_hash_for_install()

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

    now = int(time.time())
    ts = now
    if gps and gps.get("system_time"):
        # Unsynced device clocks would land outside the retention window.
        ts = clamp_ts(gps["system_time"], now)

    # If we have no usable coordinates, there's nothing to place on the map —
    # skip so we never insert coordinate-less rows (which break aggregation).
    if lat is None or lon is None:
        return None

    # Send only what ingest stores: no serial, config, or session metadata.
    record = {
        "device_hash": device_hash,
        "ts": ts,
        "lat": lat,
        "lon": lon,
        "rssi_dbm": None,          # not exposed in the analysis NDJSON
        "detection": det["detection"],
        "network": None,
    }
    return {"device": device_hash, "count": 1, "records": [record]}


def post_batch(endpoint: str, batch: Dict[str, Any]) -> bool:
    payload = gzip.compress(json.dumps(batch).encode())
    try:
        resp = requests.post(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "X-Raymap-Pow": solve_pow(payload, batch["device"], len(batch["records"])),
            },
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
        default=os.environ.get("RAYMAP_INGEST", "https://api.raymap.org/ingest"),
    )
    parser.add_argument(
        "--version", action="version", version=f"raymap-collector {__version__}"
    )
    parser.add_argument(
        "--interval", type=int, default=0,
        help="loop every N seconds (0 = run once); the deployment uses 900 (15 min)"
    )
    parser.add_argument(
        "--once", action="store_true", help="pull current status once and exit (default)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-coords", action="store_true",
                        help="print coordinates in logs (off by default)")
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
    preflight(serial)

    def run_once() -> None:
        # Distinguish "hotspot unplugged" from "GPS/config unreadable": without
        # this both show up as a silent skip in the log.
        if serial not in adb_devices():
            print(f"[{time.strftime('%H:%M:%S')}] hotspot not connected — waiting")
            return
        config = parse_config(adb_cat(serial, DEVICE_CONFIG))
        batch = build_records(serial, config, args.lat, args.lon)
        if batch is None:
            print(f"[{time.strftime('%H:%M:%S')}] no coordinates available — skipping post")
            return
        rec = batch["records"][0]
        # Coordinates are never printed by default.
        print(
            f"[{time.strftime('%H:%M:%S')}] detection={rec.get('detection')} "
            f"ts={rec.get('ts')} has_coords={rec.get('lat') is not None}"
        )
        if args.dry_run:
            shown = rec if args.show_coords else redact(rec)
            print("--dry-run: would post ->", json.dumps(shown)[:300])
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