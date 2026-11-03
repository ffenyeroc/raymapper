# Runbook — local raymap deployment

## What runs

| Component | Managed by | Logs |
|---|---|---|
| PostGIS database | Docker Compose (`restart: unless-stopped`) | `docker logs raymap_db` |
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
- Raw `qmdl` captures accumulate on the device (~47 MB after 33 sessions of 214 MB). Delete
  old ones periodically — they contain identifiers we deliberately never upload.

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
