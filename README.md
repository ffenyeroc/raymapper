# raymap-map — ingest, aggregation and hex map UI

Hosts and visualizes crowdsourced Rayhunter hotspot detections as an **H3 hexagon mesh**
over the US. Hosted locally for now: uvicorn + PostGIS in Docker Compose. The cloud path
(Cloud Run + Cloud SQL + Cloud Storage) is deferred until the project moves off localhost.

> Companion to `raymap-collector`, which polls a USB-tethered hotspot and uploads gzipped
> batches to `POST /ingest`.

## Privacy model

- **No precise coordinates are ever stored.** Each report is snapped to **H3 cells at
  resolutions 3–8** and the lat/lon are discarded.
- **The smallest cell the map shows is r6** (36 km², ~7.4 km across) at any zoom. r7 and r8
  are still stored (r7 keys the detection archive) but the API clamps requests to r3–r6, so
  the floor is enforced server-side, not just in the UI.
- Device ids are random per install and hashed before upload. Single-device cells are shown
  but **marked unconfirmed** (hollow).
- **Grey means “no reports here”, not “safe”.** The UI says so in the legend and the tooltip.

| res | area | across | used at zoom |
|---|---|---|---|
| r3 | 12,393 km² | ~138 km | ≤ 5 (national — ~10 cells for a median state) |
| r4 | 1,770 km² | ~52 km | 6–7 |
| r5 | 253 km² | ~20 km | 8 |
| r6 | 36 km² | ~7.4 km | ≥ 9 — **smallest cell shown** |
| r7 | 5.2 km² | ~2.8 km | stored only (detection-archive key) |
| r8 | 0.74 km² | ~1.06 km | stored only |

## Retention

| Data | Retained | Purpose |
|---|---|---|
| `reports` (raw) | **72 hours** | dedup/idempotency, retractions, replay after an aggregation fix |
| `daily_cells` | **90 days** | `(day, res, cell)` aggregates — what the map reads |
| `detection_months` | **indefinite** | only cells with detections, month × H3 r7 (the searchable public record) |

Aggregation and detection-folding run **before** pruning, so nothing is lost when raw rows expire.

## How the map is drawn

- The API returns **only cells that have reports**, filtered to the requested viewport.
- The UI generates the rest of the mesh itself: `h3.polygonToCells()` over the padded
  viewport, clipped to the vendored US boundary with a small point-in-polygon test, then
  merged with the API data by H3 index. Cells with no reports render grey.
- Resolution follows zoom (table above) and never goes finer than r6; if a viewport would
  exceed 8,000 cells the UI steps down a level automatically.
- Reported cells that fall outside the simplified coastline are still drawn.
- **Search** (top bar): a ZIP code, `ZIP+4`, `town, ST`, or a bare town name flies the map to
  that place. Lookup is a vendored Census gazetteer (`ui/data/places.json.gz`, ~32k places and
  ~34k ZIPs) so queries never leave the browser — no geocoding service, no leaked IPs.

## Running locally as a service

`./scripts/install_services.sh install` installs two launchd agents — the API (KeepAlive,
bound to 127.0.0.1:8000) and a 03:30 nightly `pg_dump` — so the deployment survives reboots
and the database is backed up. Configuration lives in `.env` (copy `.env.example`).

Start/stop, backups, restore drills, moderation, upgrades and troubleshooting are in
[`RUNBOOK.md`](RUNBOOK.md).

## Local development

```bash
cd /Users/admin/raymap-map
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

docker compose down -v && docker compose up -d   # reset: the schema is H3-based
./.venv/bin/python seed.py --cluster             # schema + clustered sample reports
./.venv/bin/python -m uvicorn app_api:app --port 8000 --app-dir .

# Open http://localhost:8000
```

## API

