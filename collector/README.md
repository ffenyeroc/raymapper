# raymap-collector (macOS)

A **pure-Python + `requests`** dev version of the USB collector for the
[crowdsourced Rayhunter map](https://github.com/EFForg/rayhunter) project.

> Part of the raymapper monorepo: this directory is the device client, the map service is
> at the repository root. Run everything below from `collector/`.

It polls the Rayhunter daemon on a **USB-tethered Orbic RC400L** over the local
`:8080` API, then periodically uploads compact, gzipped JSON status records to
a map server. It is station-local: the computer talks to the hotspot over USB
(no mobile-data cost for telemetry) and pushes aggregated updates over its own
internet connection.

> This is the **development** build. It assumes Python + `requests` are
> available (use the included venv). A distribution-ready, dependency-free
> `.app` bundle for non-technical users comes later.

---

## Files

| File | Purpose |
|---|---|
| `collector.py` | Main loop: discover → poll → queue → upload. CLI entry point. |
| `discovery.py` | Finds the USB-tethered hotspot's gateway and confirms Rayhunter on `:8080`. |
| `rayhunter.py` | Thin client for the device's local REST API (`/api/gps`, `/api/time`, `/api/config`, …). |
| `uploader.py` | On-disk queue + gzipped JSON upload with retry/backoff. |
| `pow.py` | Proof-of-work header (`X-Raymap-Pow`) bound to the device and payload. |
| `config.py` / `config.toml` | Config loading (stdlib `tomllib`) + editable settings. |
| `mock_rayhunter.py` | Fake of the device API for testing without hardware. |
| `mock_map_server.py` | Receiver that prints/records uploaded batches — verifies the upload half. |
| `org.raymap.collector.plist` | `launchd` LaunchAgent manifest (runs it in the background). |
| `test_collector.py` | Unit tests for the poll/upload schedule (no device required). |
| `requirements.txt` | Only `requests`. |

Licences live at the repository root: code is Apache-2.0 (`../LICENSE`), and `../NOTICE`
covers the third-party positions, including that Rayhunter is a separate GPL-3.0 program
(no code copied).

---

## Tests

```bash
./.venv/bin/python -m unittest -q test_collector
```

Covers `should_upload` (interval vs. change, and retry-after-failure timing),
`status_key` (timestamp is ignored), the device-id hash, and proof of work
(difficulty scaling, payload binding).

---

## Quick start (dev)

```bash
cd collector
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 1) Terminal 1 — fake hotspot:
./.venv/bin/python mock_rayhunter.py --port 8080

# 2) Terminal 2 — fake map server (receiver):
./.venv/bin/python mock_map_server.py --port 9000

# 3) Terminal 3 — the collector (single cycle, verbose):
./.venv/bin/python collector.py --once --mock -v
```

To point at a real map server and real USB device:

1. Edit `config.toml`:
   - `server_url = "https://your.map.server/ingest"`
   - `gps_source = "fixed"` with your lat/lon, or `"api"` to read from the device.
   - `device_id` (optional — a random one is generated and hashed if omitted).
2. Run:
   ```bash
   ./.venv/bin/python collector.py -v
   ```

## Run as a background macOS service (launchd)

```bash
cp org.raymap.collector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/org.raymap.collector.plist
# stop with: launchctl unload ~/Library/LaunchAgents/org.raymap.collector.plist
```
Edit the two absolute paths in the plist first to match your checkout.

---

## What gets uploaded

Records look like (after gzipping and hashing the device id):

```json
{
  "device": "24903bf29ccf65b1",
  "count": 1,
  "records": [
    {"ts": 1786728901, "lat": 40.7128, "lon": -74.006,
     "gps_source": "fixed", "rssi_dbm": null, "detection": null, "network": null}
  ]
}
```

`rssi_dbm`, `detection`, and `network` are placeholders for v1 — they will be
populated once the corresponding Rayhunter endpoints are confirmed.

## Design points
- **Precedence of transport**: device is polled over USB (cheap, reliable); only
  small, gzipped batches go upstream over the host's internet.
- **Bandwidth discipline**: poll locally every 60s, upload at most every 900s
  (or on change), deltas/gzip so idle periods cost ~nothing.
- **Abuse resistance**: every upload carries a proof-of-work header sized to the
  record count (`12 bits + 1 per doubling`, bound to the device and payload), so
  flooding the map costs CPU, not just bandwidth.
- **Resilience**: records are queued to `state.jsonl` on disk; if the server is
  unreachable the collector backs off (up to 5 min) and never loses the latest
  point. Restart-safe and idempotent.
- **Privacy**: the device id is **hashed before upload**; location is user-set
  (fixed) or sourced from the device.

## Notes / limitations
- macOS RNDIS-tethered devices may need the `HoRNDIS` driver; CDC-ECM devices
  work natively. `discovery.py` probes whatever interface presents.
- This is a stationary (USB-attached) design — it is not for real-time mobile
  tracking, which intentionally fits the privacy-preserving aggregate map.
- The map service lives in the companion `raymap-map` project (`POST /ingest`).
  Set `server_url` to its ingest endpoint; until then the uploader queues and
  logs locally without losing records.
