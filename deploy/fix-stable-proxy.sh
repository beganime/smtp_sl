#!/usr/bin/env bash
set -Eeuo pipefail

HOST_CONF=/etc/nginx/sites-available/smtp-sl-internal
COMPOSE_FILE=/srv/akyl/docker-compose.prod.yml
PUBLIC_CONF=/srv/akyl/deploy/nginx/conf.d/default.conf

cp -a "$HOST_CONF" "$HOST_CONF.backup-before-stable-host"
cp -a "$COMPOSE_FILE" "$COMPOSE_FILE.backup-before-smtp-sl-host-gateway"
cp -a "$PUBLIC_CONF" "$PUBLIC_CONF.backup-before-smtp-sl-host-gateway"

sed -i 's/listen 172\.19\.0\.1:8011;/listen 0.0.0.0:8011;/' "$HOST_CONF"
if ! grep -q 'allow 172.16.0.0/12;' "$HOST_CONF"; then
    sed -i '/server_name tmmail\.ru;/a\    allow 127.0.0.1;\n    allow 172.16.0.0/12;\n    allow 192.168.0.0/16;\n    deny all;' "$HOST_CONF"
fi

if ! grep -q 'host.docker.internal:host-gateway' "$COMPOSE_FILE"; then
    sed -i '/^    image: nginx:1\.27-alpine$/a\    extra_hosts:\n      - "host.docker.internal:host-gateway"' "$COMPOSE_FILE"
fi
sed -i 's#proxy_pass http://172\.19\.0\.1:8011;#proxy_pass http://host.docker.internal:8011;#' "$PUBLIC_CONF"

nginx -t
systemctl restart nginx
systemctl is-active --quiet nginx
curl --fail --silent --show-error -H 'Host: tmmail.ru' http://127.0.0.1:8011/login/ >/dev/null

cd /srv/akyl
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate nginx
docker exec akyl-nginx-1 nginx -t
docker exec akyl-nginx-1 wget -qO- --header='Host: tmmail.ru' http://host.docker.internal:8011/login/ >/dev/null
docker exec akyl-nginx-1 nginx -s reload

curl --fail --silent --show-error https://tmmail.ru/login/ >/dev/null
echo 'Stable SMTP_SL proxy is healthy'
