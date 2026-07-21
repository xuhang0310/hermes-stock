from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.execution.backends import ExecutionResult
from backend.app.execution import broker
from backend.app.skills import agent_workspace


class _FakeBackend:
    name = "docker"

    def __init__(self) -> None:
        self.called = False

    def run_python(self, *, user_id, workspace_root, script_path, timeout_s):
        self.called = True
        self.call = {
            "user_id": user_id,
            "workspace_root": workspace_root,
            "script_path": script_path,
            "timeout_s": timeout_s,
        }
        output = workspace_root / "exports" / "report.xlsx"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"xlsx")
        return ExecutionResult(exit_code=0, stdout="ok\n", ms=25)

    def health(self):
        return {"ok": True, "isolated": True, "backend": "docker"}


class WorkspaceExecutionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = SimpleNamespace(workspace_exec_enabled=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_script(self) -> None:
        with patch.object(agent_workspace, "_WORKSPACE_ROOT", self.root):
            with agent_workspace.workspace_user_scope(7):
                result = agent_workspace.ws_write_file("scripts/build.py", "print('ok')")
        self.assertNotIn("error", result)

    def test_execution_requires_one_shot_authorization(self) -> None:
        self._write_script()
        fake = _FakeBackend()
        with (
            patch.object(agent_workspace, "_WORKSPACE_ROOT", self.root),
            patch.object(agent_workspace, "settings", self.settings),
            patch.object(agent_workspace, "get_execution_backend", return_value=fake),
            agent_workspace.workspace_user_scope(7),
        ):
            result = agent_workspace.ws_run_python("scripts/build.py")
        self.assertIn("未授权", result["error"])
        self.assertFalse(fake.called)

    def test_authorized_execution_reports_new_artifacts(self) -> None:
        self._write_script()
        fake = _FakeBackend()
        with (
            patch.object(agent_workspace, "_WORKSPACE_ROOT", self.root),
            patch.object(agent_workspace, "settings", self.settings),
            patch.object(agent_workspace, "get_execution_backend", return_value=fake),
            patch.object(agent_workspace, "_audit"),
            agent_workspace.workspace_user_scope(7),
            agent_workspace.workspace_execution_scope(True),
        ):
            result = agent_workspace.ws_run_python("scripts/build.py", timeout_s=999)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["backend"], "docker")
        self.assertEqual(result["artifacts"][0]["path"], "exports/report.xlsx")
        self.assertEqual(fake.call["user_id"], 7)
        self.assertEqual(fake.call["timeout_s"], 120)


class SandboxBrokerValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.user_root = self.base / "users" / "42"
        self.user_root.mkdir(parents=True)
        (self.user_root / "scripts").mkdir()
        (self.user_root / "scripts" / "ok.py").write_text("print('ok')", encoding="utf-8")
        self.settings = SimpleNamespace(
            workspace_dir=str(self.base),
            workspace_exec_image="sandbox:test",
            workspace_exec_pids_limit=64,
            workspace_exec_memory="512m",
            workspace_exec_cpus=1.0,
            workspace_exec_file_bytes=16 * 1024 * 1024,
            workspace_exec_tmpfs_bytes=32 * 1024 * 1024,
            workspace_exec_workspace_files=100,
            workspace_exec_workspace_bytes=64 * 1024 * 1024,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolve_target_is_scoped_to_requested_user(self) -> None:
        with patch.object(broker, "settings", self.settings):
            target = broker._resolve_target(
                broker.ExecutePythonIn(user_id=42, script_path="scripts/ok.py", timeout_s=10)
            )
        self.assertEqual(target.workspace_root, self.user_root)
        self.assertEqual(target.relative_script, "scripts/ok.py")

    def test_path_traversal_is_rejected(self) -> None:
        with patch.object(broker, "settings", self.settings):
            with self.assertRaises(broker.DockerCommandError):
                broker._resolve_target(
                    broker.ExecutePythonIn(user_id=42, script_path="../43/steal.py", timeout_s=10)
                )

    def test_container_options_are_fixed_and_hardened(self) -> None:
        with patch.object(broker, "settings", self.settings):
            target = broker._resolve_target(
                broker.ExecutePythonIn(user_id=42, script_path="scripts/ok.py", timeout_s=10)
            )
            command = broker._container_create_command(target, "fixed-name")
        joined = " ".join(command)
        self.assertIn("--read-only", command)
        self.assertIn("--network none", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges:true", command)
        self.assertIn(f"src={self.user_root}", joined)
        self.assertNotIn("/run/docker.sock", joined)
        self.assertEqual(command[-3:], ["sandbox:test", "sleep", "infinity"])

    def test_workspace_quota_is_enforced_before_execution(self) -> None:
        self.settings.workspace_exec_workspace_bytes = 1
        with patch.object(broker, "settings", self.settings):
            with self.assertRaises(broker.DockerCommandError):
                broker._check_workspace_quota(self.user_root)


if __name__ == "__main__":
    unittest.main()
