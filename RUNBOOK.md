# Runbook — local raymap deployment

## What runs

| Component | Managed by | Logs |
|---|---|---|
| PostgreSQL 16 | Docker Compose (`restart: unless-stopped`) | `docker logs raymap_db` |
| API + map UI | launchd `org.raymap.api` (KeepAlive, binds 127.0.0.1:8000) | `~/Library/Logs/raymap-api.log` |
| Nightly backup | launchd `org.raymap.backup` (03:30) | `~/Library/Logs/raymap-backup.log` |
| Device reader | launchd `org.raymap.device` (posts every 15 min while a hotspot is attached) | `~/Library/Logs/raymap-device.log` |

Docker Desktop must start at login for the database to come back after a reboot
(Settings → General → *Start Docker Desktop when you sign in*). The API waits up to 60 s
for the port before starting, so boot order is not critical.

## Everyday commands

```bash
./scripts/install_services.sh status     # are the agents loaded?
./scripts/install_services.sh install    # install/refresh all agents
./scripts/install_services.sh uninstall  # stop and remove them

launchctl kickstart -k gui/$(id -u)/org.raymap.api   # restart the API

docker compose up -d                     # database
curl -s localhost:8000/healthz           # {"status":"ok"}
tail -f ~/Library/Logs/raymap-api.log    # API log
```

In `launchctl list`, the first column is the PID (present = running) and the second the last
exit status; a negative value such as `-15` just means it was stopped by a signal (a
`kickstart -k` restart), not a crash.

The API binds **127.0.0.1** by default, which keeps `/internal/*` loopback-only. To serve the
map to your LAN, set `RAYMAP_BIND=0.0.0.0` **and** `RAYMAP_ADMIN_TOKEN` in `.env`.

### Log rotation

launchd appends to `~/Library/Logs/raymap-*.log` forever. Install the supplied
`newsyslog` rule so they cap at 10 MB (API) / 1 MB (backup) and keep 7 compressed
generations:

```bash
sudo cp launchd/raymap.newsyslog.conf /etc/newsyslog.d/raymap.conf
```

Without it, truncate by hand: `: > ~/Library/Logs/raymap-api.log` (the running process
keeps its file descriptor, so the file regrows from empty).

## Configuration

`cp .env.example .env`, then edit. `scripts/run_api.sh` sources it on start; the app itself
only reads the environment.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | compose default | Postgres DSN |
| `RAYMAP_ADMIN_TOKEN` | unset | allow remote `/internal/*` (sent as `X-Raymap-Admin`) |
| `RAYMAP_HEX_RES` | `6` | default resolution when a request omits `?res` |
| `RAYMAP_MAINTAIN_INTERVAL_S` | `300` | refresh/fold/prune cadence (`0` disables) |
| `RAYMAP_RAW_RETAIN_HOURS` / `RAYMAP_CELL_RETAIN_DAYS` | `72` / `90` | retention |
| `RAYMAP_QUOTA_RECORDS_HOUR` | `600` | per-device ingest quota |
| `RAYMAP_BACKUP_DIR` / `RAYMAP_BACKUP_KEEP` | `~/raymap-backups` / `14` | backups |

## Backups

Nightly `pg_dump --clean --if-exists`, gzipped, newest 14 kept.

```bash
./scripts/backup_db.sh                   # run one now
ls -lt ~/raymap-backups | head
```

**Restore (destructive — replaces all data):**

```bash
docker compose up -d
gunzip -c ~/raymap-backups/raymap-YYYYmmdd-HHMMSS.sql.gz \
  | docker exec -i raymap_db psql -U raymap -d raymap
curl -X POST 'localhost:8000/internal/refresh?days=30'
```

The dump uses `--clean --if-exists`, so restoring over a populated database works without
wiping the volume first.

**Restore drill (safe — into a scratch database):**

_Last drill: 2026-11-03 — restored 19,563 reports / 45,860 cells / 2,935 archive rows,
identical to the live database._

