# shijingxuan.com 自动续期

主域名和 `www.shijingxuan.com` 共用一张 Let's Encrypt 证书。

2026-09-30 已在正式服务器启用：新证书到期时间为北京时间 2026-12-29 20:35:39。两个域名的公网 TLS 校验通过，完整续期演练（包含 Nginx 重载钩子）成功，定时器已设置为开机启用。首页、行业分析、TSP 和后端健康接口均返回 HTTP 200。

## 首次配置

在生产服务器以 root 执行：

```bash
bash deploy/ssl/prepare-acme.sh
```

从公网核实以下两条 HTTP 地址返回 `hermes-stock-acme-ready`，且不跳转到 HTTPS：

- `http://shijingxuan.com/.well-known/acme-challenge/hermes-preflight`
- `http://www.shijingxuan.com/.well-known/acme-challenge/hermes-preflight`

然后执行：

```bash
bash deploy/ssl/enable-auto-renew.sh
/opt/hermes-stock/certbot-venv/bin/certbot renew \
  --cert-name shijingxuan.com --dry-run --run-deploy-hooks --no-random-sleep-on-renew
```

`prepare-acme.sh` 为现有 Nginx 配置增加 HTTP-01 验证路径，其他请求继续使用原有 HTTPS 跳转。脚本只修改证书和验证相关内容，保留生产服务器上的 TSP、Jenkins 等路由。Certbot 安装于应用 release 目录之外的独立环境，正常发布不会删除它。

`enable-auto-renew.sh` 签发生产证书，将现有 Nginx 配置切换为 Certbot 管理的证书路径，并启用定时任务。两份脚本都支持重复执行；Nginx 修改前备份到 `/var/backups/hermes-stock-ssl/<UTC时间>/`，校验或重载失败会恢复修改前的配置。原始手动证书保留在 `/etc/nginx/ssl/`。

## 自动运行

- 证书：`/etc/letsencrypt/live/shijingxuan.com/`。
- 续期配置：`/etc/letsencrypt/renewal/shijingxuan.com.conf`。
- Certbot：`/opt/hermes-stock/certbot-venv/bin/certbot`。
- 定时器：`hermes-stock-cert-renew.timer`，服务器时间每天 03:00 和 15:00 起，各自随机延迟最多 45 分钟；开机补执行错过的任务。
- 服务：`hermes-stock-cert-renew.service`，只检查这张证书，是否需要续期由 Certbot 判定。
- 续期成功钩子：`/usr/local/sbin/hermes-stock-cert-deploy-hook`，执行 `nginx -t`，通过后重载 Nginx，使新证书生效。钩子保存在证书续期配置中，手动执行 `certbot renew` 也会调用它。

域名必须继续解析到本机，公网 80 端口和 HTTP-01 路径必须保持可达。证书和账户私钥只保留在服务器，不提交到仓库。

## 检查与排障

```bash
systemctl is-enabled hermes-stock-cert-renew.timer
systemctl is-active hermes-stock-cert-renew.timer
systemctl list-timers --all hermes-stock-cert-renew.timer
journalctl -u hermes-stock-cert-renew.service -n 50 --no-pager
tail -n 50 /var/log/letsencrypt/letsencrypt.log
/opt/hermes-stock/certbot-venv/bin/certbot certificates --cert-name shijingxuan.com
nginx -t
```

续期演练使用 `--dry-run --run-deploy-hooks`。它验证两个域名的挑战和成功钩子，并继续使用当前生产证书，不安装测试证书。

原有 `scripts/deploy.py` 只更新 Nginx 上游并重载，不覆盖站点配置；如果以后手动更新整个站点配置，需要保留 ACME location、跳转排除规则和 Certbot 证书路径。

参考：[Certbot 官方续期说明](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)、[Let's Encrypt HTTP-01 验证说明](https://letsencrypt.org/docs/challenge-types/#http-01-challenge)。
