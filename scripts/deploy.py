#!/usr/bin/env python3
"""SixRabbits Stock 一键部署脚本 —— release + 软链，原子切换、失败自动回滚。

服务器约定（与现网一致）:
  /opt/hermes-stock/
    current -> releases/<时间戳>   (软链，切它即回滚)
    releases/<时间戳>/             (backend/ + frontend/dist/ + pyproject.toml)
    venv/                          (Python 依赖已装)
  systemd 服务 hermes-stock: uvicorn backend.app.main:app --host 127.0.0.1 --port 18080（单 worker）
  nginx: listen 18088, root=current/frontend/dist, /api/ -> 127.0.0.1:18080
  .env 是每个 release 各自的实体文件（不在 shared），部署时从上一版复制、绝不覆盖生产配置。

用法:
  1) 前端本地构建（服务器无 node）: 加 --build，或先手动 `cd frontend && npm run build`
  2) 密码用环境变量（推荐，避免进 shell 历史）:
       set STOCK_DEPLOY_PASSWORD=***      (Windows)
       export STOCK_DEPLOY_PASSWORD=***   (bash)
       python scripts/deploy.py --host your-server-ip --user root --build
     或显式:  python scripts/deploy.py --host your-server-ip --user root --password *** --build

设计要点（都是上次部署踩过的坑）:
  - 验证用 Python 逐步轮询，不用 shell `set -e` + curl（启动窗口 curl 失败会误退出、跳过回滚）
  - 复制上一版 .env，不覆盖；缺 STOCK_WEBHOOK_ENC_KEY 才补一个（已存在绝不重生成，否则飞书密文失效）
  - 切软链前记录旧 release，验证失败自动切回并重启
"""
from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("需要 paramiko：python -m pip install paramiko", file=sys.stderr)
    sys.exit(2)

# Windows 中文控制台默认 GBK，输出 emoji/部分 Unicode 会崩；强制 UTF-8 容错。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_NAMES = {".env", "agent_audit.log"}
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".venv", ".git", "chroma_data"}


def log(msg: str) -> None:
    print(msg, flush=True)


def collect_files() -> list[str]:
    """返回相对项目根、需要上传的文件列表（backend 代码 + 前端 dist + 元数据）。"""
    files: list[str] = []
    for dp, dns, fns in os.walk(ROOT / "backend"):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            if fn in EXCLUDE_NAMES or fn.endswith(".pyc"):
                continue
            if fn.startswith("backend-") and fn.endswith(".log"):
                continue
            files.append(str(Path(dp, fn).relative_to(ROOT)))
    dist = ROOT / "frontend" / "dist"
    if not dist.exists():
        raise SystemExit("frontend/dist 不存在。先 `cd frontend && npm run build`，或用 --build。")
    for dp, _dns, fns in os.walk(dist):
        for fn in fns:
            files.append(str(Path(dp, fn).relative_to(ROOT)))
    for fn in ("pyproject.toml", "README.md"):
        if (ROOT / fn).exists():
            files.append(fn)
    for dp, _dns, fns in os.walk(ROOT / "deploy"):
        for fn in fns:
            files.append(str(Path(dp, fn).relative_to(ROOT)))
    return files


def build_frontend() -> None:
    log("本地构建前端 (npm run build)...")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    r = subprocess.run([npm, "run", "build"], cwd=str(ROOT / "frontend"))
    if r.returncode != 0:
        raise SystemExit("前端构建失败，终止部署。")


def api_upstream_command(ports: list[int]) -> str:
    lines = "\n".join(f"server 127.0.0.1:{port} max_fails=2 fail_timeout=10s;" for port in ports)
    return (
        "cat > /etc/nginx/hermes-stock-upstream-servers.conf <<'EOF'\n"
        f"{lines}\nEOF\n"
        "nginx -t && systemctl reload nginx"
    )


