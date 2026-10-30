"""Issue/list/revoke an MCP credential or verify its connection, via HTTP only."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from urllib.parse import urlparse

import httpx


def validate_url(raw: str) -> str:
    url = raw.rstrip("/")
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL cannot contain credentials, query or fragment.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Use HTTPS, or HTTP on localhost for development.")
    if not parsed.hostname:
        raise ValueError("Base URL requires a hostname.")
    return url


async def check_connection(base_url: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = os.getenv("HERMES_MCP_TOKEN") or getpass.getpass("MCP token: ")
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60, follow_redirects=False) as client:
        async with streamable_http_client(f"{base_url}/api/mcp/", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                tools = await session.list_tools()
                print(json.dumps({"server": info.serverInfo.name, "tools": [tool.name for tool in tools.tools]}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "list", "revoke", "check"])
    parser.add_argument("--base-url", default="https://shijingxuan.com")
    parser.add_argument("--username")
    parser.add_argument("--name", default="external-agent")
    parser.add_argument("--scopes", help="Comma-separated MCP scopes; omitted means read-only")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--token-id", type=int)
    args = parser.parse_args()
    base_url = validate_url(args.base_url)
    if args.action == "check":
        asyncio.run(check_connection(base_url))
        return 0
    if args.action == "revoke" and not args.token_id:
        parser.error("revoke requires --token-id")
    # Credentials stay in memory; neither the password nor web JWT is written to disk.
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False) as client:
        login = client.post("/api/auth/login", json={
            "username": args.username or input("Username: ").strip(),
            "password": getpass.getpass("Password: "), "remember_me": False,
        })
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        if args.action == "create":
            payload = {"name": args.name, "expires_in_days": args.days}
            if args.scopes is not None:
                payload["scopes"] = [scope.strip() for scope in args.scopes.split(",") if scope.strip()]
            response = client.post("/api/mcp-tokens", json=payload)
        elif args.action == "list":
            response = client.get("/api/mcp-tokens")
        else:
            response = client.delete(f"/api/mcp-tokens/{args.token_id}")
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.HTTPStatusError as exc:
        # Do not print request headers, bodies or credentials on failure.
        raise SystemExit(f"HTTP {exc.response.status_code}: check the URL, account permissions and deployment status.") from None
