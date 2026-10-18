#!/usr/bin/env bash
# Run after prepare-acme.sh and public HTTP-01 preflight verification.
set -euo pipefail
test "$(id -u)" = 0
CERTBOT=/opt/hermes-stock/certbot-venv/bin/certbot
test -x "$CERTBOT"

install -d -m 755 /usr/local/sbin
cat > /usr/local/sbin/hermes-stock-cert-deploy-hook <<'HOOK'
#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
/usr/bin/logger -t hermes-stock-cert 'Certificate deployed; nginx configuration validated and reloaded.'
HOOK
chmod 755 /usr/local/sbin/hermes-stock-cert-deploy-hook

"$CERTBOT" certonly --webroot --webroot-path /var/www/hermes-acme \
    --cert-name shijingxuan.com -d shijingxuan.com -d www.shijingxuan.com \
    --non-interactive --agree-tos --register-unsafely-without-email \
    --keep-until-expiring \
    --deploy-hook /usr/local/sbin/hermes-stock-cert-deploy-hook

python3 - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import os
import shutil
import subprocess

config = Path('/etc/nginx/conf.d/hermes-stock.conf')
original = config.read_text()
updated = original
for old, new in [
    ('ssl_certificate /etc/nginx/ssl/shijingxuan.com.pem;',
     'ssl_certificate /etc/letsencrypt/live/shijingxuan.com/fullchain.pem;'),
    ('ssl_certificate_key /etc/nginx/ssl/shijingxuan.com.key;',
     'ssl_certificate_key /etc/letsencrypt/live/shijingxuan.com/privkey.pem;'),
]:
    if old in updated:
        updated = updated.replace(old, new, 1)
    elif new not in updated:
        raise SystemExit('Unexpected certificate path; no nginx changes made.')

if updated != original:
    backup = Path('/var/backups/hermes-stock-ssl') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(mode=0o700)
    shutil.copy2(config, backup / 'hermes-stock.conf')
    staged = config.with_name(config.name + '.ssl-pending')
    staged.write_text(updated)
    shutil.copystat(config, staged)
    os.replace(staged, config)
    try:
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    except Exception:
        shutil.copy2(backup / 'hermes-stock.conf', config)
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        raise
    print('Nginx now uses the managed certificate. Backup:', backup)
else:
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    print('Managed certificate path already configured.')
PY

cat > /etc/systemd/system/hermes-stock-cert-renew.service <<'SERVICE'
[Unit]
Description=Renew shijingxuan.com TLS certificate
Wants=network-online.target
After=network-online.target nginx.service
ConditionPathExists=/etc/letsencrypt/renewal/shijingxuan.com.conf

[Service]
Type=oneshot
ExecStart=/opt/hermes-stock/certbot-venv/bin/certbot renew --quiet --cert-name shijingxuan.com --no-random-sleep-on-renew
TimeoutStartSec=30min
SERVICE

cat > /etc/systemd/system/hermes-stock-cert-renew.timer <<'TIMER'
[Unit]
Description=Check shijingxuan.com certificate renewal twice daily

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=45min
Persistent=true
Unit=hermes-stock-cert-renew.service

[Install]
WantedBy=timers.target
TIMER

systemd-analyze verify /etc/systemd/system/hermes-stock-cert-renew.service /etc/systemd/system/hermes-stock-cert-renew.timer
systemctl daemon-reload
systemctl enable --now hermes-stock-cert-renew.timer
systemctl start hermes-stock-cert-renew.service
systemctl is-enabled hermes-stock-cert-renew.timer
systemctl is-active hermes-stock-cert-renew.timer
systemctl list-timers --all hermes-stock-cert-renew.timer --no-pager
"$CERTBOT" certificates --cert-name shijingxuan.com