class Deployer:
    def __init__(self, args: argparse.Namespace, password: str | None):
        self.a = args
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {"port": args.port, "username": args.user, "timeout": 20}
        if getattr(args, "key", None):
            connect_kwargs["key_filename"] = args.key
        else:
            connect_kwargs["password"] = password
        self.client.connect(args.host, **connect_kwargs)
        self.sftp = self.client.open_sftp()
        self._made: set[str] = set()

    def run(self, cmd: str, timeout: int = 200) -> tuple[int, str, str]:
        _in, out, err = self.client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace").rstrip(), err.read().decode("utf-8", "replace").rstrip()

    def http_code(self, url: str) -> str:
        _, o, _ = self.run(f"curl -s -m 6 -o /dev/null -w '%{{http_code}}' {url} || echo 000")
        return o.strip()

    def _ha_enabled(self) -> bool:
        rc, _, _ = self.run("test -f /etc/systemd/system/hermes-stock-api-a.service && test -f /etc/systemd/system/hermes-stock-api-b.service")
        return rc == 0

    def _wait_health(self, port: int) -> bool:
        health = f"http://127.0.0.1:{port}/api/health"
        for _ in range(30):
            if self.http_code(health) == "200":
                return True
            time.sleep(1)
        return False

    def _set_api_upstream(self, ports: list[int]) -> None:
        command = api_upstream_command(ports)
        rc, out, err = self.run(command)
        if rc != 0:
            raise RuntimeError(f"更新 Nginx 上游失败: {out} {err}")

    def _rolling_restart(self) -> bool:
        replicas = (("hermes-stock-api-a", 18080, [18082]), ("hermes-stock-api-b", 18082, [18080]))
        for service, port, remaining in replicas:
            self._set_api_upstream(remaining)
            rc, out, err = self.run(f"systemctl restart {service}")
            if rc != 0 or not self._wait_health(port):
                log(f"副本 {service} 重启或健康检查失败: {out} {err}")
                self._set_api_upstream([18080, 18082])
                return False
            self._set_api_upstream([18080, 18082])
        return True

    def _restart_background(self) -> bool:
        rc, out, err = self.run("systemctl restart hermes-stock-background")
        if rc != 0 or not self._wait_health(18084):
            log(f"后台实例重启或健康检查失败: {out} {err}")
            return False
        return True

    def _sandbox_enabled(self) -> bool:
        rc, _, _ = self.run("test -f /etc/systemd/system/hermes-stock-sandboxd.service")
        return rc == 0

    def _restart_sandbox(self) -> bool:
        rc, out, err = self.run("systemctl restart hermes-stock-sandboxd")
        if rc != 0:
            log(f"沙箱 broker 重启失败: {out} {err}")
            return False
        for _ in range(20):
            rc, out, _ = self.run(
                "curl --silent --max-time 2 --unix-socket /run/hermes-stock-sandbox/sandbox.sock "
                "http://sandboxd/health"
            )
            if rc == 0 and '"ok":true' in out.replace(" ", "").lower():
                return True
            time.sleep(1)
        log("沙箱 broker 健康检查超时")
        return False

    def _mkdir(self, rdir: str) -> None:
        if rdir in self._made or rdir in ("", "/"):
            return
        self._mkdir(posixpath.dirname(rdir))
        try:
            self.sftp.stat(rdir)
        except IOError:
            self.sftp.mkdir(rdir)
        self._made.add(rdir)

    def upload(self, files: list[str], remote_rel: str) -> None:
        self._mkdir(remote_rel)
        for rel in files:
            rpath = posixpath.join(remote_rel, rel.replace("\\", "/"))
            self._mkdir(posixpath.dirname(rpath))
            self.sftp.put(str(ROOT / rel), rpath)

    def deploy(self) -> int:
        a = self.a
        release = datetime.now().strftime("%Y%m%d%H%M%S")
        new = f"{a.remote_base}/releases/{release}"
        cur = f"{a.remote_base}/current"

        rc, old, _ = self.run(f"readlink -f {cur}")
        old = old.strip()
        log(f"上一版 current -> {old or '(无，首次部署)'}")

        files = collect_files()
        log(f"上传 {len(files)} 个文件 -> releases/{release} ...")
        self.upload(files, new)

        enc_key = secrets.token_urlsafe(36)
        server = f"""
set -u
NEW={new}; CUR={cur}; OLD='{old}'
if [ -n "$OLD" ] && [ -f "$OLD/backend/.env" ]; then
  cp "$OLD/backend/.env" "$NEW/backend/.env"
else
  [ -f "$NEW/backend/.env.example" ] && cp "$NEW/backend/.env.example" "$NEW/backend/.env" || touch "$NEW/backend/.env"
fi
grep -q '^STOCK_WEBHOOK_ENC_KEY=' "$NEW/backend/.env" || echo 'STOCK_WEBHOOK_ENC_KEY={enc_key}' >> "$NEW/backend/.env"
grep -q '^STOCK_SCHEDULER_ENABLED=' "$NEW/backend/.env" || echo 'STOCK_SCHEDULER_ENABLED=true' >> "$NEW/backend/.env"
grep -q '^STOCK_WORKSPACE_EXEC_BACKEND=' "$NEW/backend/.env" || echo 'STOCK_WORKSPACE_EXEC_BACKEND=docker' >> "$NEW/backend/.env"
grep -q '^STOCK_WORKSPACE_EXEC_SOCKET=' "$NEW/backend/.env" || echo 'STOCK_WORKSPACE_EXEC_SOCKET=/run/hermes-stock-sandbox/sandbox.sock' >> "$NEW/backend/.env"
grep -q '^STOCK_WORKSPACE_EXEC_IMAGE=' "$NEW/backend/.env" || echo 'STOCK_WORKSPACE_EXEC_IMAGE=hermes-stock-python-sandbox:latest' >> "$NEW/backend/.env"
chmod 600 "$NEW/backend/.env"
chown -R {a.owner} "$NEW"
/opt/hermes-stock/venv/bin/pip install --disable-pip-version-check -q -r "$NEW/backend/requirements.txt" || exit 31
docker build --quiet -f "$NEW/deploy/sandbox/Dockerfile" -t hermes-stock-python-sandbox:latest "$NEW" || exit 32
id -u hermes-sandbox >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin hermes-sandbox
usermod -aG docker hermes-sandbox
install -m 0644 "$NEW/deploy/systemd/hermes-stock-sandboxd.service" /etc/systemd/system/hermes-stock-sandboxd.service
systemctl daemon-reload
systemctl enable hermes-stock-sandboxd >/dev/null 2>&1
ln -sfn "$NEW" "$CUR"
echo SWITCHED
"""
        rc, out, err = self.run(server, timeout=1800)
        if "SWITCHED" not in out:
            log(f"切换/重启失败: {out} {err}")
            self._rollback(cur, old)
            return 1
        log(f"已切换 current -> {new}，重启服务，验证中...")

        if self._ha_enabled() and not a.defer_restart:
            if not self._restart_sandbox() or not self._restart_background() or not self._rolling_restart():
                self._rollback(cur, old)
                return 1
        elif not a.defer_restart:
            rc, out, err = self.run(f"systemctl restart {a.service}")
            if rc != 0:
                log(f"服务重启失败: {out} {err}")
                self._rollback(cur, old)
                return 1

        health = f"http://127.0.0.1:{a.backend_port}/api/health"
        route = f"http://127.0.0.1:{a.backend_port}/api/notify-channels"
        code = "000"
        for _ in range(12):
            code = self.http_code(health)
            if code == "200":
                break
            time.sleep(2)
        rcode = self.http_code(route)
        log(f"验证: health={code}  notify-channels(未登录)={rcode}（期望 401=路由存在）")

        if code != "200" or rcode != "401":
            log("!! 验证不通过，回滚")
            self._rollback(cur, old)
            return 1

        self.run("nginx -t >/dev/null 2>&1 && systemctl reload nginx")
        fcode = self.http_code(f"http://127.0.0.1:{a.nginx_port}/")
        log(f"nginx 前端 {a.nginx_port}: {fcode}")
        self._cleanup(a.remote_base, keep=a.keep)
        log(f"\n[OK] 部署成功  release={release}  current -> {self.run(f'readlink -f {cur}')[1]}")
        return 0

    def _rollback(self, cur: str, old: str) -> None:
        if not old:
            log("无旧版本可回滚（首次部署失败），请手动检查服务。")
            return
        self.run(f"ln -sfn {old} {cur}")
        if self._sandbox_enabled() and self.run(f"test -f {old}/backend/app/execution/broker.py")[0] == 0:
            self._restart_sandbox()
        elif self._sandbox_enabled():
            self.run("systemctl stop hermes-stock-sandboxd")
        if self._ha_enabled():
            self._restart_background()
            self._rolling_restart()
        else:
            self.run(f"systemctl restart {self.a.service} && sleep 3")
        code = self.http_code(f"http://127.0.0.1:{self.a.backend_port}/api/health")
        log(f"已回滚到 {old}，health={code}")

    def _cleanup(self, base: str, keep: int) -> None:
        rc, out, _ = self.run(
            f"ls -1dt {base}/releases/*/ 2>/dev/null | tail -n +{keep + 1} | xargs -r rm -rf; echo cleaned"
        )
        if "cleaned" in out:
            log(f"已清理旧 release（保留最近 {keep} 个）")

    def close(self) -> None:
        self.sftp.close()
        self.client.close()


