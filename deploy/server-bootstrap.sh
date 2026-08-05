#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN="${DOMAIN:-tmmail.ru}"
APP_USER="${APP_USER:-smtp_sl}"
APP_ROOT="/opt/smtp_sl"
APP_DIR="$APP_ROOT/app"
DATA_DIR="$APP_ROOT/data"
BACKUP_DIR="$APP_ROOT/backups"
ARCHIVE="${ARCHIVE:-/tmp/smtp_sl-release.tar.gz}"
ENV_SOURCE="${ENV_SOURCE:-/tmp/smtp_sl.env}"
ENV_FILE="/etc/smtp_sl.env"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите скрипт через sudo." >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE" ]]; then
  echo "Не найден архив приложения: $ARCHIVE" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx sqlite3 rsync

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" "$APP_ROOT" "$APP_DIR" "$DATA_DIR" "$BACKUP_DIR"

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
  if [[ ! -f "$staging/db.sqlite3" ]]; then
    echo "В первом релизе отсутствует db.sqlite3." >&2
    exit 1
  fi
  install -o "$APP_USER" -g "$APP_USER" -m 640 "$staging/db.sqlite3" "$DATA_DIR/db.sqlite3"
fi
if [[ -d "$staging/media" && ! -d "$DATA_DIR/media" ]]; then
  cp -a "$staging/media" "$DATA_DIR/media"
fi
install -d -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/media"
ln -sfn "$DATA_DIR/media" "$APP_DIR/media"

if [[ -f "$ENV_SOURCE" ]]; then
  install -o root -g "$APP_USER" -m 640 "$ENV_SOURCE" "$ENV_FILE"
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "Для первого деплоя нужен закрытый файл $ENV_SOURCE." >&2
  exit 1
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

cat > /etc/systemd/system/smtp-sl.service <<EOF
[Unit]
Description=SMTP_SL Django application
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/gunicorn smtp_sl.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 120 --access-logfile - --error-logfile -
Restart=always
RestartSec=3
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/smtp-sl-worker.service <<EOF
[Unit]
Description=SMTP_SL background task worker
After=network.target smtp-sl.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_ROOT/venv/bin/python manage.py process_tasks
Restart=always
RestartSec=5
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/smtp-sl <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 55m;

    location /static/ { alias $APP_DIR/staticfiles/; expires 7d; }
    location /media/ { alias $DATA_DIR/media/; expires 7d; }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/smtp-sl /etc/nginx/sites-enabled/smtp-sl
nginx -t
systemctl daemon-reload
systemctl enable --now smtp-sl smtp-sl-worker nginx
systemctl restart smtp-sl smtp-sl-worker nginx

if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null || true
  ufw allow 'Nginx Full' >/dev/null || true
fi

certbot --nginx --non-interactive --agree-tos --redirect \
  --email "${LETSENCRYPT_EMAIL:?Укажите LETSENCRYPT_EMAIL}" \
  -d "$DOMAIN"

systemctl is-active --quiet smtp-sl
curl --fail --silent --show-error "https://$DOMAIN/login/" >/dev/null
echo "SMTP_SL успешно развёрнут: https://$DOMAIN"
