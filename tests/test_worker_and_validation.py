import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.registry import HarnessRegistry
from harness.router import HarnessRouter, StepResult
from harness.types import ErrorCategory, UsageMetrics
from orchestrator.models import ExecutionMeta, HarnessConfig, Task
from orchestrator.state import SystemState
from orchestrator.storage import (
    create_project,
    create_task,
    get_default_worker_prompt,
    load_task,
    restore_project_json_files,
    save_task,
    snapshot_project_json_files,
    validate_project_json_files,
)
import orchestrator.worker as worker_module
from web.app import app


class _ScriptedRouter(HarnessRouter):
    """Test router returning scripted outcomes and optionally mutating project files."""

    def __init__(self, state_manager, outcomes):
        super().__init__(registry=HarnessRegistry(), state_manager=state_manager)
        self._outcomes = list(outcomes)
        self.calls = []

    async def run_step(self, request, profile=None, on_event=None, harness_instance=None):
        self.calls.append((request, profile))
        action = self._outcomes.pop(0)
        return action(request, profile)


class TestWorkerAndValidation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.test_state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        self._tmpdir.cleanup()

    def test_v1_v2_task_compatibility_and_validation_snapshot(self):
        # Load v1 9-field payload without harness_config or execution_meta
        v1_task = Task(id="DEMO-0", title="V1", descriptions="Compat", status="in_progress", assignee="AI")
        self.assertIsNone(v1_task.harness_config)
        self.assertIsNone(v1_task.execution_meta)

        create_project("DEMO", "Demo", "Test validation", get_default_worker_prompt())
        t1 = create_task("DEMO", "DEMO-0", "Subtask", "Do work", assignee="AI", status="in_progress")
        self.assertIsNotNone(t1)
        self.assertEqual(validate_project_json_files("DEMO"), [])

        snap = snapshot_project_json_files("DEMO")
        bad_path = os.path.join(self._tmpdir.name, "projects", "DEMO", "tasks", "DEMO-1", "task.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{ broken json")

        errors = validate_project_json_files("DEMO")
        self.assertEqual(len(errors), 1)
        self.assertIn("DEMO-1", errors[0][0])

        restore_project_json_files(snap)
        self.assertEqual(validate_project_json_files("DEMO"), [])

    async def test_execute_ai_task_happy_path_no_clobber_and_no_gemini_md(self):
        create_project("ALPHA", "Alpha", "Build feature", get_default_worker_prompt())
        sub = create_task("ALPHA", "ALPHA-0", "Step 1", "Write code", assignee="AI", status="in_progress")
        sub.harness_config = HarnessConfig(harness="agy", model="gemini-3.8-flash", session_mode="sticky")
        save_task("ALPHA", sub)

        def _mutate_task_and_succeed(req, profile):
            # Simulate AI worker updating task.json directly on disk
            task_on_disk = load_task("ALPHA", "ALPHA-1")
            task_on_disk.status = "complete"
            task_on_disk.assignee = "Admin"
            save_task("ALPHA", task_on_disk)
            return StepResult(
                success=True,
                harness="agy",
                model="gemini-3.8-flash",
                session_id="conv-sticky-1",
                response="Completed step 1",
                status="SUCCESS",
                duration_seconds=4.5,
                usage=UsageMetrics(total_tokens=1500),
                output_lines=["[result] status=SUCCESS"],
            )

        router = _ScriptedRouter(self.test_state, [_mutate_task_and_succeed])
        with patch.object(worker_module, "state", self.test_state), \
             patch.object(worker_module, "send_telegram_alert") as mock_alert:
            res = await worker_module.execute_ai_task_async("ALPHA", router=router)

        self.assertTrue(res.success)
        mock_alert.assert_not_called()

        # Verify no GEMINI.md or gemini.md polluted the project folder
        project_dir = os.path.join(self._tmpdir.name, "projects", "ALPHA")
        self.assertFalse(os.path.exists(os.path.join(project_dir, "gemini.md")))
        self.assertFalse(os.path.exists(os.path.join(project_dir, "GEMINI.md")))

        updated = load_task("ALPHA", "ALPHA-1")
        self.assertEqual(updated.status, "complete")
        self.assertEqual(updated.assignee, "Admin")
        self.assertIsInstance(updated.execution_meta, ExecutionMeta)
        self.assertEqual(updated.execution_meta.total_steps, 1)
        self.assertEqual(updated.execution_meta.total_tokens, 1500)
        self.assertEqual(updated.execution_meta.session_id, "conv-sticky-1")

    async def test_json_auto_repair_and_rollback_on_persistent_corruption(self):
        create_project("BETA", "Beta", "Test repair", get_default_worker_prompt())
        proj_json = os.path.join(self._tmpdir.name, "projects", "BETA", "project.json")

        # Case A: Repair succeeds on second router call
        def _corrupt_json(req, profile):
            with open(proj_json, "w", encoding="utf-8") as f:
                f.write("{ invalid ")
            return StepResult(success=True, harness="agy", model="default", status="SUCCESS")

        def _repair_json(req, profile):
            valid = Task(id="BETA-0", title="Beta", descriptions="Repaired", status="in_progress", assignee="AI")
            save_task("BETA", valid)
            return StepResult(success=True, harness="agy", model="default", status="SUCCESS")

        router_ok = _ScriptedRouter(self.test_state, [_corrupt_json, _repair_json])
        with patch.object(worker_module, "state", self.test_state), \
             patch.object(worker_module, "send_telegram_alert"):
            res = await worker_module.execute_ai_task_async("BETA", router=router_ok)

        self.assertTrue(res.success)
        self.assertEqual(len(router_ok.calls), 2)
        self.assertEqual(load_task("BETA", "BETA-0").descriptions, "Repaired")

        # Case B: Repair leaves JSON invalid -> snapshot restored and task marked failed
        def _leave_corrupted(req, profile):
            return StepResult(success=True, harness="agy", model="default", status="SUCCESS")

        router_fail = _ScriptedRouter(self.test_state, [_corrupt_json, _leave_corrupted])
        with patch.object(worker_module, "state", self.test_state), \
             patch.object(worker_module, "send_telegram_alert") as mock_alert:
            res_fail = await worker_module.execute_ai_task_async("BETA", router=router_fail)

        self.assertFalse(res_fail.success)
        mock_alert.assert_called_once()
        restored = load_task("BETA", "BETA-0")
        self.assertEqual(restored.status, "failed")
        self.assertEqual(restored.assignee, "Admin")
        self.assertEqual(restored.descriptions, "Repaired")

    async def test_rate_limit_fallback_and_auth_error_handling(self):
        create_project("GAMMA", "Gamma", "Test errors", get_default_worker_prompt())

        def _hit_429(req, profile):
            return StepResult(
                success=False,
                harness="agy",
                model=profile.model,
                status="ERROR",
                error_category=ErrorCategory.RATE_LIMIT,
                error_message="429 quota exceeded",
            )

        router_429 = _ScriptedRouter(self.test_state, [_hit_429])
        with patch.object(worker_module, "state", self.test_state), \
             patch.object(worker_module, "send_telegram_alert") as mock_alert:
            res_429 = await worker_module.execute_ai_task_async("GAMMA", router=router_429)

        self.assertFalse(res_429.success)
        self.assertTrue(res_429.fallback_switched)
        mock_alert.assert_called_once()
        # Task must remain in_progress / AI so next tick retries with fallback model
        t_after_429 = load_task("GAMMA", "GAMMA-0")
        self.assertEqual(t_after_429.status, "in_progress")
        self.assertEqual(t_after_429.assignee, "AI")

        # Auth error immediately fails task to Admin
        def _hit_401(req, profile):
            return StepResult(
                success=False,
                harness="agy",
                model=profile.model,
                status="ERROR",
                error_category=ErrorCategory.AUTH_ERROR,
                error_message="401 Unauthorized",
            )

        router_401 = _ScriptedRouter(self.test_state, [_hit_401])
        with patch.object(worker_module, "state", self.test_state), \
             patch.object(worker_module, "send_telegram_alert") as mock_alert_401:
            res_401 = await worker_module.execute_ai_task_async("GAMMA", router=router_401)

        self.assertFalse(res_401.success)
        mock_alert_401.assert_called_once()
        t_after_401 = load_task("GAMMA", "GAMMA-0")
        self.assertEqual(t_after_401.status, "failed")
        self.assertEqual(t_after_401.assignee, "Admin")

    def test_web_api_create_project_does_not_write_gemini_md(self):
        client = TestClient(app)
        resp = client.post("/api/projects", json={"pid": "WEBTEST", "title": "Web", "desc": "Check API"})
        self.assertEqual(resp.status_code, 200)
        proj_dir = os.path.join(self._tmpdir.name, "projects", "WEBTEST")
        self.assertTrue(os.path.isfile(os.path.join(proj_dir, "system_prompt.md")))
        self.assertFalse(os.path.exists(os.path.join(proj_dir, "GEMINI.md")))


if __name__ == "__main__":
    unittest.main()
