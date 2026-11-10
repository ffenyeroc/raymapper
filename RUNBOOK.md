# Runbook — raymap deployment

Live: **https://raymap.org** (map) · **https://api.raymap.org** (API + ingest).
Everything below assumes the VPS deployment described in *Deploy*, not a workstation.

## What runs

| Component | Managed by | Where |
|---|---|---|
| PostgreSQL 16 | `postgresql.service` (host packages) | `127.0.0.1:5432`, database `raymap` |
| API + map UI | `raymap-api.service` (uvicorn, `127.0.0.1:8000`, `--proxy-headers`, `--no-access-log`) | `/opt/raymap` |
| TLS + reverse proxy | `nginx.service` (certbot-managed vhost) | `:80` → 301, `:443` |
| Nightly backup | `raymap-backup.timer` (03:30 UTC, `Persistent=true`) | `/var/backups/raymap` |

Configuration lives in `/opt/raymap/.env` (mode `600`, owned by `raymap`). Logs go to the
journal — the app writes no access log, at the edge or in the process.

## Everyday commands

```bash
systemctl status raymap-api --no-pager      # is it running
systemctl restart raymap-api                # restart after a code change
journalctl -u raymap-api -f                 # follow the log
journalctl -u raymap-api -n 50 --no-pager   # recent log

curl -s https://api.raymap.org/healthz      # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://raymap.org/     # 200

systemctl list-timers raymap-backup.timer   # when the next dump runs
sudo certbot renew --dry-run                # TLS renewal check
nginx -t && systemctl reload nginx          # after an nginx change
```

## Configuration

Edit `/opt/raymap/.env`, then `systemctl restart raymap-api`. `provision.sh` writes this file
on first install and never overwrites it.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | set by `provision.sh` | Postgres DSN (generated password) |
| `RAYMAP_ADMIN_TOKEN` | set by `provision.sh` | **mandatory here** — `/internal/*` behind nginx |
| `RAYMAP_HEX_RES` | `6` | default resolution when a request omits `?res` |
| `RAYMAP_MAINTAIN_INTERVAL_S` | `300` | refresh/fold/prune cadence (`0` disables) |
| `RAYMAP_RAW_RETAIN_HOURS` / `RAYMAP_CELL_RETAIN_DAYS` | `72` / `90` | retention |
| `RAYMAP_QUOTA_RECORDS_HOUR` | `600` | per-device ingest quota |
| `RAYMAP_BACKUP_DIR` / `RAYMAP_BACKUP_KEEP` | `/var/backups/raymap` / `14` | backups |

The DB password is also kept in `/root/raymap-db-password.txt` (`600`) for restores.

## Deploy (Ubuntu 24.04)

One small VPS runs PostgreSQL 16, uvicorn and nginx. No Docker, and no PostGIS — cells are
H3 strings and centroids are computed in Python, so a stock PostgreSQL is enough.

```bash
# on the server, from a checkout of this repo
sudo ./deploy/provision.sh /path/to/raymapper
```

That installs the packages, creates the `raymap` user and database, rsyncs the app into
`/opt/raymap`, writes `.env` with a generated DB password **and admin token**, applies
`schema.sql`, enables `raymap-api.service` and `raymap-backup.timer`, and installs the nginx
site. It prints the two DNS records to create:

```
raymap.org      A  <server ip>
api.raymap.org  A  <server ip>
```

Then, once DNS resolves (and ports 80/443 are open in the provider firewall):

```bash
sudo certbot --nginx -d raymap.org -d api.raymap.org   # adds TLS + the 80→443 redirect
curl -s https://api.raymap.org/healthz
```

The map UI and the API are the same process, so both names proxy to it and the collector's
endpoint is `https://api.raymap.org/ingest`.

### Standing guarantees of this setup

- **`RAYMAP_ADMIN_TOKEN` is mandatory.** Behind nginx every request arrives from
  `127.0.0.1`, so a loopback exemption would hand `/internal/*` to the internet. `_guard()`
  refuses the exemption whenever proxy headers are present: an unset token fails closed.
- **`access_log off` in nginx and `--no-access-log` in uvicorn.** A contributor's IP address
  is the one piece of metadata this project deliberately does not keep.
- **Per-IP limit on `/ingest`** (`conf.d/raymap-limits.conf`, 30 r/m, burst 20). Proof of work
  alone is cheap enough to flood.
- **Re-running `provision.sh` never overwrites** `nginx/sites-available/raymap` (certbot owns
  the TLS block) or `.env`.

## Upgrading

```bash
# on the server
git -C /opt/src/raymapper pull            # or re-rsync the checkout
sudo ./deploy/provision.sh /opt/src/raymapper
sudo systemctl restart raymap-api
```

