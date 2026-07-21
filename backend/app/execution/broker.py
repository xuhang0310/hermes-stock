"""Narrow Docker broker for workspace Python execution.

The public API process cannot access Docker. This service is the only component
with Docker socket access, and callers cannot choose images, mounts, commands,
container names, environment variables, or security options.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_LABEL = "com.sixrabbits.workspace-sandbox"
_SPEC_VERSION = "1"
_SCRIPT_MAX_BYTES = 2 * 1024 * 1024
_OUTPUT_MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class BrokerSettings:
    """Non-secret settings supplied by the sandboxd systemd unit only."""

    workspace_dir: str = os.getenv("SANDBOX_WORKSPACE_DIR", "/opt/hermes-stock/workspace")
    workspace_exec_enabled: bool = os.getenv("SANDBOX_ENABLED", "false").lower() == "true"
    workspace_exec_image: str = os.getenv("SANDBOX_IMAGE", "hermes-stock-python-sandbox:latest")
    workspace_exec_memory: str = os.getenv("SANDBOX_MEMORY", "1g")
    workspace_exec_cpus: float = float(os.getenv("SANDBOX_CPUS", "2"))
    workspace_exec_pids_limit: int = int(os.getenv("SANDBOX_PIDS_LIMIT", "128"))
    workspace_exec_tmpfs_bytes: int = int(os.getenv("SANDBOX_TMPFS_BYTES", str(256 * 1024 * 1024)))
    workspace_exec_file_bytes: int = int(os.getenv("SANDBOX_FILE_BYTES", str(64 * 1024 * 1024)))
    workspace_exec_workspace_bytes: int = int(os.getenv("SANDBOX_WORKSPACE_BYTES", str(256 * 1024 * 1024)))
    workspace_exec_workspace_files: int = int(os.getenv("SANDBOX_WORKSPACE_FILES", "2000"))
    workspace_exec_max_concurrency: int = int(os.getenv("SANDBOX_MAX_CONCURRENCY", "4"))


settings = BrokerSettings()


class ExecutePythonIn(BaseModel):
    user_id: int = Field(ge=1, le=2**63 - 1)
    script_path: str = Field(min_length=1, max_length=300)
    timeout_s: int = Field(default=30, ge=1, le=120)


@dataclass(frozen=True)
class SandboxTarget:
    user_id: int
    workspace_root: Path
    script_path: Path
    relative_script: str
    host_uid: int
    host_gid: int


class DockerCommandError(RuntimeError):
    pass


def _workspace_base() -> Path:
    if not settings.workspace_dir:
        raise DockerCommandError("sandboxd 要求显式配置 STOCK_WORKSPACE_DIR")
    base = Path(settings.workspace_dir).expanduser()
    if not base.is_absolute():
        raise DockerCommandError("STOCK_WORKSPACE_DIR 必须是绝对路径")
    return base.resolve()


def _resolve_target(payload: ExecutePythonIn) -> SandboxTarget:
    raw = payload.script_path.strip().replace("\\", "/")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or "\x00" in raw:
        raise DockerCommandError("脚本路径越权")
    if relative.suffix.lower() != ".py":
        raise DockerCommandError("只允许执行 .py 文件")

    base = _workspace_base()
    expected_root = base / "users" / str(payload.user_id)
    try:
        root = expected_root.resolve(strict=True)
        root.relative_to(base)
    except (FileNotFoundError, ValueError) as exc:
        raise DockerCommandError("用户工作区不存在或路径异常") from exc
    if root != expected_root.absolute():
        raise DockerCommandError("用户工作区不能是符号链接")

    try:
        script = (root / relative.as_posix()).resolve(strict=True)
        resolved_relative = script.relative_to(root).as_posix()
    except (FileNotFoundError, ValueError) as exc:
        raise DockerCommandError("脚本不存在或路径越权") from exc
    if not script.is_file() or script.suffix.lower() != ".py":
        raise DockerCommandError("脚本不存在或不是 .py 文件")
    if script.stat().st_size > _SCRIPT_MAX_BYTES:
        raise DockerCommandError("脚本超过 2MB 上限")
    stat = root.stat()
    return SandboxTarget(
        user_id=payload.user_id,
        workspace_root=root,
        script_path=script,
        relative_script=resolved_relative,
        host_uid=stat.st_uid,
        host_gid=stat.st_gid,
    )


def _container_name(user_id: int) -> str:
    return f"hermes-stock-sandbox-u{user_id}"


def _workspace_usage(root: Path) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total_files += 1
            total_bytes += path.stat().st_size
            if (
                total_files > settings.workspace_exec_workspace_files
                or total_bytes > settings.workspace_exec_workspace_bytes
            ):
                break
        except OSError:
            continue
    return total_bytes, total_files


def _check_workspace_quota(root: Path) -> None:
    total_bytes, total_files = _workspace_usage(root)
    if total_files > settings.workspace_exec_workspace_files:
        raise DockerCommandError(
            f"工作区文件数超过上限 {settings.workspace_exec_workspace_files}，请先清理"
        )
    if total_bytes > settings.workspace_exec_workspace_bytes:
        limit_mb = settings.workspace_exec_workspace_bytes // (1024 * 1024)
        raise DockerCommandError(f"工作区占用超过上限 {limit_mb}MB，请先清理")


def _run_docker(args: list[str], *, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["/usr/bin/docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerCommandError(f"Docker 命令执行失败: {str(exc)[:200]}") from exc
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "Docker 命令失败").strip()
        raise DockerCommandError(message[:500])
    return proc


def _image_id() -> str:
    proc = _run_docker(["image", "inspect", "--format", "{{.Id}}", settings.workspace_exec_image])
    image_id = proc.stdout.strip()
    if not image_id:
        raise DockerCommandError(f"沙箱镜像不存在: {settings.workspace_exec_image}")
    return image_id


def _inspect_container(name: str) -> dict | None:
    proc = _run_docker(["container", "inspect", name], check=False)
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout)
        return rows[0] if isinstance(rows, list) and rows else None
    except (json.JSONDecodeError, TypeError):
        return None


def _container_matches(container: dict, target: SandboxTarget, image_id: str) -> bool:
    labels = (container.get("Config") or {}).get("Labels") or {}
    mounts = container.get("Mounts") or []
    workspace_mount = next((m for m in mounts if m.get("Destination") == "/workspace"), None)
    return bool(
        labels.get(_LABEL) == "true"
        and labels.get(f"{_LABEL}.spec") == _SPEC_VERSION
        and container.get("Image") == image_id
        and workspace_mount
        and Path(str(workspace_mount.get("Source") or "")).resolve() == target.workspace_root
        and bool(workspace_mount.get("RW"))
    )


def _container_create_command(target: SandboxTarget, name: str) -> list[str]:
    return [
        "container", "create",
        "--name", name,
        "--label", f"{_LABEL}=true",
        "--label", f"{_LABEL}.user-id={target.user_id}",
        "--label", f"{_LABEL}.spec={_SPEC_VERSION}",
        "--read-only",
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(settings.workspace_exec_pids_limit),
        "--memory", settings.workspace_exec_memory,
        "--memory-swap", settings.workspace_exec_memory,
        "--cpus", str(settings.workspace_exec_cpus),
        "--ulimit", "nofile=256:256",
        "--ulimit", f"fsize={settings.workspace_exec_file_bytes}:{settings.workspace_exec_file_bytes}",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={settings.workspace_exec_tmpfs_bytes},mode=1777",
        "--mount", f"type=bind,src={target.workspace_root},dst=/workspace",
        "--workdir", "/workspace",
        "--env", "HOME=/tmp",
        "--env", "MPLCONFIGDIR=/tmp/matplotlib",
        "--env", "PYTHONIOENCODING=utf-8",
        "--env", "LANG=C.UTF-8",
        settings.workspace_exec_image,
        "sleep", "infinity",
    ]


def _ensure_container(target: SandboxTarget) -> str:
    name = _container_name(target.user_id)
    image_id = _image_id()
    container = _inspect_container(name)
    if container is not None and not _container_matches(container, target, image_id):
        _run_docker(["container", "rm", "--force", name], timeout=20)
        container = None
    if container is None:
        _run_docker(_container_create_command(target, name), timeout=30)
    return name


def _execute_in_container(name: str, target: SandboxTarget, timeout_s: int) -> dict:
    command = [
        "/usr/bin/docker", "container", "exec",
        "--user", f"{target.host_uid}:{target.host_gid}",
        "--workdir", "/workspace",
        name,
        "/usr/local/bin/python", "/opt/hermes-sandbox/runner.py",
        "--timeout", str(timeout_s),
        "--output-limit", str(_OUTPUT_MAX_BYTES),
        "--file-limit", str(settings.workspace_exec_file_bytes),
        "--script", f"/workspace/{target.relative_script}",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s + 8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _run_docker(["container", "kill", name], timeout=10, check=False)
        return {
            "exit_code": None,
            "timed_out": True,
            "stdout": "",
            "stderr": f"[执行超时 {timeout_s}s，容器已强制停止]",
            "truncated": False,
        }
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "沙箱执行器异常退出").strip()
        raise DockerCommandError(message[:500])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DockerCommandError("沙箱执行器返回了无效结果") from exc
    if not isinstance(payload, dict):
        raise DockerCommandError("沙箱执行器返回了无效结果")
    return payload


_locks_guard = threading.Lock()
_user_locks: dict[int, threading.Lock] = {}
_capacity = threading.BoundedSemaphore(value=max(1, settings.workspace_exec_max_concurrency))


def _user_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


app = FastAPI(title="Hermes Workspace Sandbox Broker", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict:
    try:
        image_id = _image_id()
        return {"ok": True, "backend": "docker", "isolated": True, "image_id": image_id[:24]}
    except DockerCommandError as exc:
        return {"ok": False, "backend": "docker", "isolated": True, "error": str(exc)}


@app.post("/v1/execute/python")
def execute_python(payload: ExecutePythonIn) -> dict:
    if not settings.workspace_exec_enabled:
        raise HTTPException(status_code=503, detail="工作区执行能力未开启")
    try:
        target = _resolve_target(payload)
        _check_workspace_quota(target.workspace_root)
    except DockerCommandError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not _capacity.acquire(timeout=5):
        raise HTTPException(status_code=429, detail="沙箱并发已满，请稍后重试")
    started = time.monotonic()
    try:
        with _user_lock(payload.user_id):
            name = _ensure_container(target)
            # A broker crash may leave the reusable container running. Stop it
            # first so every request begins with a clean process namespace.
            _run_docker(["container", "stop", "--time", "1", name], timeout=10, check=False)
            _run_docker(["container", "start", name], timeout=20)
            try:
                result = _execute_in_container(name, target, payload.timeout_s)
            finally:
                # Every run starts from a clean process state. The container object
                # persists for fast reuse, while detached children cannot survive.
                _run_docker(["container", "stop", "--time", "1", name], timeout=10, check=False)
            _check_workspace_quota(target.workspace_root)
    except DockerCommandError as exc:
        raise HTTPException(status_code=500, detail=f"沙箱执行失败: {str(exc)[:300]}") from exc
    finally:
        _capacity.release()
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result
