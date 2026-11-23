import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.registry import HarnessRegistry, create_default_registry
from harness.router import HarnessRouter
from harness.types import (
    CanonicalTool,
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HealthStatus,
    ModelInfo,
    PermissionPreset,
    RunRequest,
    UsageMetrics,
)
from orchestrator.models import HarnessConfig, Task
from orchestrator.state import BACKOFF_SCHEDULE_SECONDS, DEFAULT_FALLBACK_CHAIN, SystemState


class _FakeHarness(BaseHarness):
    def __init__(self, events=None, available=True, name="fake", **kwargs):
        self._events = list(events or [])
        self._available = available
        self._name = name
        self.stopped = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=self._name, display_name="Fake Harness")

    async def check_health(self) -> HealthStatus:
        return HealthStatus(harness=self._name, available=self._available, message="fake")

    async def list_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        return []

    async def run_stream(self, request: RunRequest):
        for ev in self._events:
            yield ev

    async def stop(self) -> None:
        self.stopped = True


class TestHarnessRouterAndState(unittest.IsolatedAsyncioTestCase):
    async def test_registry_operations_and_health(self):
        reg = create_default_registry()
        self.assertTrue(reg.has("agy"))
        self.assertTrue(reg.has("claude"))
        self.assertEqual(reg.list_names(), ["agy", "claude"])

        with self.assertRaises(ValueError):
            reg.register("agy", _FakeHarness)

        reg.register("fake", lambda **kw: _FakeHarness(available=False, name="fake", **kw))
        reg.register("claude", lambda **kw: _FakeHarness(available=False, name="claude", **kw), overwrite=True)
        reg.register("agy", lambda **kw: _FakeHarness(available=True, name="agy", **kw), overwrite=True)

        health = await reg.check_all_health()
        self.assertTrue(health["agy"].available)
        self.assertFalse(health["claude"].available)
        self.assertFalse(health["fake"].available)
        self.assertEqual(await reg.list_available(), ["agy"])

        with self.assertRaises(KeyError):
            reg.create("unknown")

    async def test_three_level_profile_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            st = SystemState(state_file=os.path.join(tmpdir, "system_state.json"))
            st.set_harness("agy", "gemini-3.8-flash")
            st.set_effort("medium")
            st.set_permission_preset("auto")

            router = HarnessRouter(registry=create_default_registry(), state_manager=st)

            # Global only
            p_global = router.resolve_profile()
            self.assertEqual(p_global.harness, "agy")
            self.assertEqual(p_global.model, "gemini-3.8-flash")
            self.assertEqual(p_global.effort, "medium")
            self.assertEqual(p_global.permission_preset, PermissionPreset.AUTO)

            # Project overrides model + permission preset
            proj = Task(
                id="P-0",
                harness_config=HarnessConfig(model="gemini-3.1-pro", permission_preset="accept-edits"),
            )
            p_proj = router.resolve_profile(project_task=proj)
            self.assertEqual(p_proj.model, "gemini-3.1-pro")
            self.assertEqual(p_proj.effort, "medium")
            self.assertEqual(p_proj.permission_preset, PermissionPreset.ACCEPT_EDITS)

            # Task overrides effort + session_mode while inheriting project model & preset
            sub = Task(
                id="P-1",
                harness_config=HarnessConfig(effort="high", session_mode="sticky"),
            )
            p_task = router.resolve_profile(project_task=proj, active_task=sub)
            self.assertEqual(p_task.model, "gemini-3.1-pro")
            self.assertEqual(p_task.effort, "high")
            self.assertEqual(p_task.permission_preset, PermissionPreset.ACCEPT_EDITS)
            self.assertEqual(p_task.session_mode, "sticky")

    async def test_router_run_step_aggregation_and_unknown_harness(self):
        reg = HarnessRegistry()
        fake = _FakeHarness(
            name="agy",
            events=[
                HarnessEvent(type="init", harness="agy", session_id="s-1"),
                HarnessEvent(
                    type="tool",
                    harness="agy",
                    canonical_tool=CanonicalTool.SHELL,
                    tool_state="ACTIVE",
                    tool_info={"summary": "pytest"},
                ),
                HarnessEvent(
                    type="tool",
                    harness="agy",
                    canonical_tool=CanonicalTool.SHELL,
                    tool_state="DONE",
                    duration_seconds=1.2,
                    tool_info={"summary": "pytest"},
                ),
                HarnessEvent(type="text_delta", harness="agy", text_delta="All good"),
                HarnessEvent(
                    type="result",
                    harness="agy",
                    session_id="s-1",
                    status="SUCCESS",
                    duration_seconds=2.5,
                    usage=UsageMetrics(total_tokens=420),
                ),
            ],
        )
        reg.register("agy", lambda **kw: fake)
        router = HarnessRouter(registry=reg)

        seen_types = []
        res = await router.run_step(
            RunRequest(prompt="test", working_dir=tempfile.gettempdir()),
            on_event=lambda e: seen_types.append(e.type),
        )
        self.assertTrue(res.success)
        self.assertEqual(res.session_id, "s-1")
        self.assertEqual(res.response, "All good")
        self.assertEqual(res.usage.total_tokens, 420)
        self.assertEqual(len(res.tool_events), 1)
        self.assertIn("DONE", res.tool_events[0])
        self.assertTrue(fake.stopped)
        self.assertIn("result", seen_types)

        # Unregistered harness
        bad_profile = router.resolve_profile(
            active_task={"harness_config": {"harness": "missing_cli"}}
        )
        bad_res = await router.run_step(RunRequest(prompt="test", working_dir=tempfile.gettempdir()), profile=bad_profile)
        self.assertFalse(bad_res.success)
        self.assertEqual(bad_res.error_category, ErrorCategory.BINARY_NOT_FOUND)

    def test_system_state_migration_fallback_and_backoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "system_state.json")
            # Write legacy v1 state file
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"model_index": 1, "interval_seconds": 5, "start_time": "2026-03-01 10:00:00"}, f)

            st = SystemState(state_file=state_path)
            self.assertEqual(st.get_current_harness(), "agy")
            self.assertEqual(st.get_current_model(), DEFAULT_FALLBACK_CHAIN[1]["model"])
            self.assertEqual(st.get_state()["default_permission_preset"], "auto")

            # Advance across remaining fallback entries
            for _ in range(len(DEFAULT_FALLBACK_CHAIN) - 2):
                self.assertFalse(st.handle_429())

            # At end of chain -> backoff schedule 120 -> 300 -> 600
            for expected_wait in BACKOFF_SCHEDULE_SECONDS:
                self.assertTrue(st.handle_429())
                self.assertEqual(st.get_interval(), expected_wait)

            st.reset_success()
            self.assertEqual(st.get_interval(), 5)
            # reset_success preserves model_index
            self.assertEqual(st.get_state()["model_index"], len(DEFAULT_FALLBACK_CHAIN) - 1)

            st.reset_model()
            self.assertEqual(st.get_state()["model_index"], 0)
            self.assertEqual(st.get_current_model(), DEFAULT_FALLBACK_CHAIN[0]["model"])

            self.assertTrue(st.set_effort("xhigh"))
            self.assertFalse(st.set_effort("invalid"))
            self.assertEqual(st.set_permission_preset("readonly"), "plan")

    async def test_router_cancelled_when_harness_stopped_without_result(self):
        class _StoppedMidStreamHarness(_FakeHarness):
            async def run_stream(self, request: RunRequest):
                yield HarnessEvent(type="init", harness="agy", session_id="s-stop")
                yield HarnessEvent(type="text_delta", harness="agy", text_delta="<div class='partial'>")
                self.stopped = True
                return

        reg = HarnessRegistry()
        reg.register("agy", lambda **kw: _StoppedMidStreamHarness(name="agy"))
        router = HarnessRouter(registry=reg)

        res = await router.run_step(RunRequest(prompt="go", working_dir=tempfile.gettempdir()))
        self.assertFalse(res.success)
        self.assertEqual(res.status, "CANCELLED")
        self.assertEqual(res.error_category, ErrorCategory.CANCELLED)


if __name__ == "__main__":
    unittest.main()
