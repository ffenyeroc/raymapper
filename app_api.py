"""
raymap-map API — FastAPI app.

  POST /ingest             accept the collector's gzipped (or plain) JSON batch
  GET  /geojson            aggregated cells as GeoJSON (cacheable)
  POST /internal/refresh   recompute aggregates (scheduler / local dev)
  GET  /healthz            liveness + database check

Privacy: no precise coordinates are stored. Each report is snapped to a
geohash-6 cell (~1.2 km x 0.6 km) at insert time; lat/lon are discarded.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)

# Privacy knob: geohash length. 6 ~= 1.2 km x 0.6 km (project requirement: no
# more accurate than ~1 km). 5 ~= 4.9 km x 4.9 km, 7 ~= 153 m x 153 m.
CELL_PRECISION = max(4, min(8, int(os.environ.get("RAYMAP_CELL_PRECISION", "6"))))

MAX_DAYS = 90          # hard bound on /geojson lookback (also the presence retention)
MAX_FEATURES = 20000   # response cap
ATTRIBUTION = "Raymap aggregate data (c) contributors, CC BY 4.0"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def clamp_days(days: int) -> int:
    """Bound the lookback so /geojson stays cheap and cannot be abused."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 30
    return max(1, min(MAX_DAYS, days))


def classify(device_count: int, detection_device_count: int) -> Tuple[str, str]:
    """Map counts to (status, confidence). Single-device rows are reported but
    clearly marked so the UI can render them as unconfirmed."""
    if detection_device_count >= 2:
        return "detected_corroborated", "multi"
    if detection_device_count == 1:
        return "detected_single", "single"
    if device_count >= 2:
        return "clear_multi", "multi"
    return "clear_single", "single"


def validate_record(r: Any) -> Tuple[bool, str]:
    """Validate one report before it reaches SQL. Returns (ok, error)."""
    if not isinstance(r, dict):
        return False, "record is not an object"
    lat, lon = r.get("lat"), r.get("lon")
    if lat is None or lon is None:
        return False, "missing lat/lon"
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False, "lat/lon not numeric"
    if not (-90.0 <= lat <= 90.0):
        return False, "lat out of range"
    if not (-180.0 <= lon <= 180.0):
        return False, "lon out of range"
    ts = r.get("ts")
    if ts is None:
        return False, "missing ts"
    try:
        float(ts)
    except (TypeError, ValueError):
        return False, "ts not numeric"
    rssi = r.get("rssi_dbm")
    if rssi is not None and not isinstance(rssi, (int, float)):
        return False, "rssi_dbm not numeric"
    detection = r.get("detection")
    if detection is not None and not isinstance(detection, str):
        return False, "detection not a string"
    return True, ""


app = FastAPI(title="raymap-map API", version="0.2.0")

# Dev-permissive CORS; tightened to the UI origin in M1.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _conn():
    return psycopg.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
INSERT_REPORT = """
INSERT INTO reports (device_hash, ts, cell, rssi_dbm, detection, network)
SELECT %s, to_timestamp(%s),
       ST_GeoHash(ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s),
       %s, %s, %s
ON CONFLICT (device_hash, ts) DO NOTHING
"""


def _insert_batch(rows: List[Dict[str, Any]]) -> int:
    """Insert validated rows; coordinates are reduced to a cell and dropped."""
    inserted = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    INSERT_REPORT,
                    (
                        r["device_hash"],
                        float(r["ts"]),
                        float(r["lon"]),   # ST_MakePoint(x=lon, y=lat)
                        float(r["lat"]),
                        CELL_PRECISION,
                        int(r["rssi_dbm"]) if r.get("rssi_dbm") is not None else None,
                        r.get("detection"),
                        r.get("network"),
                    ),
                )
                if cur.rowcount == 1:
                    inserted += 1
    return inserted


@app.post("/ingest")
async def ingest(request: Request) -> JSONResponse:
    body = await request.body()
    # The collector sends gzip (Content-Encoding: gzip); accept both.
    if body[:2] == b"\x1f\x8b" or request.headers.get("content-encoding") == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError:
            return JSONResponse({"error": "invalid gzip body"}, status_code=400)

    try:
        batch = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if isinstance(batch, dict):
        records = batch.get("records") or []
        device_hash = batch.get("device") or ""
    elif isinstance(batch, list):
        records = batch
        device_hash = ""
    else:
        return JSONResponse({"error": "unexpected body shape"}, status_code=400)
    if not isinstance(records, list):
        return JSONResponse({"error": "records must be a list"}, status_code=400)

    rows, rejected, first_error = [], 0, None
    for r in records:
        if isinstance(r, dict):
            r = dict(r)
            if device_hash and not r.get("device_hash"):
                r["device_hash"] = device_hash
        if not isinstance(r, dict) or not r.get("device_hash"):
            rejected += 1
            first_error = first_error or "missing device hash"
            continue
        ok, err = validate_record(r)
        if not ok:
            rejected += 1
            first_error = first_error or err
            continue
        rows.append(r)

    inserted = _insert_batch(rows) if rows else 0
    payload: Dict[str, Any] = {
        "ok": True,
        "inserted": inserted,
        "rejected": rejected,
    }
    if first_error:
        payload["first_error"] = first_error
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# GeoJSON for the map
# ---------------------------------------------------------------------------
@app.get("/geojson")
def geojson(days: int = 30, include_detections_only: bool = False) -> JSONResponse:
    """Return aggregated cells as GeoJSON. Reads only the aggregate table, so it
    is cheap and cacheable; refreshing is an explicit job (/internal/refresh)."""
    days = clamp_days(days)
    query = """
        SELECT day, cell, device_count, report_count,
               detection_count, detection_device_count, last_ts,
               ST_Y(ST_PointFromGeoHash(cell, 4326)) AS lat,
               ST_X(ST_PointFromGeoHash(cell, 4326)) AS lon
        FROM daily_cells
        WHERE day >= (now() - make_interval(days => %s))::date
    """
    params: List[Any] = [days]
    if include_detections_only:
        query += " AND detection_count > 0"
    query += " ORDER BY report_count DESC LIMIT " + str(MAX_FEATURES)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    features = []
    for day, cell, dev, rep, det, det_dev, last_ts, lat, lon in rows:
        status, confidence = classify(dev, det_dev)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "cell": cell,
                    "day": str(day),
                    "device_count": dev,
                    "report_count": rep,
                    "detection_count": det,
                    "detection_device_count": det_dev,
                    "status": status,
                    "confidence": confidence,
                    "last_ts": last_ts.isoformat() if last_ts else None,
                },
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )

    return JSONResponse(
        {
            "type": "FeatureCollection",
            "attribution": ATTRIBUTION,
            "license": "CC BY 4.0",
            "days": days,
            "features": features,
        }
    )


# ---------------------------------------------------------------------------
# Maintenance + health (moved out of the read path in M0)
# ---------------------------------------------------------------------------
@app.post("/internal/refresh")
def refresh(days: int = 30) -> JSONResponse:
    """Recompute daily cells and fold detection history. Called by the scheduler."""
    days = clamp_days(days)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (days,))
            cur.execute("SELECT fold_detection_months();")
    return JSONResponse({"ok": True, "lookback_days": days})


@app.get("/healthz")
def healthz() -> JSONResponse:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
    except psycopg.Error as exc:
        return JSONResponse({"status": "degraded", "error": str(exc)[:200]}, status_code=503)
    return JSONResponse({"status": "ok"})


# Serve the map UI (ui/ folder) at the root of the same origin.
# Mounted last so the API routes take precedence.
_UI_DIR = Path(__file__).resolve().parent / "ui"
if _UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