`provision.sh` re-runs `pip install`, re-applies `schema.sql` (idempotent) and reloads nginx;
it leaves `.env` and the certbot vhost alone. If a commit names a migration, run it after:

```bash
sudo -u raymap psql "$(sudo grep ^DATABASE_URL /opt/raymap/.env | cut -d= -f2-)" -f /opt/raymap/scripts/<migration>.sql
```

## Backups

Nightly `pg_dump --clean --if-exists`, gzipped, mode `600`, newest 14 kept.

```bash
sudo systemctl start raymap-backup.service    # run one now
sudo ls -lt /var/backups/raymap | head
```

**Restore (destructive — replaces all data):**

```bash
DB=$(sudo grep ^DATABASE_URL /opt/raymap/.env | cut -d= -f2-)
gunzip -c /var/backups/raymap/raymap-YYYYmmdd-HHMMSS.sql.gz | psql "$DB"
sudo systemctl restart raymap-api
curl -s -X POST -H "X-Raymap-Admin: $(sudo grep ^RAYMAP_ADMIN_TOKEN /opt/raymap/.env | cut -d= -f2)" \
  https://api.raymap.org/internal/refresh
```

The dump uses `--clean --if-exists`, so restoring over a populated database works without
dropping it first.

**Restore drill (safe — into a scratch database):**

```bash
DB=$(sudo grep ^DATABASE_URL /opt/raymap/.env | cut -d= -f2-)
TEST_DB="${DB%/*}/raymap_restore_test"        # swap only the database name
sudo -u postgres psql -qc 'DROP DATABASE IF EXISTS raymap_restore_test'
sudo -u postgres psql -qc 'CREATE DATABASE raymap_restore_test OWNER raymap'
gunzip -c "$(sudo ls -1t /var/backups/raymap/*.sql.gz | head -1)" | psql "$TEST_DB" -q
psql "$TEST_DB" -tAc \
  'select (select count(*) from reports), (select count(*) from daily_cells), (select count(*) from detection_months)'
sudo -u postgres psql -qc 'DROP DATABASE raymap_restore_test'
```

**Backups share the disk with the database.** A disk failure loses both, so keep a snapshot
schedule on the instance as well (14-day retention costs well under $1/month).

## Moderation

```bash
TOKEN=$(sudo grep ^RAYMAP_ADMIN_TOKEN /opt/raymap/.env | cut -d= -f2)
API=https://api.raymap.org

# block / unblock a device
curl -X POST $API/internal/block -H "X-Raymap-Admin: $TOKEN" -H 'content-type: application/json' \
     -d '{"device_hash":"<hash>","reason":"poisoning"}'
curl -X DELETE $API/internal/block/<hash> -H "X-Raymap-Admin: $TOKEN"

# retract a whole day (raw reports + aggregates + that month's archive, then re-fold)
curl -X POST $API/internal/retract -H "X-Raymap-Admin: $TOKEN" -H 'content-type: application/json' \
     -d '{"day":"YYYY-MM-DD","reason":"bad upload"}'

curl $API/internal/audit?limit=20 -H "X-Raymap-Admin: $TOKEN"
```

Without the token these return `403` — including from the server itself, because the proxy
headers make every caller look like loopback. Every mutation is written to `audit_log`.

## Collector (the station side)

