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
import h3
import json
import logging
import os
import secrets
import threading
import time
import zlib
from contextlib import asynccontextmanager
from datetime import date
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

# /internal/* mutates data. It is loopback-only unless a token is configured;
# set RAYMAP_ADMIN_TOKEN before exposing this host to a network.
ADMIN_TOKEN = os.environ.get("RAYMAP_ADMIN_TOKEN", "")

# Stored per report: r7 is the detection-archive key, r8 the original floor.
HEX_RESOLUTIONS = tuple(range(3, 9))
# Served/displayed band. The map's minimum cell size is r6 (~7.4 km across), so
# the API refuses anything finer even though r7/r8 remain stored.
HEX_RES_MIN = 3
HEX_RES_MAX = 6
DEFAULT_RES = max(
    HEX_RES_MIN, min(HEX_RES_MAX, int(os.environ.get("RAYMAP_HEX_RES", "6")))
)

MAX_DAYS = 90            # /geojson lookback bound (also presence retention)
MAX_FEATURES = 20000     # response cap
MAX_ROWS = 200000        # SQL safety cap before viewport filtering

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


def clamp_res(res: int) -> int:
    """Bound resolution to the privacy band (r3 min, r8 max)."""
    try:
        res = int(res)
    except (TypeError, ValueError):
        return DEFAULT_RES
    return max(HEX_RES_MIN, min(HEX_RES_MAX, res))