```bash
docker exec raymap_db psql -U raymap -d postgres -c 'DROP DATABASE IF EXISTS raymap_restore_test'
docker exec raymap_db psql -U raymap -d postgres -c 'CREATE DATABASE raymap_restore_test'
gunzip -c "$(ls -1t ~/raymap-backups/*.sql.gz | head -1)" \
  | docker exec -i raymap_db psql -U raymap -d raymap_restore_test -q
docker exec raymap_db psql -U raymap -d raymap_restore_test -tAc \
  "select (select count(*) from reports), (select count(*) from daily_cells), (select count(*) from detection_months)"
docker exec raymap_db psql -U raymap -d postgres -c 'DROP DATABASE raymap_restore_test'
```

## Deploy to a public VPS (Ubuntu 24.04)

The launchd setup above is for a laptop. A public instance is one small VPS running
PostgreSQL 16, uvicorn and nginx — no Docker, and no PostGIS (cells are H3 strings and
centroids are computed in Python, so a stock PostgreSQL is enough).

```bash
# on the server, from a checkout of this repo
sudo ./deploy/provision.sh /path/to/raymapper
```

That installs the packages, creates the `raymap` user and database, rsyncs the app into
`/opt/raymap`, writes `.env` with a generated DB password **and admin token**, applies
`schema.sql`, enables `raymap-api.service` plus the `raymap-backup.timer`, and installs the
nginx site. It prints the two DNS records to create:

```
raymap.org      A  <server ip>
api.raymap.org  A  <server ip>
```

Then, once DNS resolves:

```bash
certbot --nginx -d raymap.org -d api.raymap.org   # adds TLS + the 80→443 redirect
curl -s https://api.raymap.org/healthz            # {"status":"ok"}
```

The map UI and the API are the same process, so both names proxy to it and the collector's
endpoint is `https://api.raymap.org/ingest`.

### Things that are different from the laptop

- **`RAYMAP_ADMIN_TOKEN` is mandatory.** Behind nginx, every request arrives from
  `127.0.0.1`, so the loopback exemption would otherwise hand `/internal/*` to the whole
  internet. `_guard()` now refuses that exemption whenever proxy headers are present, so an
  unset token fails closed instead of open.
- **`access_log off` in nginx.** A contributor's IP address is the one piece of metadata this
  project deliberately does not keep, so the edge must not record it either.
- **Logs go to the journal**: `journalctl -u raymap-api -f`.
- **Backups** land in `/var/backups/raymap` (14 kept), run by
  `systemctl list-timers raymap-backup.timer`. The script prefers the Docker container when
  it exists and falls back to `pg_dump "$DATABASE_URL"` otherwise.
- **Upgrades**: re-run `provision.sh` (rsync excludes `.env` and the history exports), then
  `systemctl restart raymap-api`.

## Moderation

```bash
# block / unblock a device
curl -X POST localhost:8000/internal/block -H 'content-type: application/json' \
     -d '{"device_hash":"<hash>","reason":"poisoning"}'
curl -X DELETE localhost:8000/internal/block/<hash>

# retract a whole day (raw reports + aggregates + that month's archive, then re-fold)
curl -X POST localhost:8000/internal/retract -H 'content-type: application/json' \
     -d '{"day":"2026-11-02","reason":"bad upload"}'

curl localhost:8000/internal/audit?limit=20
```

From another host, add `-H "X-Raymap-Admin: $RAYMAP_ADMIN_TOKEN"`.

## Device (Orbic RC400L + Rayhunter)

Rayhunter is installed on the device and its daemon listens on the device's own 127.0.0.1:8080.
Reach it from the Mac with an ADB forward (no HoRNDIS needed):

```bash
adb devices                                  # 764fa857 ... device
adb forward tcp:8080 tcp:8080
curl -s localhost:8080/api/time              # daemon is alive
```

Reporting runs as a service — `org.raymap.device` wraps `scripts/run_adb_reader.sh`, which
runs `adb_reader.py --interval 900` (detections + GPS straight from the device files) and
re-arms itself if the device is unplugged. Nothing to start by hand:

```bash
launchctl kickstart -k gui/$(id -u)/org.raymap.device   # restart the reader
tail -f ~/Library/Logs/raymap-device.log                # one line per report
./.venv/bin/python adb_reader.py --once --dry-run       # inspect without uploading
```

