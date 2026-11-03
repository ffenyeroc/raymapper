"""
raymap-map API — FastAPI app (local hosting).

  POST /ingest             collector/ADB batch (gzip or plain), proof-of-work gated
  GET  /geojson            aggregated cells as GeoJSON (cacheable)
  POST /internal/refresh   recompute aggregates (manual)
  POST /internal/maintain  refresh + fold + prune (background loop / manual)
  GET  /healthz            liveness + database check

Privacy: no precise coordinates are stored. Each report is snapped to a
geohash-6 cell (~1.2 km x 0.6 km) at insert time; lat/lon are discarded.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("raymap")
logging.basicConfig(
    level=os.environ.get("RAYMAP_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://raymap:raymap@127.0.0.1:5433/raymap"
)

# Privacy knob: geohash length. 6 ~= 1.2 km x 0.6 km (requirement: no more
# accurate than ~1 km). 5 ~= 4.9 km, 7 ~= 153 m.
CELL_PRECISION = max(4, min(8, int(os.environ.get("RAYMAP_CELL_PRECISION", "6"))))

MAX_DAYS = 90            # /geojson lookback bound (also presence retention)
MAX_FEATURES = 20000     # response cap

# Ingest limits (trust boundaries).
MAX_BODY_BYTES = int(os.environ.get("RAYMAP_MAX_BODY_BYTES", 1_048_576))
MAX_DECOMPRESSED_BYTES = int(os.environ.get("RAYMAP_MAX_DECOMPRESSED_BYTES", 4_194_304))
MAX_RECORDS = int(os.environ.get("RAYMAP_MAX_RECORDS", 500))
QUOTA_RECORDS_HOUR = int(os.environ.get("RAYMAP_QUOTA_RECORDS_HOUR", 600))

# Proof of work: bits = 12 + ceil(log2(n_records)), payload & device bound.
POW_REQUIRED = os.environ.get("RAYMAP_POW_REQUIRED", "1") == "1"
POW_BASE_BITS = 12
POW_BUCKET_S = 300

# Retention / maintenance.
RAW_RETAIN_HOURS = int(os.environ.get("RAYMAP_RAW_RETAIN_HOURS", 72))
CELL_RETAIN_DAYS = int(os.environ.get("RAYMAP_CELL_RETAIN_DAYS", 90))
REFRESH_LOOKBACK_DAYS = int(os.environ.get("RAYMAP_REFRESH_LOOKBACK_DAYS", 30))
MAINTAIN_INTERVAL_S = int(os.environ.get("RAYMAP_MAINTAIN_INTERVAL_S", 300))

ATTRIBUTION = "Raymap aggregate data (c) contributors, CC BY 4.0"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def clamp_days(days: int) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 30
    return max(1, min(MAX_DAYS, days))


def classify(device_count: int, detection_device_count: int) -> Tuple[str, str]:
    if detection_device_count >= 2:
        return "detected_corroborated", "multi"
    if detection_device_count == 1:
        return "detected_single", "single"
    if device_count >= 2:
        return "clear_multi", "multi"
    return "clear_single", "single"


def validate_record(r: Any) -> Tuple[bool, str]:
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


def target_bits(n_records: int) -> int:
    """Difficulty for a batch: 12 bits, +1 bit per doubling of records."""
    n = max(1, int(n_records))
    return POW_BASE_BITS + (n - 1).bit_length()


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        bits += 8 - byte.bit_length()
        break
    return bits


def pow_verify(raw_body: bytes, device_hash: str, header: Optional[str],
               n_records: int, now: Optional[float] = None) -> Tuple[bool, str, int]:
    """Verify 'X-Raymap-Pow: <bits>:<nonce>'. Returns (ok, reason, required_bits)."""
    required = target_bits(n_records)
    if not POW_REQUIRED:
        return True, "pow disabled", required
    if not header or ":" not in header:
        return False, "missing proof of work", required
    try:
        bits_s, nonce = header.split(":", 1)
        bits = int(bits_s)
    except (ValueError, AttributeError):
        return False, "malformed proof of work", required
    if bits < required:
        return False, f"insufficient proof of work ({bits} < {required})", required

    digest = hashlib.sha256(raw_body).hexdigest()
    bucket = int((now if now is not None else time.time()) // POW_BUCKET_S)
    # Accept the previous bucket too, so a +/-5 min clock skew still works.
    for b in (bucket, bucket - 1):
        seed = f"{device_hash}|{b}|{digest}|{nonce}".encode()
        if leading_zero_bits(hashlib.sha256(seed).digest()) >= bits:
            return True, "ok", required
    return False, "proof of work does not verify", required


def decompress_capped(body: bytes, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """gzip-inflate with a hard output cap (gzip-bomb guard)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit)
    if d.unconsumed_tail:
        raise ValueError("decompressed body exceeds limit")
    return out