The shipped client is the binary from [GitHub Releases](https://github.com/ffenyeroc/raymapper/releases).
It reads a hotspot running Rayhunter over USB and posts one record every 15 minutes.

```bash
./raymapper-collector --version                 # check the build
./raymapper-collector --once --dry-run          # inspect a record, upload nothing
./raymapper-collector --interval 900            # normal running
```

- Needs USB debugging enabled on the hotspot, and the “Allow USB debugging” prompt accepted
  once. `adb` ships inside the archive.
- Uploads are proof-of-work signed and land on the map as `clear_single` cells until an
  analyzer event fires — green means “no detection reported”, not “no catcher”.
- The reader prints a preflight line on start (`preflight: Rayhunter 0.12.0`). It is written
  against that layout (`config.toml`, `qmdl/manifest.toml`, analyzer ndjson); any other
  version prints a warning instead of failing quietly.
- Raw `qmdl` captures accumulate on the device (~47 MB after 33 sessions; ~142 MB free of
  214 MB). They hold identifiers this project deliberately never uploads, so prune them when
  `/data` gets tight — the script keeps the newest sessions and never deletes the detection
  ndjson or the manifest:

```bash
./scripts/prune_device_qmdl.sh                  # dry run, keeps the newest 5
./scripts/prune_device_qmdl.sh --keep 3 --apply
```

If the hotspot keeps its daemon on a USB-tethered interface instead of ADB, the HTTP client
in `collector/` is the alternative path (`collector/README.md`).

## History export

Run monthly, or after a retraction, from `/opt/raymap` so the output lands where the API
serves it (`/opt/raymap/ui/data/history/`, which upgrades preserve):

```bash
cd /opt/raymap && sudo -u raymap ./.venv/bin/python scripts/export_history.py
```

## Releases (collector builds)

Tagging builds the collector for every supported platform and attaches the archives to a
GitHub Release:

```bash
git tag v1.0.2 && git push origin v1.0.2     # .github/workflows/release.yml
```

Four legs: `linux-x86_64`, `win64`, `macos-arm64` (`macos-15`), `macos-x86_64`
(`macos-15-intel` — `macos-14` is x64 *and* deprecated, and `macos-13` is retired, so the
label matters: getting it wrong ships two Intel builds and no Apple Silicon one). Each leg
builds `adb_reader.py` with PyInstaller `--onedir`, copies `adb` from Android platform-tools
next to the binary, runs the frozen binary’s `--version` as a smoke test, and uploads
`<archive>` + `<archive>.sha256`.

Two build details are load-bearing — the release fails or the download breaks if they are
removed:

- **macOS legs re-sign the bundle.** PyInstaller collects `Python.framework` without its
  `_CodeSignature/CodeResources`, so the archive fails `codesign --verify --strict` and
  Gatekeeper reports *“Python.framework is damaged”* on first launch. The workflow signs every
  framework and the binary ad-hoc (`codesign --force --deep --sign -`) and verifies each one.
  That satisfies integrity checking but is **not notarisation**: browser downloads still hit
  the “unidentified developer” prompt. `curl`/`gh` downloads are not quarantined.
- **The Linux leg needs glibc ≥ 2.35** because it builds on Ubuntu 22.04 (→ Ubuntu 22.04+,
  Debian 12+). Bumping the runner raises that floor for every user.

Users can verify an archive before running it:

```bash
shasum -a 256 -c raymapper-collector-1.0.2-linux-x86_64.tar.gz.sha256
```

Downloads are hosted on **GitHub Releases** because release-asset egress is free there —
never through the VPS, whose free egress is 1 GB/month (~25 downloads).

After a release, update the manifest the Downloads page renders, then commit:

```bash
gh release view v1.0.2 --json assets --jq '.assets[] | {name, size, url}'   # gather
# edit ui/data/downloads.json: version, artifacts[] with file/size/sha256/url
```

The page validates the manifest at runtime and shows “no installers published yet” when
`artifacts` is empty, so it is safe to ship ahead of the first release.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/healthz` 503 | `systemctl status postgresql`, `journalctl -u raymap-api -n 50` |
| 502 from nginx | `systemctl status raymap-api`; is uvicorn on `127.0.0.1:8000`? |
| 404 on the apex but 200 on `api.` | nginx `server_name` or DNS record missing for the host |
| TLS error / expired cert | `sudo certbot certificates`; `sudo certbot renew` |
| `403` on `/internal/*` | no `X-Raymap-Admin` header — the loopback exemption does not apply behind nginx |
| Ingest `428` | client is not sending `X-Raymap-Pow` (old build) |
| Ingest `429` | per-device quota, or the nginx per-IP limit; wait, then investigate |
| Ingest `413` | body over 1 MiB gzipped (`RAYMAP_MAX_BODY_BYTES`) |
| Map shows only grey | no reports in the window — check the collector log and `/geojson?days=7` |
| Search finds nothing | use `Town, ST` or a 5-digit ZIP; rebuild with `scripts/build_places.py --download` |
| Disk full | prune `/var/backups/raymap`; check `journalctl --disk-usage` |

## Development

This is a contributing workflow, **not a deployment**. It runs the same code against a local
PostgreSQL in Docker so you can iterate without touching the live instance.

```bash
git clone https://github.com/ffenyeroc/raymapper && cd raymapper
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

docker compose up -d                          # PostgreSQL 16 on 127.0.0.1:5433
./.venv/bin/python seed.py --cluster          # schema + clustered sample reports
./.venv/bin/python -m uvicorn app_api:app --port 8000 --app-dir .

# http://localhost:8000
```

Test data only — reset with `docker compose down -v && docker compose up -d`, then
`seed.py` again. To keep only a single real sensor’s rows:

```bash
REAL=$(docker exec raymap_db psql -U raymap -d raymap -tAc \
  "SELECT device_hash FROM reports GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
docker exec raymap_db psql -U raymap -d raymap -c "BEGIN;
  DELETE FROM reports WHERE device_hash <> '$REAL';
  DELETE FROM daily_cells;          -- derived: rebuilt from reports below
  DELETE FROM detection_months;     -- derived: monotonic fold, so drop it too
COMMIT;"
```

`audit_log` is intentionally kept — it is the operator history, not report data.
