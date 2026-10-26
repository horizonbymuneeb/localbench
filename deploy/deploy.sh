#!/usr/bin/env bash
# Idempotent deploy/refresh for LocalBench on this box (run as root: sudo bash deploy/deploy.sh)
set -euo pipefail
DOMAIN=bench.example.com
APP=/opt/localbench
ORIGIN=127.0.0.1:21700

echo "== 1/6 layout"
install -d -o ubuntu -g ubuntu "$APP/data"
chmod 700 "$APP/data" || true
[[ -f "$APP/data/presets.json" ]] && chmod 600 "$APP/data/presets.json" || true
[[ -x "$APP/.venv/bin/python" ]] || { echo "venv missing at $APP/.venv"; exit 1; }

echo "== 2/6 python deps"
sudo -u ubuntu "$APP/.venv/bin/python" -m pip -q install -r "$APP/backend/requirements.txt"

echo "== 3/6 nginx vhost"
install -m 644 "$APP/deploy/localbench.nginx.conf" /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t

echo "== 4/6 TLS"
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
  install -d /var/www/certbot
  # temporary HTTP-only vhost so ACME can answer
  cat > /etc/nginx/sites-available/$DOMAIN.acme <<EOF
server { listen 80; listen [::]:80; server_name $DOMAIN;
  location /.well-known/acme-challenge/ { root /var/www/certbot; }
  location / { return 301 https://\$host\$request_uri; } }
EOF
  ln -sf /etc/nginx/sites-available/$DOMAIN.acme /etc/nginx/sites-enabled/$DOMAIN.acme
  systemctl reload nginx
  certbot certonly --webroot -w /var/www/certbot -d $DOMAIN --non-interactive --agree-tos --keep-until-expiring
  rm -f /etc/nginx/sites-enabled/$DOMAIN.acme /etc/nginx/sites-available/$DOMAIN.acme
fi

echo "== 5/6 systemd"
install -m 644 "$APP/deploy/localbench.service" /etc/systemd/system/localbench.service
systemctl daemon-reload
systemctl enable localbench.service >/dev/null
systemctl restart nginx
systemctl restart localbench.service

echo "== 6/6 health"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$ORIGIN/healthz" || true)
  [[ "$code" == "200" ]] && { echo "origin OK ($code)"; break; }
  sleep 1
done
curl -s -m 15 "https://$DOMAIN/healthz" && echo
echo "deployed: https://$DOMAIN"