| Endpoint | Description |
|---|---|
| `POST /ingest` | Collector batch: `{device, count, records:[...]}`, optional gzip, **`X-Raymap-Pow` required**. Returns `{inserted, rejected, first_error?}`. |
| `GET /geojson?days=7&res=6&bbox=w,s,e,n` | Aggregated hex cells. `days` bounded 1–90, `res` clamped to **3–6** (`res=7|8` returns r6), `bbox` optional (viewport filter). Features carry `h3`, `res`, `status`, `confidence`, counts and `last_ts`; the response carries `attribution` (CC BY 4.0). |
| `POST /internal/refresh?days=30` | Recompute `daily_cells` (all resolutions) and fold `detection_months`. |
| `POST /internal/maintain` | refresh + fold + prune, in that order (runs automatically every `RAYMAP_MAINTAIN_INTERVAL_S`). |
| `GET /detections?start=YYYY-MM&end=YYYY-MM&res=6&min_devices=1&bbox=w,s,e,n` | Month-level **detection history** from the archive, folded to ≤ r6 so history never resolves finer than the map. CC BY 4.0. |
| `POST /internal/block` · `DELETE /internal/block/{hash}` | Block/unblock a device (audited). |
| `POST /internal/retract` | Delete one day of reports/aggregates and re-fold that month (audited). |
| `GET /internal/audit` | Recent operator actions. |
| `GET /healthz` | Liveness plus a database check. |

### Display states (six)

| `status` | Meaning | Rendering |
|---|---|---|
| `detected_corroborated` | ≥2 devices reported a detection | solid red |
| `detected_single` | 1 device reported a detection | **hollow red** + “unconfirmed” |
| `clear_multi` | ≥2 devices, no detections | solid green |
| `clear_single` | 1 device, no detections | hollow green |
| *(no feature)* → `no_data` | no reports in this window | **grey** — explicitly not evidence of safety |

## Moderation and the audit trail

`/internal/*` mutates data, so it is **loopback-only by default**. Set a token to allow
remote use; send it as `X-Raymap-Admin`.

```bash
# block a device (quota/poisoning), and unblock it again
curl -X POST localhost:8000/internal/block -H 'content-type: application/json' \
     -d '{"device_hash":"<hash>","reason":"poisoning"}'
curl -X DELETE localhost:8000/internal/block/<hash>

# retract a whole day (deletes its raw reports and aggregates, then re-folds that month)
curl -X POST localhost:8000/internal/retract -H 'content-type: application/json' \
     -d '{"day":"2026-11-02","reason":"bad upload"}'

# what happened
curl localhost:8000/internal/audit?limit=20
```

Retraction clears that month from `detection_months` and re-folds from what remains, so it
is not undone by the monotonic fold. Every action is written to `audit_log`.

## History exports (CC BY 4.0)

```bash
./.venv/bin/python scripts/export_history.py
```

Writes `ui/data/history/YYYY-MM.ndjson.gz` plus `index.json` (attribution, licence, per-month
totals), served statically at `/data/history/…`. Detection cells only — presence data is not
retained past 90 days. The files are generated, so they are gitignored; re-run monthly or
after a retraction.

## Ingest protection

| Control | Behaviour |
|---|---|
| **Proof of work** | `X-Raymap-Pow: <bits>:<nonce>`; `bits = 12 + ceil(log2(records))`, bound to device + raw body + 5-minute bucket. Missing/insufficient → **428** + `Retry-With-Bits`. |
| Body cap | 1 MB compressed / 4 MB decompressed (gzip-bomb guard) → **413** / **400** |
| Record cap | 500 records per batch → **413** |
| Per-device quota | 600 records/hour → **429** |
| Blocklist | `blocked_devices` table → **403** |

`RAYMAP_POW_REQUIRED=0` disables the check — only for a throwaway local box.

## Local hosting knobs

| Env var | Default | Purpose |
|---|---|---|
| `RAYMAP_MAINTAIN_INTERVAL_S` | `300` | maintenance cadence (`0` disables the loop) |
| `RAYMAP_RAW_RETAIN_HOURS` | `72` | raw report retention |
| `RAYMAP_CELL_RETAIN_DAYS` | `90` | daily-cell retention |
| `RAYMAP_QUOTA_RECORDS_HOUR` | `600` | per-device ingest quota |
| `RAYMAP_HEX_RES` | `6` | default resolution when a request omits `res` |
| `RAYMAP_POW_REQUIRED` | `1` | disable proof of work (local only) |
| `RAYMAP_ADMIN_TOKEN` | *(unset)* | allow remote `/internal/*`; sent as `X-Raymap-Admin` |