def parse_bbox(value: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    """Parse 'west,south,east,north' into a normalized tuple, or None."""
    if not value:
        return None
    try:
        w, s, e, n = (float(x) for x in value.split(","))
    except (TypeError, ValueError):
        return None
    return (min(w, e), min(s, n), max(w, e), max(s, n))


def parse_month(value: Optional[str]) -> Optional[date]:
    """Parse 'YYYY-MM' (or 'YYYY-MM-DD') into the first of that month."""
    if not value:
        return None
    try:
        return date.fromisoformat(value + "-01" if len(value) == 7 else value).replace(day=1)
    except ValueError:
        return None


def admin_allowed(client_host: Optional[str], token: Optional[str]) -> bool:
    """Admin endpoints mutate data: require the token when one is configured,
    otherwise accept loopback callers only."""
    if ADMIN_TOKEN:
        return secrets.compare_digest(token or "", ADMIN_TOKEN)
    hygienic = {"127.0.0.1", "::1", "localhost"}
    return client_host in hygienic


def fold_history(rows: List[Tuple[Any, ...]], res: int) -> Dict[Tuple[date, str], Tuple[int, int, Optional[Any]]]:
    """Aggregate archive rows (month, cell@r7, detections, detecting devices,
    last_report) up to `res`. Pure, so it can be unit tested."""
    agg: Dict[Tuple[date, str], Tuple[int, int, Optional[Any]]] = {}
    for month, cell, det, det_dev, last in rows:
        parent = cell if h3.get_resolution(cell) == res else h3.cell_to_parent(cell, res)
        key = (month, parent)
        d, v, t = agg.get(key, (0, 0, None))
        stamps = [x for x in (t, last) if x is not None]
        agg[key] = (d + det, max(v, det_dev), max(stamps) if stamps else None)
    return agg


def h3_columns(lat: float, lng: float) -> Dict[str, str]:
    """H3 index for every stored resolution (r3..r8)."""
    return {f"h3_r{r}": h3.latlng_to_cell(lat, lng, r) for r in HEX_RESOLUTIONS}


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
INSERT INTO reports (device_hash, ts, h3_r3, h3_r4, h3_r5, h3_r6, h3_r7, h3_r8,
                     rssi_dbm, detection, network)
VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (device_hash, ts) DO NOTHING
"""


def _insert_batch(rows: List[Dict[str, Any]]) -> int:
    """Insert validated rows; coordinates become H3 cells and are discarded."""
    inserted = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cells = h3_columns(float(r["lat"]), float(r["lon"]))
                cur.execute(
                    INSERT_REPORT,
                    (
                        r["device_hash"],
                        float(r["ts"]),
                        *(cells[f"h3_r{res}"] for res in HEX_RESOLUTIONS),
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
def geojson(days: int = 30, res: int = DEFAULT_RES,
            include_detections_only: bool = False,
            bbox: Optional[str] = None) -> JSONResponse:
    days = clamp_days(days)
    res = clamp_res(res)
    box = parse_bbox(bbox)
    query = """
        SELECT day, cell, device_count, report_count,
               detection_count, detection_device_count, last_ts
        FROM daily_cells
        WHERE res = %s
          AND day >= (now() - make_interval(days => %s))::date
    """
    params: List[Any] = [res, days]
    if include_detections_only:
        query += " AND detection_count > 0"
    # H3 centroids are not available in SQL, so viewport filtering happens in
    # Python: never truncate before filtering, cap the response afterwards.
    query += " ORDER BY report_count DESC LIMIT " + str(MAX_ROWS)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    features = []
    for day, cell, dev, rep, det, det_dev, last_ts in rows:
        lat, lng = h3.cell_to_latlng(cell)
        if box and not (box[0] <= lng <= box[2] and box[1] <= lat <= box[3]):
            continue
        if len(features) >= MAX_FEATURES:
            break
        status, confidence = classify(dev, det_dev)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "h3": cell,
                    "res": res,
                    "day": str(day),
                    "device_count": dev,
                    "report_count": rep,
                    "detection_count": det,
                    "detection_device_count": det_dev,
                    "status": status,
                    "confidence": confidence,
                    "last_ts": last_ts.isoformat() if last_ts else None,
                },
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
            }
        )

    return JSONResponse(
        {
            "type": "FeatureCollection",
            "attribution": ATTRIBUTION,
            "license": "CC BY 4.0",
            "days": days,
            "res": res,
            "features": features,
        }
    )


# ---------------------------------------------------------------------------
# Maintenance + health
# ---------------------------------------------------------------------------
def _guard(request: Request) -> Optional[JSONResponse]:
    """/internal/* mutates data: loopback-only unless RAYMAP_ADMIN_TOKEN is set."""
    host = request.client.host if request.client else None
    if admin_allowed(host, request.headers.get("x-raymap-admin")):
        return None
    return JSONResponse(
        {
            "error": "admin endpoints are loopback-only",
            "hint": "set RAYMAP_ADMIN_TOKEN and send X-Raymap-Admin for remote use",
        },
        status_code=403,
    )


def _audit(cur, action: str, details: Dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, action, details) VALUES ('operator', %s, %s);",
        (action, json.dumps(details)),
    )


@app.post("/internal/refresh")
def refresh(request: Request, days: int = 30) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    days = clamp_days(days)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_daily_cells(%s);", (days,))
            cur.execute("SELECT fold_detection_months();")
            _audit(cur, "refresh", {"days": days})
    return JSONResponse({"ok": True, "lookback_days": days})


@app.post("/internal/maintain")
def maintain(request: Request) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    result = maintain_once()
    with _conn() as conn:
        with conn.cursor() as cur:
            _audit(cur, "maintain", result)
    return JSONResponse({"ok": True, **result})


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


@app.post("/internal/block")
async def block(request: Request) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    device_hash = str((body or {}).get("device_hash") or "").strip()
    if not device_hash:
        return JSONResponse({"error": "device_hash is required"}, status_code=400)
    reason = str((body or {}).get("reason") or "")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO blocked_devices (device_hash, reason) VALUES (%s, %s) "
                "ON CONFLICT (device_hash) DO UPDATE SET reason = EXCLUDED.reason",
                (device_hash, reason),
            )
            _audit(cur, "block_device", {"device_hash": device_hash, "reason": reason})
    return JSONResponse({"ok": True, "blocked": device_hash})


@app.delete("/internal/block/{device_hash}")
def unblock(request: Request, device_hash: str) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM blocked_devices WHERE device_hash = %s", (device_hash,))
            removed = cur.rowcount
            _audit(cur, "unblock_device", {"device_hash": device_hash, "removed": removed})
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/internal/retract")
async def retract(request: Request) -> JSONResponse:
    """Delete one day of reports and its aggregates (e.g. after poisoning).

    The affected month is also cleared from the detection archive and re-folded
    from what remains, so a retraction is not undone by the monotonic fold.
    """
    denied = _guard(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    day_raw = str((body or {}).get("day") or "")
    try:
        day = date.fromisoformat(day_raw)
    except ValueError:
        return JSONResponse({"error": "day must be YYYY-MM-DD"}, status_code=400)
    reason = str((body or {}).get("reason") or "")
    month = day.replace(day=1)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reports WHERE ts::date = %s", (day,))
            reports = cur.rowcount
            cur.execute("DELETE FROM daily_cells WHERE day = %s", (day,))
            cells = cur.rowcount
            cur.execute("DELETE FROM detection_months WHERE month = %s", (month,))
            cur.execute("SELECT refresh_daily_cells(%s);", (REFRESH_LOOKBACK_DAYS,))
            cur.execute("SELECT fold_detection_months();")
            _audit(
                cur,
                "retract_day",
                {"day": str(day), "reason": reason,
                 "reports_deleted": reports, "cells_deleted": cells},
            )
    return JSONResponse(
        {"ok": True, "day": str(day), "reports_deleted": reports, "cells_deleted": cells}
    )


@app.get("/internal/audit")
def audit_log(request: Request, limit: int = 100) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    limit = max(1, min(1000, limit))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT at, actor, action, details FROM audit_log ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
    return JSONResponse(
        {
            "events": [
                {"at": r[0].isoformat(), "actor": r[1], "action": r[2], "details": r[3]}
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Detection history (the searchable public record)
# ---------------------------------------------------------------------------
@app.get("/detections")
def detections(start: Optional[str] = None, end: Optional[str] = None,
               res: int = HEX_RES_MAX, min_devices: int = 1,
               bbox: Optional[str] = None, limit: int = MAX_FEATURES) -> JSONResponse:
    """Month-level detection history from the archive.

    The archive is keyed at r7; rows are folded up to `res`, clamped to the same
    3-6 band as the map so history can never resolve finer than the map does.
    """
    res = clamp_res(res)
    box = parse_bbox(bbox)
    start_month = parse_month(start)
    end_month = parse_month(end)
    if start and start_month is None:
        return JSONResponse({"error": "start must be YYYY-MM"}, status_code=400)
    if end and end_month is None:
        return JSONResponse({"error": "end must be YYYY-MM"}, status_code=400)
    limit = max(1, min(MAX_FEATURES, limit))
    min_devices = max(1, min_devices)

    query = (
        "SELECT month, cell, detection_count, detection_device_count, last_report "
        "FROM detection_months WHERE TRUE"
    )
    params: List[Any] = []
    if start_month:
        query += " AND month >= %s"
        params.append(start_month)
    if end_month:
        query += " AND month <= %s"
        params.append(end_month)
    query += " ORDER BY month DESC LIMIT " + str(MAX_ROWS)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    agg = fold_history(rows, res)
    features = []
    for (month, cell), (det, det_dev, last) in sorted(
        agg.items(), key=lambda kv: kv[1][0], reverse=True
    ):
        if det_dev < min_devices:
            continue
        lat, lng = h3.cell_to_latlng(cell)
        if box and not (box[0] <= lng <= box[2] and box[1] <= lat <= box[3]):
            continue
        if len(features) >= limit:
            break
        status, confidence = classify(0, det_dev)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "h3": cell,
                    "res": res,
                    "month": str(month),
                    "detection_count": det,
                    "detection_device_count": det_dev,
                    "status": status,
                    "confidence": confidence,
                    "last_report": last.isoformat() if last else None,
                },
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
            }
        )

    return JSONResponse(
        {
            "type": "FeatureCollection",
            "attribution": ATTRIBUTION,
            "license": "CC BY 4.0",
            "res": res,
            "features": features,
        }
    )


_UI_DIR = Path(__file__).resolve().parent / "ui"
if _UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
