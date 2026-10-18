#!/usr/bin/env bash
# Prepare HTTP-01 without interrupting HTTPS or replacing application routes.
set -euo pipefail
test "$(id -u)" = 0
install -d -m 755 /var/www/hermes-acme/.well-known/acme-challenge
install -d -m 700 /var/backups/hermes-stock-ssl

python3 - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import os
import shutil
import subprocess

config = Path('/etc/nginx/conf.d/hermes-stock.conf')
original = config.read_text()
old_map = '''map "$scheme:$host" $hermes_force_https {
    default 0;
    "http:shijingxuan.com" 1;
    "http:www.shijingxuan.com" 1;
}'''
new_map = '''map "$scheme:$host:$uri" $hermes_force_https {
    default 0;
    ~^http:(shijingxuan\\.com|www\\.shijingxuan\\.com):/\\.well-known/acme-challenge/ 0;
    ~^http:(shijingxuan\\.com|www\\.shijingxuan\\.com): 1;
}'''
challenge = '''    # HTTP-01 must be reachable before the HTTPS redirect.
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/hermes-acme;
        default_type text/plain;
        try_files $uri =404;
    }

'''
updated = original
if old_map in updated:
    updated = updated.replace(old_map, new_map, 1)
elif new_map not in updated:
    raise SystemExit('Unexpected HTTPS redirect map; no nginx changes made.')
if challenge not in updated:
    if '/.well-known/acme-challenge/' in original:
        raise SystemExit('Existing ACME location requires review; no nginx changes made.')
    marker = '    ssl_certificate '
    if updated.count(marker) != 1:
        raise SystemExit('Unexpected certificate directives; no nginx changes made.')
    updated = updated.replace(marker, challenge + marker, 1)

if updated != original:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/var/backups/hermes-stock-ssl') / stamp
    backup.mkdir(mode=0o700)
    shutil.copy2(config, backup / 'hermes-stock.conf')
    staged = config.with_name(config.name + '.acme-pending')
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
    print('Nginx ACME route enabled. Backup:', backup)
else:
    subprocess.run(['nginx', '-t'], check=True)
    print('Nginx ACME route is already configured.')
PY

printf '%s' 'hermes-stock-acme-ready' > /var/www/hermes-acme/.well-known/acme-challenge/hermes-preflight
chmod 644 /var/www/hermes-acme/.well-known/acme-challenge/hermes-preflight
for domain in shijingxuan.com www.shijingxuan.com; do
    result=''
    for attempt in {1..10}; do
        result=$(curl --noproxy '*' -fsS --max-time 15 --resolve "$domain:80:127.0.0.1" "http://$domain/.well-known/acme-challenge/hermes-preflight") || true
        if test "$result" = 'hermes-stock-acme-ready'; then break; fi
        sleep 1
    done
    test "$result" = 'hermes-stock-acme-ready'
    printf 'HTTP-01 local check passed: %s\n' "$domain"
done

if ! test -x /opt/hermes-stock/certbot-venv/bin/certbot; then
    if ! test -x /opt/hermes-stock/certbot-venv/bin/python; then
        uv venv /opt/hermes-stock/certbot-venv --python /usr/bin/python3
    fi
    UV_HTTP_TIMEOUT=30 uv pip install --python /opt/hermes-stock/certbot-venv/bin/python \
        --index-url https://mirrors.aliyun.com/pypi/simple 'certbot==5.8.0'
fi
/opt/hermes-stock/certbot-venv/bin/certbot --version
