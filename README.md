# raymap-map — ingest, aggregation and map UI

Hosts and visualizes crowdsourced Rayhunter hotspot detections on a map of the
US. Target deployment is GCP-native and near-zero cost: **Cloud Run + Cloud
SQL (PostGIS) + Cloud Storage**; local development uses Docker Compose.

> Companion to `raymap-collector`, which polls a USB-tethered hotspot and
> uploads gzipped batches to `POST /ingest`.

## Privacy model

- **No precise coordinates are ever stored.** Each report is snapped at insert
  time to a **geohash-6 cell** (~1.2 km x 0.6 km) and the lat/lon are discarded.
  This replaces per-report jitter, which is defeated by averaging repeated
  reports from a stationary collector.
- Device ids are generated randomly per install and hashed before upload.
- **Single-device cells are shown but marked unconfirmed** (hollow markers).
- Areas with no reports say so explicitly: absence of data is *not* evidence
  of safety.

## Retention

Short by design, with the detection record preserved:

| Data | Retained | Purpose |
|---|---|---|
| `reports` (raw) | **72 hours** | dedup/idempotency, retractions, replay after an aggregation fix |
| `daily_cells` | **90 days** | day x cell aggregates — what the live map reads |
| `detection_months` | **indefinite** | only cells with detections, month x cell (the searchable public record) |

Aggregation and detection-folding run **before** the pruning job, so nothing is
lost when raw rows expire.

## Architecture

```
collector --POST /ingest (gzip JSON)--> Cloud Run: raymap-api
                                          | validate, snap to geohash-6
                                          v
                                   Cloud SQL Postgres + PostGIS
                                          | refresh_daily_cells()
                                          | fold_detection_months()
                                          v
                              daily_cells / detection_months
                                          | export
                                          v
                    Cloud Storage: geojson.json.gz --> browser (cached)
```

- `reports` holds raw rows for 72 h; `daily_cells` is the only table the read
  path touches, so `/geojson` is cheap and cacheable.
- Refreshing is an explicit job (`POST /internal/refresh`), **not** a side
  effect of `/geojson`.
- The Cloudflare Worker ingest path was **removed** in M0: it duplicated
  ingest without the privacy transform.

## Local development

```bash
cd /Users/admin/raymap-map
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

docker compose down -v && docker compose up -d   # reset: schema changed in M0
./.venv/bin/python seed.py                       # schema + ~300k sample reports
./.venv/bin/python -m uvicorn app_api:app --port 8000 --app-dir .

# Open http://localhost:8000 -> MapLibre UI
# New ingests appear after an explicit refresh:
curl -X POST 'http://localhost:8000/internal/refresh?days=7'
```

## API

| Endpoint | Description |
|---|---|
| `POST /ingest` | Collector batch: `{device, count, records:[...]}`, optional gzip, **`X-Raymap-Pow` header required**. Validates every record; returns `{inserted, rejected, first_error?}`. Coordinates are snapped to a cell and discarded. |
| `GET /geojson?days=7&include_detections_only=false` | Aggregated cells as GeoJSON. `days` is bounded to **1..90**; each feature carries `status`, `confidence`, `device_count`, `detection_device_count`, `last_ts`. Response includes `attribution` (CC BY 4.0). |
| `POST /internal/refresh?days=30` | Recompute `daily_cells` and fold `detection_months` (manual). |
| `POST /internal/maintain` | refresh + fold + prune, in that order (runs automatically every `RAYMAP_MAINTAIN_INTERVAL_S`). |
| `GET /healthz` | Liveness plus a database check (503 when the DB is unreachable). |

### Display states (k = 1)

| `status` | Meaning | Rendering |
|---|---|---|
| `detected_corroborated` | >=2 devices reported a detection | solid red |
| `detected_single` | 1 device reported a detection | **hollow red** + "unconfirmed" |
| `clear_multi` | >=2 devices, no detections | solid green |
| `clear_single` | 1 device, no detections | hollow green |
| *(no feature)* | no reports in the window | legend note: not evidence of safety |

Tunable with `RAYMAP_CELL_PRECISION` (default `6`; 5 ~= 4.9 km, 7 ~= 153 m).

## Ingest protection

