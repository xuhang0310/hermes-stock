#!/usr/bin/env python3
"""Authenticated production smoke test for workspace execution."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx
from sqlalchemy import select

from backend.app.auth.auth import create_access_token
from backend.app.core.database import SessionLocal
from backend.app.core.models import User
from backend.app.core.settings import settings


def _token_and_user() -> tuple[str, User]:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.is_enabled.is_(True)).order_by(User.id).limit(1))
        if user is None:
            raise RuntimeError("no enabled user available for smoke test")
        token = create_access_token(user)
        db.expunge(user)
        return token, user


def _consume_sse(response: httpx.Response) -> dict:
    event_types: list[str] = []
    artifacts: list[dict] = []
    actions: list[dict] = []
    error = None
    session_id = None
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        kind = str(event.get("type") or "")
        event_types.append(kind)
        if kind == "session":
            session_id = event.get("session_id")
        elif kind == "artifact":
            artifacts.append(event)
        elif kind == "action":
            actions.append(event)
        elif kind == "error":
            error = event.get("content")
    return {
        "event_types": event_types,
        "artifacts": artifacts,
        "actions": actions,
        "error": error,
        "session_id": session_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    token, user = _token_and_user()
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
        status_response = client.get("/api/workspace/status")
        status_response.raise_for_status()
        status = status_response.json()

        profiles_response = client.get("/api/digital-humans")
        profiles_response.raise_for_status()
        profiles = profiles_response.json()
        if not profiles:
            raise RuntimeError("no enabled digital human available")
        profile_id = profiles[0]["id"]
        capability_response = client.get(f"/api/digital-humans/{profile_id}/capabilities")
        capability_response.raise_for_status()
        tools = {
            item["name"]
            for item in capability_response.json().get("workspace_tools") or []
        }

    summary = {
        "status": status,
        "ws_run_python_registered": "ws_run_python" in tools,
        "executed": False,
    }
    if not args.execute:
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if status.get("exec_available") and summary["ws_run_python_registered"] else 1

    payload = {
        "question": (
            "这是交付验收。请先把 Python 脚本写入 scripts/delivery-smoke.py，再在 Python 沙箱执行。"
            "脚本必须用 openpyxl 生成 exports/delivery-smoke.xlsx，工作表名为验收，"
            "A1 写入 Python沙箱，B1 写入 正常。完成后只简短确认。"
        ),
        "history": [],
        "digital_human_id": profile_id,
        "allow_code_execution": True,
    }
    started = time.monotonic()
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=180) as client:
        with client.stream("POST", "/api/agent/chat", json=payload) as response:
            response.raise_for_status()
            stream = _consume_sse(response)
        if stream["session_id"]:
            client.delete(f"/api/agent/sessions/{stream['session_id']}")

    expected = "exports/delivery-smoke.xlsx"
    artifact_paths = [str(item.get("path") or "") for item in stream["artifacts"]]
    root = Path(settings.workspace_dir) / "users" / str(user.id)
    output = (root / expected).resolve()
    exists = output.is_file()
    if exists:
        output.unlink()
    script = (root / "scripts" / "delivery-smoke.py").resolve()
    if script.is_file():
        script.unlink()

    summary.update({
        "executed": True,
        "duration_s": round(time.monotonic() - started, 2),
        "done": "done" in stream["event_types"],
        "error": stream["error"],
        "artifact_event": expected in artifact_paths,
        "artifact_existed": exists,
        "tool_actions": [item.get("tool") for item in stream["actions"]],
    })
    print(json.dumps(summary, ensure_ascii=False))
    ok = (
        status.get("exec_available")
        and summary["ws_run_python_registered"]
        and summary["done"]
        and not summary["error"]
        and summary["artifact_event"]
        and summary["artifact_existed"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
