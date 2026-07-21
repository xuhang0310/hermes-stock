#!/usr/bin/env python3
"""Run one untrusted Python script and emit a bounded JSON result."""
from __future__ import annotations

import argparse
import json
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


def _limits(timeout_s: int, file_bytes: int) -> None:
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_s + 2, timeout_s + 3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int) -> bool:
    remaining = max(0, limit - len(buffer))
    if remaining:
        buffer.extend(chunk[:remaining])
    return len(chunk) > remaining


def run(script: Path, timeout_s: int, output_limit: int, file_bytes: int) -> dict:
    started = time.monotonic()
    env = {
        "HOME": "/tmp",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": "/tmp/matplotlib",
        "LANG": "C.UTF-8",
    }
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd="/workspace",
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=lambda: _limits(timeout_s, file_bytes),
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    timed_out = False
    deadline = started + timeout_s

    while selector.get_map():
        if time.monotonic() >= deadline and proc.poll() is None:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for key, _mask in selector.select(timeout=0.1):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            truncated = _append_bounded(buffers[key.data], chunk, output_limit) or truncated
        if proc.poll() is not None and not selector.get_map():
            break

    try:
        exit_code = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        exit_code = None
    stderr = buffers["stderr"].decode("utf-8", "replace")
    if timed_out:
        stderr += f"\n[执行超时 {timeout_s}s，已强制终止]"
        exit_code = None
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": buffers["stdout"].decode("utf-8", "replace"),
        "stderr": stderr,
        "truncated": truncated,
        "ms": int((time.monotonic() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--output-limit", type=int, default=32768)
    parser.add_argument("--file-limit", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()

    script = Path(args.script).resolve()
    workspace = Path("/workspace").resolve()
    try:
        script.relative_to(workspace)
    except ValueError:
        print(json.dumps({"error": "script path escaped workspace"}))
        return 2
    if not script.is_file() or script.suffix.lower() != ".py":
        print(json.dumps({"error": "script not found"}))
        return 2
    result = run(
        script,
        max(1, min(args.timeout, 120)),
        max(1024, min(args.output_limit, 64 * 1024)),
        max(1024 * 1024, min(args.file_limit, 64 * 1024 * 1024)),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