# ---------------------------------------------------------------------------
# Maintenance loop (local replacement for a cloud scheduler)
# ---------------------------------------------------------------------------
def maintain_once() -> Dict[str, Any]:
    """Refresh aggregates, fold detection history, then prune. Order matters:
    pruning last guarantees no data is lost before it is aggregated."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (REFRESH_LOOKBACK_DAYS,))
            cur.execute("SELECT fold_detection_months();")
            cur.execute("SELECT prune_reports(%s);", (RAW_RETAIN_HOURS,))
            cur.execute("SELECT prune_daily_cells(%s);", (CELL_RETAIN_DAYS,))
    return {
        "refreshed_days": REFRESH_LOOKBACK_DAYS,
        "raw_retain_hours": RAW_RETAIN_HOURS,
        "cell_retain_days": CELL_RETAIN_DAYS,
    }


def _maintain_loop(stop: threading.Event) -> None:
    while not stop.wait(MAINTAIN_INTERVAL_S):
        try:
            log.info("maintain: %s", maintain_once())
        except Exception as exc:  # keep the loop alive across DB blips
            log.warning("maintain failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    thread = None
    if MAINTAIN_INTERVAL_S > 0:
        thread = threading.Thread(target=_maintain_loop, args=(stop,), daemon=True)
        thread.start()
        log.info("maintenance loop started (every %ss)", MAINTAIN_INTERVAL_S)
    yield
    stop.set()
    if thread:
        thread.join(timeout=5)


app = FastAPI(title="raymap-map API", version="0.3.0", lifespan=lifespan)

# Local hosting: same origin. Tighten if the UI is ever served elsewhere.
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


def _is_blocked(device_hash: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM blocked_devices WHERE device_hash = %s;", (device_hash,))
            return cur.fetchone() is not None


def _over_quota(device_hash: str, n_records: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM reports "
                "WHERE device_hash = %s AND ts > now() - interval '1 hour';",
                (device_hash,),
            )
            used = cur.fetchone()[0]
    return used + n_records > QUOTA_RECORDS_HOUR


@app.post("/ingest")
async def ingest(request: Request) -> JSONResponse:
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "body too large"}, status_code=413)

    body = raw_body
    if raw_body[:2] == b"\x1f\x8b" or request.headers.get("content-encoding") == "gzip":
        try:
            body = decompress_capped(raw_body)
        except (OSError, ValueError, zlib.error) as exc:
            return JSONResponse({"error": f"invalid gzip body: {exc}"}, status_code=400)

    try:
        batch = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if isinstance(batch, dict):
        records = batch.get("records") or []
        device_hash = str(batch.get("device") or "")
    elif isinstance(batch, list):
        records = batch
        device_hash = ""
    else:
        return JSONResponse({"error": "unexpected body shape"}, status_code=400)
    if not isinstance(records, list):
        return JSONResponse({"error": "records must be a list"}, status_code=400)
    if len(records) > MAX_RECORDS:
        return JSONResponse({"error": f"too many records (>{MAX_RECORDS})"}, status_code=413)

    if not device_hash:
        return JSONResponse({"error": "missing device id"}, status_code=400)

    if _is_blocked(device_hash):
        return JSONResponse({"error": "device blocked"}, status_code=403)

    ok, reason, required = pow_verify(
        raw_body, device_hash, request.headers.get("x-raymap-pow"), len(records)
    )
    if not ok:
        return JSONResponse(
            {"error": reason, "required_bits": required},
            status_code=428,
            headers={"Retry-With-Bits": str(required)},
        )

    if _over_quota(device_hash, len(records)):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

    rows, rejected, first_error = [], 0, None
    for r in records:
        if isinstance(r, dict):
            r = dict(r)
            r.setdefault("device_hash", device_hash)
        if not isinstance(r, dict) or not r.get("device_hash"):
            rejected += 1
            first_error = first_error or "missing device hash"
            continue
        valid, err = validate_record(r)
        if not valid:
            rejected += 1
            first_error = first_error or err
            continue
        rows.append(r)

    inserted = _insert_batch(rows) if rows else 0
    payload: Dict[str, Any] = {"ok": True, "inserted": inserted, "rejected": rejected}
    if first_error:
        payload["first_error"] = first_error
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# GeoJSON for the map
# ---------------------------------------------------------------------------
@app.get("/geojson")
def geojson(days: int = 30, include_detections_only: bool = False) -> JSONResponse:
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
# Maintenance + health
# ---------------------------------------------------------------------------
@app.post("/internal/refresh")
def refresh(days: int = 30) -> JSONResponse:
    days = clamp_days(days)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (days,))
            cur.execute("SELECT fold_detection_months();")
    return JSONResponse({"ok": True, "lookback_days": days})


@app.post("/internal/maintain")
def maintain() -> JSONResponse:
    return JSONResponse({"ok": True, **maintain_once()})


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


_UI_DIR = Path(__file__).resolve().parent / "ui"
if _UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
