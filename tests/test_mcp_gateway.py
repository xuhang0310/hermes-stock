"""Exercise the actual MCP protocol and auth wrapper without external services."""
from __future__ import annotations

import unittest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from backend.app.mcp_gateway import server
from backend.app.mcp_gateway.auth import McpPrincipal, SCOPE_PERMISSIONS


class MCPGatewayTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected DB")))
        self.audit = self.enterContext(patch.object(server, "_write_log"))
        all_scopes = frozenset(SCOPE_PERMISSIONS)
        self.principals = {
            "owner-a": McpPrincipal(11, 1, "researcher", frozenset({"all"}), all_scopes),
            "owner-b": McpPrincipal(22, 2, "researcher", frozenset({"all"}), all_scopes),
            "readonly": McpPrincipal(11, 3, "viewer", frozenset({"data:read"}), frozenset({"market:read", "portfolio:read"})),
        }

        def authenticate(token):
            if token not in self.principals:
                raise HTTPException(401, "invalid token")
            return self.principals[token]

        self.enterContext(patch.object(server, "_authenticate", side_effect=authenticate))
        mcp = server.create_mcp_server(transport_security=TransportSecuritySettings(
            allowed_hosts=["testserver"], allowed_origins=["https://allowed.example"],
        ))
        mounted = server.MCPAuthMiddleware(mcp.streamable_http_app())

        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        app = Starlette(routes=[Mount("/api/mcp", mounted)], lifespan=lifespan)
        self.client = self.enterContext(TestClient(app))

    def rpc(self, method, params=None, token="owner-a", **kwargs):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        headers.update(kwargs.pop("headers", {}))
        return self.client.post("/api/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        }, **kwargs)

    def call(self, name, arguments=None, **kwargs):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_initialize_and_discover_tools(self):
        response = self.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1"},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "SixRabbits Stock")
        self.assertNotIn("mcp-session-id", response.headers)
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual(len(tools), 13)
        for tool in tools:
            self.assertNotIn("ctx", tool["inputSchema"]["properties"])
            self.assertNotIn("user_id", tool["inputSchema"]["properties"])
            self.assertEqual(tool["annotations"]["readOnlyHint"], tool["name"] not in {"add_position", "create_schedule"})

    def test_authentication_required_for_handshake_discovery_and_calls(self):
        for method in ("initialize", "tools/list", "tools/call"):
            for token in (None, "bad", "fake.web.jwt"):
                with self.subTest(method=method, token=token):
                    response = self.rpc(method, token=token)
                    self.assertEqual(response.status_code, 401)
                    self.assertIn("Bearer", response.headers["www-authenticate"])
        self.assertTrue(all(not call.args[0]["success"] for call in self.audit.call_args_list))

    def test_scope_denies_mutations_and_logs_semantic_failure(self):
        with patch.object(server.portfolio_tools, "add_position") as add:
            result = self.call("add_position", {
                "symbol": "600036.SH", "account_id": 1, "quantity": 100,
                "execution_price": 38, "idempotency_key": "buy-1",
            }, token="readonly")
        self.assertTrue(result["isError"])
        add.assert_not_called()
        row = self.audit.call_args.args[0]
        self.assertEqual((row["user_id"], row["status_code"], row["success"]), (11, 403, False))
        self.assertEqual(len(self.audit.call_args_list), 1)

    def test_concurrent_calls_keep_request_identity(self):
        with patch.object(server.portfolio_tools, "get_positions", side_effect=lambda user_id, account_id: {"owner": user_id}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(self.call, "get_positions", {}, token=token)
                        for token in ("owner-a", "owner-b")]
                results = [job.result() for job in jobs]
                self.assertTrue(all(not item.get("isError") for item in results), results)
                owners = [item["structuredContent"]["owner"] for item in results]
        self.assertEqual(owners, [11, 22])

    def test_unknown_owner_argument_cannot_override_authenticated_user(self):
        with patch.object(server.portfolio_tools, "get_positions", return_value={"owner": 11}) as positions:
            result = self.call("get_positions", {"user_id": 999})
        # SDK rejects extra arguments; if a future SDK ignores them, identity must
        # still come exclusively from the authenticated request.
        if result.get("isError"):
            positions.assert_not_called()
        else:
            self.assertEqual(positions.call_args.kwargs["user_id"], 11)

    def test_schedules_decode_nested_schema_and_no_privileged_parameters(self):
        args = {"name": "每日复盘", "instruction": "分析持仓", "target_agent_code": "stock-pm",
                "schedule": {"type": "weekly", "run_time": "17:30", "weekdays": [1, 5]},
                "idempotency_key": "schedule-1"}
        with patch.object(server.schedule_tools, "create_schedule", return_value={"created": True}) as create:
            result = self.call("create_schedule", args)
            self.assertFalse(result.get("isError", False), result)
            self.assertEqual(create.call_args.kwargs["user_id"], 11)
            self.assertEqual(create.call_args.kwargs["schedule"], args["schedule"])
            self.assertNotIn("allow_code_execution", create.call_args.kwargs)
            create.reset_mock()
            invalid = self.call("create_schedule", {**args, "schedule": {**args["schedule"], "allow_code_execution": True}})
            self.assertTrue(invalid["isError"])
            create.assert_not_called()
        self.assertFalse(self.audit.call_args.args[0]["success"])

    def test_invalid_quantity_is_rejected_before_business_code(self):
        with patch.object(server.portfolio_tools, "add_position") as add:
            result = self.call("add_position", {"account_id": 1, "symbol": "600036.SH", "quantity": -1,
                                                "execution_price": 1, "idempotency_key": "invalid"})
        self.assertTrue(result["isError"])
        add.assert_not_called()
        self.assertFalse(self.audit.call_args.args[0]["success"])

    def test_host_origin_and_body_limit(self):
        self.assertEqual(self.rpc("tools/list", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.rpc("tools/list", headers={"Host": "evil.example"}).status_code, 421)
        response = self.rpc("tools/list", {"padding": "x" * (128 * 1024)})
        self.assertEqual(response.status_code, 413)

    def test_serialization_failure_is_an_error_and_audited_as_failure(self):
        with patch.object(server.market_tools, "get_stock_quote", return_value={"ok": True}), \
             patch.object(server, "jsonable_encoder", side_effect=RuntimeError("private detail")):
            result = self.call("get_stock_quote", {"symbol": "600036.SH"})
        self.assertTrue(result["isError"])
        self.assertNotIn("private detail", str(result))
        self.assertEqual(self.audit.call_args.args[0]["status_code"], 500)

    def test_real_fastapi_app_mount_starts_the_session_manager(self):
        from backend.app import main

        with patch.object(main, "validate_startup"), patch.object(main.skill_registry, "discover"), \
             patch.object(main.redis_broker, "close", new_callable=AsyncMock):
            with TestClient(main.create_app(init_db=False)) as client:
                response = client.post("/api/mcp/", headers={
                    "Host": "localhost:8000", "Authorization": "Bearer owner-a",
                    "Accept": "application/json, text/event-stream",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(response.json()["result"]["tools"]), 13)
                self.assertEqual(client.get("/api/mcp-tokens").status_code, 401)

    def test_official_client_handshake_and_tool_call(self):
        async def run_client():
            mcp = server.create_mcp_server(transport_security=TransportSecuritySettings(allowed_hosts=["testserver"]))
            app = Starlette(routes=[Mount("/api/mcp", server.MCPAuthMiddleware(mcp.streamable_http_app()))])
            async with mcp.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             headers={"Authorization": "Bearer owner-a"}) as client:
                    async with streamable_http_client("http://testserver/api/mcp/", http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertEqual(len(tools.tools), 13)
                            result = await session.call_tool("get_stock_quote", {"symbol": "600036.SH"})
                            self.assertFalse(result.isError)
                            self.assertEqual(result.structuredContent["trade_date"], "20261030")

        with patch.object(server.market_tools, "get_stock_quote", return_value={"trade_date": "20261030"}):
            asyncio.run(run_client())


if __name__ == "__main__":
    unittest.main()