- The device is configured `gps_mode = 1` (fixed) at its own coordinates; `/api/gps` returns them.
- Uploads are proof-of-work signed and land on the map as `clear_single` cells until an
  analyzer event fires — green means "no detection reported", not "no catcher".
- The device's cell is published at r6 (7.4 km), like any other single-device cell.
- The reader prints a preflight line on start — `preflight: Rayhunter 0.12.0`. It is written
  against that layout (`config.toml`, `qmdl/manifest.toml`, analyzer ndjson); any other
  version prints a warning instead of failing quietly.
- Raw `qmdl` captures accumulate (~47 MB after 33 sessions; 142 MB free of 214 MB). They hold
  identifiers we deliberately never upload, so prune them when `/data` gets tight — the script
  keeps the newest sessions and never deletes the detection ndjson or the manifest:

```bash
./scripts/prune_device_qmdl.sh                  # dry run, keeps the newest 5
./scripts/prune_device_qmdl.sh --keep 3 --apply
```

## History export

```bash
./.venv/bin/python scripts/export_history.py   # ui/data/history/*.ndjson.gz + index.json
```

Run monthly, or after a retraction. Served at `/data/history/`.

## Test data

```bash
./.venv/bin/python seed.py --cluster --rows 20000 --days 3 --devices 200
```

## Keep only real device data (destructive)

After seeding, or when you want the map to show only your own sensor:

```bash
REAL=$(docker exec raymap_db psql -U raymap -d raymap -tAc \
  "SELECT device_hash FROM reports GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
docker exec raymap_db psql -U raymap -d raymap -c "BEGIN;
  DELETE FROM reports WHERE device_hash <> '$REAL';
  DELETE FROM daily_cells;          -- derived: rebuilt from reports below
  DELETE FROM detection_months;     -- derived: monotonic fold, so drop it too
COMMIT;"
curl -X POST 'localhost:8000/internal/refresh?days=30'
./.venv/bin/python scripts/export_history.py   # rewrites /data/history and drops stale files
```

`audit_log` is intentionally kept — it is the operator history, not report data.
A lone remaining device is published at r6 (~7.4 km), the finest cell the map shows.

## Reset the dev database (destructive)

```bash
docker compose down -v && docker compose up -d
./.venv/bin/python seed.py --cluster
```

## Upgrading

```bash
cd /Users/admin/raymap-map
git pull
./.venv/bin/pip install -r requirements.txt
# apply schema changes (idempotent), or a migration script if one is named in the commit
./.venv/bin/python -c "import psycopg;s=open('schema.sql').read();c=psycopg.connect('postgresql://raymap:raymap@127.0.0.1:5433/raymap');c.cursor().execute(s);c.commit()"
launchctl kickstart -k gui/$(id -u)/org.raymap.api
```

Dropping `reports.h3_r8` (schema v0.3.1) needs more than `schema.sql`, which only adds:

```bash
./.venv/bin/python scripts/migrate_drop_r8.py --dry-run   # inspect
./.venv/bin/python scripts/migrate_drop_r8.py             # apply (idempotent)
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `/healthz` returns 503 | `docker compose up -d`, then `docker logs raymap_db` |
| API not reachable, agent flapping | `tail -50 ~/Library/Logs/raymap-api.log`; port clash with `lsof -ti tcp:8000` |
| `403` on `/internal/*` | caller is not loopback and no `RAYMAP_ADMIN_TOKEN` is set |
| Search finds nothing | use `Town, ST` or a 5-digit ZIP; rebuild the index with `scripts/build_places.py --download` |
| Map shows only grey | no reports in the window — `seed.py`, or check the collector uploads |
| Ingest `428` | client is not sending `X-Raymap-Pow` (old build) |
| Ingest `429` | device over quota; wait or raise `RAYMAP_QUOTA_RECORDS_HOUR` |
| ADB reader finds no device | `adb devices`; see the collector README |

## Uninstall

```bash
./scripts/install_services.sh uninstall
docker compose down            # add -v to delete the data volume too
# backups stay in ~/raymap-backups unless you remove them
```