def main() -> int:
    p = argparse.ArgumentParser(description="SixRabbits Stock 部署脚本（release + 软链）")
    p.add_argument("--host", required=True)
    p.add_argument("--user", default="root")
    p.add_argument("--password", default=os.getenv("STOCK_DEPLOY_PASSWORD"))
    p.add_argument("--key", default=os.getenv("STOCK_DEPLOY_KEY"), help="SSH 私钥路径（与密码二选一）")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--remote-base", default="/opt/hermes-stock")
    p.add_argument("--service", default="hermes-stock")
    p.add_argument("--owner", default="hermes:hermes")
    p.add_argument("--backend-port", type=int, default=18080)
    p.add_argument("--nginx-port", type=int, default=80)
    p.add_argument("--keep", type=int, default=5, help="保留最近 N 个 release")
    p.add_argument("--build", action="store_true", help="部署前本地 npm run build")
    p.add_argument("--defer-restart", action="store_true", help="switch release without restarting a legacy single-instance service")
    args = p.parse_args()

    if args.build:
        build_frontend()

    if args.key:
        password = None
        log(f"使用 SSH 私钥: {args.key}")
    else:
        password = args.password or getpass.getpass(f"{args.user}@{args.host} 密码: ")
        if not password:
            raise SystemExit("缺少密码：用 --password 或环境变量 STOCK_DEPLOY_PASSWORD；或用 --key 指定私钥。")

    d = Deployer(args, password)
    try:
        return d.deploy()
    finally:
        d.close()


if __name__ == "__main__":
    raise SystemExit(main())
