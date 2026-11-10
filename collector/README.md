# raymap-collector

The device client for [raymapper](https://raymap.org): it reads a hotspot running
[Rayhunter](https://github.com/EFForg/rayhunter) and uploads one compact status record per
cycle to the map.

> Part of the raymapper monorepo: this directory is the device client, the map service is
> at the repository root. Run everything below from `collector/`.

It talks to the hotspot over USB and uploads over the computer's own internet connection — no
mobile data. The ADB serial, IMSI, IMEI and phone number are never read into a record, logged or
transmitted; the only identifier that leaves the machine is a hash of a random per-install id.

> **Shipped builds** are self-contained binaries attached to
> [GitHub Releases](https://github.com/ffenyeroc/raymapper/releases): unpack, run
> `./raymapper-collector --version`, then let it run. `adb` is bundled, and the only
> requirement is a hotspot with Rayhunter installed and USB debugging enabled. Running from
> this source tree (below) is for development.
>
> **macOS:** the archives are ad-hoc signed but **not notarised**. If Gatekeeper blocks the
> first launch ("cannot be verified" or "is damaged"), right-click → *Open*, or clear the flag
> once with `xattr -dr com.apple.quarantine raymapper-collector`. Downloads made with `curl`
> or `gh` are not quarantined and start normally.
>
> **Linux:** built against **glibc 2.35** — Ubuntu 22.04+, Debian 12+ and newer. Older
> distributions fail with `GLIBC_2.34 not found`. `adb` needs USB access: a `no permissions`
> error means no udev rule for the device and no membership in `plugdev`.

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
| `../launchd/org.raymap.device.plist` | `launchd` LaunchAgent that runs the collector at login (installed via `scripts/install_services.sh`) |
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
   - `server_url = "https://api.raymap.org/ingest"`
   - `gps_source = "fixed"` with your lat/lon, or `"api"` to read from the device.
   - `device_id` (optional — a random one is generated and hashed if omitted).
2. Run:
   ```bash
   ./.venv/bin/python collector.py -v
   ```

## Run as a background macOS service (launchd)

The LaunchAgent at the repository root also works for this client — it wraps `adb_reader.py`,
which is the same poll-and-upload loop:

```bash
./scripts/install_services.sh install     # copies launchd/org.raymap.device.plist into ~/Library/LaunchAgents
./scripts/install_services.sh status      # launchctl list | grep raymap
./scripts/install_services.sh uninstall   # stop and remove
```

The installed copy has this checkout's paths substituted in, so it survives being run from any
directory. Logs go to `~/Library/Logs/raymap-device.log`.

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

`rssi_dbm`, `detection`, and `network` are populated where the corresponding Rayhunter
endpoint provides them, and left null otherwise — a null means "not reported", not "no
detection".

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

## Notes and limitations

- Most hotspots expose a CDC-ECM interface and are reachable over ADB without extra drivers.
  macOS no longer ships an RNDIS driver, so a device that offers only RNDIS will not enumerate
  there — a platform limitation rather than a collector setting. `discovery.py` probes whatever
  interface the OS presents.
- The design assumes a USB-attached station, so it reports one place rather than a vehicle in
  motion. That is deliberate: the map publishes cells, not routes.
- The map service is the root of this repository (`POST /ingest`, default
  `https://api.raymap.org/ingest`). If the endpoint is unreachable the uploader keeps records in
  its local queue and retries, so nothing is dropped.