Local hosting is still an open endpoint, so ingest is gated:

| Control | Behaviour |
|---|---|
| **Proof of work** | `X-Raymap-Pow: <bits>:<nonce>`; `bits = 12 + ceil(log2(records))`, bound to the device, the raw body and a 5-minute bucket. Missing or insufficient → **428** + `Retry-With-Bits`. |
| Body cap | 1 MB compressed / 4 MB decompressed (gzip-bomb guard) → **413** / **400** |
| Record cap | 500 records per batch → **413** |
| Per-device quota | 600 records/hour → **429** |
| Blocklist | `blocked_devices` table → **403** |

An honest batch costs a few milliseconds of CPU; a 10k-record poisoning batch costs roughly
2^26 hashes (~1–2 CPU-minutes). `RAYMAP_POW_REQUIRED=0` disables the check — only for a
throwaway local box, never on a reachable host.

## Local hosting

```bash
./.venv/bin/python -m uvicorn app_api:app --port 8000 --app-dir .
```

The app runs its own maintenance loop (refresh → fold → prune, every 5 minutes), so no cron
is needed. Knobs:

| Env var | Default | Purpose |
|---|---|---|
| `RAYMAP_MAINTAIN_INTERVAL_S` | `300` | maintenance cadence (`0` disables the loop) |
| `RAYMAP_RAW_RETAIN_HOURS` | `72` | raw report retention |
| `RAYMAP_CELL_RETAIN_DAYS` | `90` | daily-cell retention |
| `RAYMAP_QUOTA_RECORDS_HOUR` | `600` | per-device ingest quota |
| `RAYMAP_POW_REQUIRED` | `1` | disable proof of work (local experiments only) |

Ban a device (find its id with `SELECT DISTINCT device_hash FROM reports`):

```sql
INSERT INTO blocked_devices (device_hash, reason) VALUES ('<hash>', 'poisoning');
```

## Files

| File | Purpose |
|---|---|
| `schema.sql` | PostGIS, `reports`, `daily_cells`, `detection_months`, refresh/fold/prune functions |
| `app_api.py` | FastAPI: `/ingest`, `/geojson`, `/internal/refresh`, `/healthz`, serves `ui/` |
| `ui/` | MapLibre GL + OpenFreeMap UI (vendored MapLibre v3.6.2, no CDN) |
| `seed.py` | Schema apply + simulated reports (cell-snapped) |
| `adb_reader.py` | Interim path: pull device data over ADB and post to `/ingest` |
| `cron.sql` | Equivalent `pg_cron` schedule for databases that have it (GCP uses Cloud Scheduler) |
| `docker-compose.yml` | Local `postgis/postgis:16-3.4` on port 5433 |
| `test_api.py` | Unit tests: classification, validation, bounds, proof-of-work, gzip cap |
| `test_adb.py` | Unit tests: install pseudonym, timestamp clamp, log redaction |
| `LICENSE` / `LICENSE-DATA` / `NOTICE` | Apache-2.0 code, CC BY 4.0 data, third-party attribution |

## Tests

```bash
./.venv/bin/python -m unittest -q test_api test_adb   # 31 tests
```

No database or device required. The tests cover the pure helpers
(`classify`, `validate_record`, `clamp_days`, cell-precision bounds), the ingest
security rules (proof-of-work difficulty and binding, gzip-bomb cap), and the ADB
reader's privacy helpers (install pseudonym, timestamp clamp, log redaction).

## Hosting

Hosted **locally** for now: uvicorn serves the API + UI, PostGIS runs in Docker Compose, and
the app's own maintenance loop handles aggregation and retention. The cloud path (Cloud Run +
Cloud SQL + Cloud Storage + Cloud Scheduler, with the HCP Terraform workspace) is deferred
until the project moves off localhost; nothing in this repo depends on it.

## License

Code: **Apache-2.0** (`LICENSE`). Aggregate data: **CC BY 4.0**
(`LICENSE-DATA`) — attribution: *"Raymap aggregate data (c) contributors,
CC BY 4.0"*. Third-party notices (MapLibre BSD-3, OpenFreeMap/OpenMapTiles/OSM
ODbL, Rayhunter GPL-3 interop) are in `NOTICE`.
