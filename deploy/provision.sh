#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 VPS to run raymap.
#
#   sudo ./deploy/provision.sh /path/to/raymapper-checkout
#
# Installs PostgreSQL + nginx, creates the service user and database, installs
# the app into /opt/raymap, applies the schema, and starts the API and the
# nightly backup timer. DNS and TLS are deliberately left manual: they need the
# A records pointing here first.
set -euo pipefail

SRC="${1:?usage: provision.sh <path to the raymapper checkout>}"
APP=/opt/raymap
APP_USER=raymap
DB_NAME=raymap
DB_USER=raymap
PW_FILE=/root/raymap-db-password.txt

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ -f "$SRC/schema.sql" ] || { echo "not a raymapper checkout: $SRC" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip postgresql postgresql-contrib \
  nginx certbot python3-certbot-nginx rsync gzip openssl

id -u "$APP_USER" >/dev/null 2>&1 \
  || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

# --- database ---------------------------------------------------------------
systemctl enable --now postgresql
if [ ! -f "$PW_FILE" ]; then
  openssl rand -hex 24 > "$PW_FILE"
  chmod 600 "$PW_FILE"
fi
DB_PASS=$(cat "$PW_FILE")

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  sudo -u postgres psql -qc "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

# --- application ------------------------------------------------------------
mkdir -p "$APP"
# --delete keeps the server honest, .env is excluded so it survives re-provisioning.
rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude .env --exclude '*.log' --exclude state.jsonl \
  --exclude 'ui/data/history' \
  "$SRC"/ "$APP"/

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" -q install --upgrade pip
"$APP/.venv/bin/pip" -q install -r "$APP/requirements.txt"

# --- config -----------------------------------------------------------------
if [ ! -f "$APP/.env" ]; then
  cat > "$APP/.env" <<EOF
DATABASE_URL=postgresql://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME
# Required: behind nginx every caller looks like loopback, so /internal/*
# only accepts this token.
RAYMAP_ADMIN_TOKEN=$(openssl rand -hex 24)
RAYMAP_HEX_RES=6
RAYMAP_MAINTAIN_INTERVAL_S=300
RAYMAP_RAW_RETAIN_HOURS=72
RAYMAP_CELL_RETAIN_DAYS=90
RAYMAP_QUOTA_RECORDS_HOUR=600
RAYMAP_BACKUP_DIR=/var/backups/raymap
RAYMAP_BACKUP_KEEP=14
EOF
fi
chown -R "$APP_USER":"$APP_USER" "$APP"
chmod 600 "$APP/.env"
install -d -o "$APP_USER" -g "$APP_USER" /var/backups/raymap

# --- schema (idempotent) ----------------------------------------------------
DB_URL=$(grep '^DATABASE_URL=' "$APP/.env" | cut -d= -f2-)
sudo -u "$APP_USER" psql "$DB_URL" -q -v ON_ERROR_STOP=1 -f "$APP/schema.sql"

# --- services ---------------------------------------------------------------
install -m 644 "$APP/deploy/raymap-api.service" /etc/systemd/system/
install -m 644 "$APP/deploy/raymap-backup.service" /etc/systemd/system/
install -m 644 "$APP/deploy/raymap-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now raymap-api.service
systemctl enable --now raymap-backup.timer

# --- nginx ------------------------------------------------------------------
# Never overwrite an existing site: certbot rewrites this file in place to add
# the 443 server block, so re-running provision would silently drop TLS.
if [ -f /etc/nginx/sites-available/raymap ]; then
  echo "nginx site already present — leaving it alone (certbot owns the TLS block)"
else
  install -m 644 "$APP/deploy/nginx-raymap.conf" /etc/nginx/sites-available/raymap
  ln -sf /etc/nginx/sites-available/raymap /etc/nginx/sites-enabled/raymap
  rm -f /etc/nginx/sites-enabled/default
fi
# The limit zone must live in the http{} context, hence conf.d rather than the site.
install -m 644 "$APP/deploy/nginx-limits.conf" /etc/nginx/conf.d/raymap-limits.conf
nginx -t
systemctl reload nginx

# External IP, not `hostname -I` (which reports the internal 10.x address).
IP=$(curl -s -m 3 -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip' 2>/dev/null || true)
[ -n "$IP" ] || IP=$(hostname -I | awk '{print $1}')
cat <<EOF

Provisioned.

  1. DNS at the registrar: A records for
       raymap.org      -> $IP
       api.raymap.org  -> $IP
  2. TLS, once DNS resolves:
       certbot --nginx -d raymap.org -d api.raymap.org
  3. Verify:
       curl -s https://api.raymap.org/healthz
       systemctl status raymap-api
       journalctl -u raymap-api -f

Admin token: grep RAYMAP_ADMIN_TOKEN $APP/.env
DB password: $PW_FILE
Upgrades: re-run this script, then 'systemctl restart raymap-api'.
EOF