Ban a device: `INSERT INTO blocked_devices (device_hash, reason) VALUES ('<hash>', 'poisoning');`

## Files

| File | Purpose |
|---|---|
| `schema.sql` | PostGIS + H3 `reports`, `daily_cells(day,res,cell)`, `detection_months`, refresh/fold/prune |
| `app_api.py` | FastAPI: `/ingest`, `/geojson`, `/internal/*`, `/healthz`, serves `ui/` |
| `ui/app.js` | Hex mesh, res-by-zoom, six display states |
| `ui/geo.js` | Point-in-polygon, bbox helpers, zoom→res ladder (dependency-free, node-tested) |
| `ui/search.js` | Offline ZIP/place lookup (dependency-free, node-tested) |
| `ui/data/places.json.gz` | Census gazetteer index for search (~800 KB gz, loaded on first search) |
| `ui/data/us_states.geojson` | US land/states boundary, vendored (Census, public domain, simplified) |
| `ui/vendor_h3-js.js` / `ui/vendor_maplibre-gl.js` | Vendored H3 4.5.0 and MapLibre 3.6.2 (no CDN) |
| `seed.py` | Schema apply + simulated reports (`--cluster` for realistic density) |
| `adb_reader.py` | Interim path: pull device data over ADB and post to `/ingest` |
| `scripts/migrate_to_hex.py` | One-time geohash→H3 migration (presence rebuilt, archive preserved) |
| `scripts/check_state_coverage.py` | Per-state cell counts + mesh coverage check |
| `scripts/build_places.py` | Rebuilds `ui/data/places.json.gz` from the Census gazetteer (`--download`) |
| `launchd/`, `scripts/install_services.sh` | launchd agents for the API and nightly backups |
| `scripts/backup_db.sh` / `scripts/run_api.sh` | `pg_dump` with pruning / service entry point |
| `RUNBOOK.md` | Operations: start/stop, backups, restore drill, moderation, upgrades |
| `test_api.py`, `test_adb.py`, `ui/test_geo.js`, `ui/test_search.js` | Unit tests |
| `LICENSE` / `LICENSE-DATA` / `NOTICE` | Apache-2.0 code, CC BY 4.0 data, third-party attribution |

## Tests

```bash
./.venv/bin/python -m unittest -q test_api test_adb   # 34 tests
node --test ui/test_geo.js ui/test_search.js          # 14 tests
./.venv/bin/python scripts/check_state_coverage.py    # geometry + coverage check
```

CI runs the same on every push/PR, plus gitleaks and an advisory `pip-audit`.
(`.github/workflows/ci.yml`)

### Serving note

`data/places.json.gz` must be served **without** `Content-Encoding: gzip` — the browser
fetches the raw bytes and inflates them with `DecompressionStream`. A proxy or CDN that
rewrites the header would double-decompress it; if that is unavoidable, serve a plain
`.json` instead and drop the `pipeThrough` call in `ui/app.js`.

## Hosting

Local: uvicorn serves the API + UI, PostGIS runs in Docker Compose, and the app's own
maintenance loop handles aggregation and retention. The cloud path (Cloud Run + Cloud SQL +
Cloud Storage + Cloud Scheduler, with an HCP Terraform workspace) is deferred; nothing here
depends on it.

## License

Code **Apache-2.0** (`LICENSE`). Aggregate data **CC BY 4.0** (`LICENSE-DATA`) —
*“Raymap aggregate data (c) contributors, CC BY 4.0”*. Third-party notices in `NOTICE`
(MapLibre BSD-3, H3/h3-js Apache-2.0, US Census boundary public domain, OpenFreeMap/
OpenMapTiles/OSM ODbL, Rayhunter GPL-3 interop).
