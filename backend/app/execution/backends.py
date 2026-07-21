from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from backend.app.core.settings import settings


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    ms: int = 0
    error: str | None = None

    @classmethod
    def from_dict(cls, payload: dict) -> "ExecutionResult":
        return cls(
            exit_code=payload.get("exit_code"),
            timed_out=bool(payload.get("timed_out")),
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            truncated=bool(payload.get("truncated")),
            ms=int(payload.get("ms") or 0),
            error=str(payload["error"]) if payload.get("error") else None,
        )


class ExecutionBackend(Protocol):
    name: str

    def run_python(
        self,
        *,
        user_id: int,
        workspace_root: Path,
        script_path: Path,
        timeout_s: int,
    ) -> ExecutionResult: ...

    def health(self) -> dict: ...


class LocalExecutionBackend:
    """Development-only backend. It intentionally offers no OS isolation."""

    name = "local"

    def run_python(
        self,
        *,
        user_id: int,
        workspace_root: Path,
        script_path: Path,
        timeout_s: int,
    ) -> ExecutionResult:
        del user_id
        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "LANG": "C.UTF-8",
        }
        if sys.platform == "win32" and os.environ.get("SYSTEMROOT"):
            clean_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(workspace_root),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            return ExecutionResult(
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                ms=int((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ExecutionResult(
                timed_out=True,
                stdout=stdout,
                stderr=stderr + f"\n[执行超时 {timeout_s}s，已强制终止]",
                ms=int((time.monotonic() - started) * 1000),
            )
        except OSError as exc:
            return ExecutionResult(error=f"本地执行失败: {str(exc)[:200]}")

    def health(self) -> dict:
        return {"ok": True, "backend": self.name, "isolated": False}


class DockerExecutionBackend:
    """Client for the narrow host-side sandbox broker over a Unix socket."""

    name = "docker"

    def run_python(
        self,
        *,
        user_id: int,
        workspace_root: Path,
        script_path: Path,
        timeout_s: int,
    ) -> ExecutionResult:
        try:
            relative_script = script_path.resolve().relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            return ExecutionResult(error="脚本不在当前用户工作区内")
        try:
            transport = httpx.HTTPTransport(uds=settings.workspace_exec_socket)
            with httpx.Client(
                transport=transport,
                base_url="http://sandboxd",
                timeout=timeout_s + 15,
            ) as client:
                response = client.post(
                    "/v1/execute/python",
                    json={
                        "user_id": user_id,
                        "script_path": relative_script,
                        "timeout_s": timeout_s,
                    },
                )
            payload = response.json()
            if response.status_code >= 400:
                detail = payload.get("detail") if isinstance(payload, dict) else None
                return ExecutionResult(error=str(detail or f"沙箱服务返回 HTTP {response.status_code}"))
            if not isinstance(payload, dict):
                return ExecutionResult(error="沙箱服务返回了无效响应")
            return ExecutionResult.from_dict(payload)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return ExecutionResult(error=f"沙箱服务不可用: {str(exc)[:200]}")

    def health(self) -> dict:
        try:
            transport = httpx.HTTPTransport(uds=settings.workspace_exec_socket)
            with httpx.Client(transport=transport, base_url="http://sandboxd", timeout=1.5) as client:
                response = client.get("/health")
            payload = response.json()
            return payload if isinstance(payload, dict) else {"ok": False, "backend": self.name}
        except (httpx.HTTPError, OSError, ValueError):
            return {"ok": False, "backend": self.name}


def get_execution_backend() -> ExecutionBackend:
    backend = settings.workspace_exec_backend.strip().lower()
    if backend == "local":
        return LocalExecutionBackend()
    if backend == "docker":
        return DockerExecutionBackend()
    raise RuntimeError(f"不支持的工作区执行后端: {backend}")
