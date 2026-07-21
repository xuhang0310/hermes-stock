"""Resource-limit smoke script copied into a disposable workspace."""
from __future__ import annotations

import subprocess
from pathlib import Path


try:
    Path("/root-filesystem-write").write_text("blocked", encoding="utf-8")
    root_read_only = False
except OSError:
    root_read_only = True

subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"root_read_only={root_read_only}")
print("x" * 100_000)
