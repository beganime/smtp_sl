#!/usr/bin/env bash
set -Eeuo pipefail

APP_USER="smtp_sl"
APP_ROOT="/opt/smtp_sl"
APP_DIR="$APP_ROOT/app"
DATA_DIR="$APP_ROOT/data"
BACKUP_DIR="$APP_ROOT/backups"
ARCHIVE="/tmp/smtp_sl-release.tar.gz"
ENV_SOURCE="/tmp/smtp_sl.env"
ENV_FILE="/etc/smtp_sl.env"

[[ "$(id -u)" -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Missing $ARCHIVE" >&2; exit 1; }
[[ -f "$ENV_SOURCE" || -f "$ENV_FILE" ]] || { echo "Missing production environment" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip nginx sqlite3 rsync curl

if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" "$APP_ROOT" "$APP_DIR" "$DATA_DIR" "$DATA_DIR/media" "$BACKUP_DIR"

if [[ -f "$DATA_DIR/db.sqlite3" ]]; then
    sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$BACKUP_DIR/db-$(date +%Y%m%d-%H%M%S).sqlite3'"
fi

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
tar -xzf "$ARCHIVE" -C "$staging"
rsync -a --delete \
    --exclude db.sqlite3 --exclude media --exclude staticfiles --exclude venv \
    "$staging/" "$APP_DIR/"

if [[ ! -f "$DATA_DIR/db.sqlite3" ]]; then
    install -o "$APP_USER" -g "$APP_USER" -m 640 "$staging/db.sqlite3" "$DATA_DIR/db.sqlite3"
fi
if [[ -d "$staging/media" ]]; then
    rsync -a "$staging/media/" "$DATA_DIR/media/"
fi
ln -sfn "$DATA_DIR/media" "$APP_DIR/media"

if [[ -f "$ENV_SOURCE" ]]; then
    install -o root -g "$APP_USER" -m 640 "$ENV_SOURCE" "$ENV_FILE"
fi

python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --upgrade pip wheel
"$APP_ROOT/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR" "$BACKUP_DIR"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
runuser -u "$APP_USER" -- "$APP_ROOT/venv/bin/python" "$APP_DIR/manage.py" migrate --noinput
runuser -u "$APP_USER" -- "$APP_ROOT/venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput
runuser -u "$APP_USER" -- "$APP_ROOT/venv/bin/python" "$APP_DIR/manage.py" check --deploy

cat > /etc/systemd/system/smtp-sl.service <<EOF
[Unit]
Description=SMTP_SL Django application
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/gunicorn smtp_sl.wsgi:application --bind 127.0.0.1:8010 --workers 2 --timeout 120 --access-logfile - --error-logfile -
Restart=always
RestartSec=3
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/smtp-sl-worker.service <<EOF
[Unit]
Description=SMTP_SL background task worker
After=network-online.target smtp-sl.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/python manage.py process_tasks
Restart=always
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/smtp-sl-internal <<EOF
server {
    listen 0.0.0.0:8011;
    server_name tmmail.ru;
    client_max_body_size 55m;
    allow 127.0.0.1;
    allow 172.16.0.0/12;
    allow 192.168.0.0/16;
    deny all;

    location /static/ {
        alias $APP_DIR/staticfiles/;
        expires 7d;
        access_log off;
    }
    location ^~ /media/mail/inbound/ {
        return 404;
    }
    location /media/ {
        alias $DATA_DIR/media/;
        expires 7d;
        access_log off;
    }
    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/smtp-sl-internal /etc/nginx/sites-enabled/smtp-sl-internal
nginx -t
systemctl daemon-reload
systemctl enable smtp-sl smtp-sl-worker nginx
systemctl restart smtp-sl smtp-sl-worker nginx

sleep 2
systemctl is-active --quiet smtp-sl
systemctl is-active --quiet smtp-sl-worker
systemctl is-active --quiet nginx
curl --fail --silent --show-error -H 'Host: tmmail.ru' "http://127.0.0.1:8011/login/" >/dev/null
echo "SMTP_SL internal application is healthy"
