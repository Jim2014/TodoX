"""Isolated tests for loop/budget guardrails, structured trace spans, and Web telemetry APIs."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.registry import HarnessRegistry
from harness.router import HarnessRouter
from harness.types import ErrorCategory, HarnessCapabilities, HarnessEvent, HealthStatus, UsageMetrics
from orchestrator.guardrails import (
    append_trace_span,
    check_task_guardrails,
    compute_project_telemetry,
    read_recent_traces,
)
from orchestrator.models import ExecutionMeta, HarnessConfig, Task
from orchestrator.state import SystemState
from orchestrator.storage import create_project, create_task, get_default_worker_prompt, load_task, save_task
from orchestrator.worker import execute_ai_task_async
from web.app import app


class _CountingStubHarness(BaseHarness):
    def __init__(self, fail_mode: bool = False):
        self.calls = 0
        self.fail_mode = fail_mode

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name="agy", display_name="Counting Stub")

    async def check_health(self) -> HealthStatus:
        return HealthStatus(harness="agy", available=True, message="OK")

    async def list_models(self, force_refresh: bool = False):
        return []

    async def run_stream(self, request):
        self.calls += 1
        if self.fail_mode:
            yield HarnessEvent(
                type="result",
                harness="agy",
                status="ERROR",
                error_category=ErrorCategory.PROCESS_CRASH,
                response="simulated crash",
            )
            return
        yield HarnessEvent(
            type="result",
            harness="agy",
            status="SUCCESS",
            duration_seconds=0.5,
            usage=UsageMetrics(input_tokens=100, output_tokens=50, total_tokens=150),
            response="ok",
        )

    async def stop(self) -> None:
        return None


class TestGuardrailsAndTelemetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        self._tmpdir.cleanup()

    async def test_guardrail_step_token_and_consecutive_failure_breakers(self):
        create_project("SAFE", "Guardrail Test", "Verify budget limits", get_default_worker_prompt())
        t1 = create_task("SAFE", "SAFE-0", "Bounded Task", "Do work", assignee="AI", status="in_progress")
        assert t1 is not None
        t1.harness_config = HarnessConfig(max_steps=2, max_tokens=500)
        t1.execution_meta = ExecutionMeta(total_steps=2, total_tokens=200)
        save_task("SAFE", t1)

        stub = _CountingStubHarness()
        reg = HarnessRegistry()
        reg.register("agy", lambda **kw: stub)
        router = HarnessRouter(registry=reg, state_manager=self.state)

        # 1. Step limit tripped BEFORE invoking harness
        res = await execute_ai_task_async("SAFE", router=router)
        self.assertIsNotNone(res)
        self.assertFalse(res.success)
        self.assertEqual(res.status, "GUARDRAIL_BLOCKED")
        self.assertEqual(stub.calls, 0)
        reloaded = load_task("SAFE", "SAFE-1")
        self.assertEqual(reloaded.status, "blocked")
        self.assertEqual(reloaded.assignee, "Admin")
        self.assertEqual(reloaded.comments[-1].author, "System (Guardrail)")

        # 2. Consecutive failure circuit-breaker
        t2 = create_task("SAFE", "SAFE-0", "Flaky Task", "Crash loop", assignee="AI", status="in_progress")
        assert t2 is not None
        t2.harness_config = HarnessConfig(max_steps=10, max_tokens=50000)
        t2.execution_meta = ExecutionMeta(total_steps=1, total_tokens=100, consecutive_failures=3)
        save_task("SAFE", t2)
        t1.status = "complete"
        save_task("SAFE", t1)

        self.assertIn("circuit-breaker", check_task_guardrails(load_task("SAFE", "SAFE-0"), t2) or "")
        res2 = await execute_ai_task_async("SAFE", router=router)
        self.assertEqual(res2.status, "GUARDRAIL_BLOCKED")
        self.assertEqual(stub.calls, 0)

        # 3. Normal execution increments steps/tokens, resets consecutive_failures, and appends trace span
        t2.status = "in_progress"
        t2.assignee = "AI"
        t2.execution_meta.consecutive_failures = 0
        save_task("SAFE", t2)
        res3 = await execute_ai_task_async("SAFE", router=router)
        self.assertTrue(res3.success)
        self.assertEqual(stub.calls, 1)
        t2_after = load_task("SAFE", "SAFE-2")
        self.assertEqual(t2_after.execution_meta.total_steps, 2)
        self.assertEqual(t2_after.execution_meta.total_tokens, 250)

        traces = read_recent_traces(pid="SAFE")
        self.assertEqual([tr["status"] for tr in traces], ["GUARDRAIL_BLOCKED", "GUARDRAIL_BLOCKED", "SUCCESS"])

    def test_web_telemetry_api_and_harness_budget_update(self):
        create_project("OBS", "Telemetry API", "Observe runs", get_default_worker_prompt())
        t1 = create_task("OBS", "OBS-0", "Step One", "Desc", assignee="Admin", status="complete")
        assert t1 is not None
        t1.execution_meta = ExecutionMeta(total_steps=3, total_tokens=2100, total_duration_seconds=4.5)
        save_task("OBS", t1)
        append_trace_span("OBS", "OBS-1", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=1.5, total_tokens=700)

        client = TestClient(app)
        r_proj = client.get("/api/projects/OBS")
        self.assertEqual(r_proj.status_code, 200)
        telemetry = r_proj.json().get("telemetry")
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry["total_steps"], 3)
        self.assertEqual(telemetry["total_tokens"], 2100)
        self.assertEqual(telemetry["success_rate_pct"], 100)

        r_tr = client.get("/api/projects/OBS/traces")
        self.assertEqual(r_tr.status_code, 200)
        self.assertEqual(len(r_tr.json()["traces"]), 1)

        # Save guardrail budget overrides via PUT /api/tasks
        r_put = client.put(
            "/api/tasks/OBS/OBS-1",
            json={
                "harness_config": {
                    "harness": "claude",
                    "model": "claude-sonnet-4-6",
                    "max_steps": "8",
                    "max_tokens": "40000",
                }
            },
        )
        self.assertEqual(r_put.status_code, 200)
        updated = load_task("OBS", "OBS-1")
        self.assertEqual(updated.harness_config.max_steps, 8)
        self.assertEqual(updated.harness_config.max_tokens, 40000)


if __name__ == "__main__":
    unittest.main()
