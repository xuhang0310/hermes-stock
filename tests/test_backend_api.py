from __future__ import annotations

import os
import select
import socketserver
import threading
import unittest
from pathlib import Path

import paramiko
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError


RABBIT_ENV = Path(r"C:\Users\Administrator\Documents\hermes-rabbit\backend\.env")
if RABBIT_ENV.exists():
    load_dotenv(RABBIT_ENV)
BACKEND_ENV = Path(__file__).resolve().parents[1] / "backend" / ".env"
if BACKEND_ENV.exists():
    load_dotenv(BACKEND_ENV, override=True)


class _TunnelHandler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    remote_host: str
    remote_port: int

    def handle(self) -> None:
        channel = self.ssh_transport.open_channel(
            "direct-tcpip",
            (self.remote_host, self.remote_port),
            self.request.getpeername(),
        )
        if channel is None:
            return
        try:
            while True:
                readers, _, _ = select.select([self.request, channel], [], [], 1)
                if self.request in readers:
                    data = self.request.recv(65535)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readers:
                    data = channel.recv(65535)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()


class _ThreadingForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MysqlSshTunnel:
    def __init__(self) -> None:
        self.client: paramiko.SSHClient | None = None
        self.server: _ThreadingForwardServer | None = None
        self.thread: threading.Thread | None = None
        self.local_port: int | None = None

    def start(self, ssh_host: str, ssh_user: str, ssh_password: str, mysql_port: int) -> int:
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=ssh_host,
            username=ssh_user,
            password=ssh_password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )

        class Handler(_TunnelHandler):
            ssh_transport = self.client.get_transport()
            remote_host = "127.0.0.1"
            remote_port = mysql_port

        self.server = _ThreadingForwardServer(("127.0.0.1", 0), Handler)
        self.local_port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.local_port

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.client:
            self.client.close()


TUNNEL = MysqlSshTunnel()

mysql_host = os.getenv("STOCK_DB_HOST") or os.getenv("RABBIT_DB_HOST", "127.0.0.1")
mysql_port = int(os.getenv("STOCK_DB_PORT") or os.getenv("RABBIT_DB_PORT", "3306"))
ssh_password = os.getenv("STOCK_SSH_PASSWORD")

if ssh_password and not os.getenv("STOCK_DB_HOST"):
    local_port = TUNNEL.start(
        ssh_host=mysql_host,
        ssh_user=os.getenv("STOCK_SSH_USER", "root"),
        ssh_password=ssh_password,
        mysql_port=mysql_port,
    )
    os.environ["STOCK_DB_HOST"] = "127.0.0.1"
    os.environ["STOCK_DB_PORT"] = str(local_port)
else:
    os.environ.setdefault("STOCK_DB_HOST", mysql_host)
    os.environ.setdefault("STOCK_DB_PORT", str(mysql_port))

os.environ.setdefault("STOCK_DB_USER", os.getenv("RABBIT_DB_USER", "root"))
os.environ.setdefault("STOCK_DB_PASSWORD", os.getenv("RABBIT_DB_PASSWORD", ""))
os.environ["STOCK_DB_NAME"] = "hermes-stock"

from backend.app.core.database import initialize_default_database, session_scope  # noqa: E402
from backend.app.auth.auth import hash_password  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.core.models import User  # noqa: E402
from backend.app.seed.seed import seed_demo  # noqa: E402


class BackendApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            initialize_default_database()
            with session_scope() as db:
                seed_demo(db)
                user = db.scalar(select(User).where(User.username == "codex_test_admin").limit(1))
                if user is None:
                    user = User(
                        username="codex_test_admin",
                        display_name="Codex Test Admin",
                        role="super_admin",
                        permission_scope="all",
                        password_hash=hash_password("codex-test-password"),
                        is_enabled=True,
                    )
                    db.add(user)
                else:
                    user.password_hash = hash_password("codex-test-password")
                    user.role = "super_admin"
                    user.permission_scope = "all"
                    user.is_enabled = True
        except OperationalError as exc:
            raise unittest.SkipTest(f"MySQL is not reachable for tests: {exc}") from exc
        cls.client = TestClient(create_app(init_db=False))
        login = cls.client.post(
            "/api/auth/login",
            json={"username": "codex_test_admin", "password": "codex-test-password"},
        )
        if login.status_code != 200:
            raise RuntimeError(login.text)
        cls.auth_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    @classmethod
    def tearDownClass(cls) -> None:
        TUNNEL.close()

    def test_dashboard_reads_seeded_mysql_database(self) -> None:
        response = self.client.get("/api/dashboard", headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["title"].startswith("SixRabbits"))
        self.assertGreaterEqual(payload["counts"]["open_positions"], 1)
        self.assertGreaterEqual(len(payload["recent_events"]), 1)
        self.assertIn("portfolio", payload)
        self.assertIn("market", payload)
        self.assertIn("runtime", payload)
        self.assertIn("approvals", payload)
        self.assertIn("risk_events", payload)
        self.assertIn("next_job", payload)
        self.assertEqual(len(payload["market"]["items"]), 3)

    def test_agent_approval_center_lists_current_user_requests(self) -> None:
        response = self.client.get("/api/agent/approvals", headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_event_detail_includes_evidence_and_reports(self) -> None:
        response = self.client.get("/api/events/demo-l0-stop-loss-000001", headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["level"], "L0")
        self.assertGreaterEqual(len(payload["evidence"]), 1)
        self.assertGreaterEqual(len(payload["reports"]), 1)

    def test_add_watchlist_rejects_unknown_symbol(self) -> None:
        response = self.client.post(
            "/api/watchlist",
            json={"symbol": "999999.SZ", "reason": "not in instruments"},
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, 400)

    def test_health_reports_mysql(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["journal_mode"], "mysql")
        self.assertIn("hermes-stock", payload["database"])

    def test_login_required_for_dashboard(self) -> None:
        response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 401)

    def test_digital_humans_and_model_configs_are_seeded(self) -> None:
        humans = self.client.get("/api/digital-humans", headers=self.auth_headers)
        platforms = self.client.get("/api/model-platforms", headers=self.auth_headers)
        models = self.client.get("/api/model-configs", headers=self.auth_headers)

        self.assertEqual(humans.status_code, 200)
        self.assertEqual(platforms.status_code, 200)
        self.assertEqual(models.status_code, 200)
        self.assertGreaterEqual(len(humans.json()), 1)
        platform_payload = platforms.json()
        model_payload = models.json()
        self.assertEqual([item["name"] for item in platform_payload], ["百炼"])
        self.assertTrue(platform_payload[0]["has_api_key"])
        active_models = [item for item in model_payload if item["is_active"]]
        self.assertEqual(len(active_models), 1)
        self.assertEqual(active_models[0]["model_name"], "qwen3.7-plus")
        self.assertNotIn("聚合AI", {item["platform_name"] for item in model_payload})
        self.assertFalse({"qwen-plus", "qwen-max", "deepseek-v3"} & {item["model_name"] for item in model_payload})

    def test_agent_sessions_start_empty_or_list(self) -> None:
        response = self.client.get("/api/agent/sessions", headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)


if __name__ == "__main__":
    unittest.main()
