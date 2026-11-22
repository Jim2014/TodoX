import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.registry import HarnessRegistry
from harness.router import HarnessRouter
from harness.types import HarnessCapabilities, HarnessEvent, HealthStatus, ModelInfo
from orchestrator.state import SystemState
from orchestrator.storage import create_project, get_default_worker_prompt, load_task, save_task
from orchestrator.watcher import AsyncWatcher


class _SlowSteerableHarness(BaseHarness):
    def __init__(self, delay=0.2, steerable=True):
        self.delay = delay
        self._steerable = steerable
        self.stopped = False
        self.steered_messages = []
        self._running = False

    @property
    def capabilities(self):
        return HarnessCapabilities(name="agy", display_name="Slow Stub", supports_steering=self._steerable)

    def is_running(self):
        return self._running

    async def check_health(self):
        return HealthStatus(harness="agy", available=True)

    async def list_models(self, force_refresh=False):
        return [ModelInfo(id="default", name="Default", harness="agy")]

    async def steer(self, message: str) -> bool:
        if not self._steerable or not self._running:
            return False
        self.steered_messages.append(message)
        return True

    async def run_stream(self, request):
        self._running = True
        try:
            await asyncio.sleep(self.delay)
            if not self.stopped:
                yield HarnessEvent(type="result", harness="agy", status="SUCCESS", response="done")
        finally:
            self._running = False

    async def stop(self) -> None:
        self.stopped = True
        self._running = False


class TestAsyncWatcher(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        self._prev_tg_token = os.environ.pop("TG_TOKEN", None)
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        if self._prev_tg_token is not None:
            os.environ["TG_TOKEN"] = self._prev_tg_token
        self._tmpdir.cleanup()

    async def test_pause_filters_and_dedup_while_running(self):
        create_project("P1", "Proj 1", "Goal 1", get_default_worker_prompt())
        create_project("P2", "Proj 2", "Goal 2", get_default_worker_prompt())

        self.state.pause_project("P2")
        watcher = AsyncWatcher(state_manager=self.state, max_concurrent_workers=2)
        self.assertEqual(watcher.find_eligible_projects(), ["P1"])

        self.state.set_watcher_paused(True)
        self.assertEqual(watcher.find_eligible_projects(), [])
        self.state.set_watcher_paused(False)
        self.state.resume_project("P2")
        self.assertEqual(watcher.find_eligible_projects(), ["P1", "P2"])

    async def test_steer_and_stop_running_worker(self):
        create_project("RUNP", "Running Proj", "Goal", get_default_worker_prompt())
        created_harnesses = []

        def _factory(**kwargs):
            h = _SlowSteerableHarness(delay=0.6, steerable=True)
            created_harnesses.append(h)
            return h

        reg = HarnessRegistry()
        reg.register("agy", _factory)
        router = HarnessRouter(registry=reg, state_manager=self.state)
        watcher = AsyncWatcher(router=router, state_manager=self.state, max_concurrent_workers=2)

        triggered = await watcher.scan_once()
        self.assertEqual(triggered, ["RUNP"])
        # Second scan while RUNP is running must NOT double-trigger
        self.assertEqual(await watcher.scan_once(), [])

        await asyncio.sleep(0.05)
        self.assertTrue(watcher.is_project_running("RUNP"))

        ok, reason = await watcher.steer_project("RUNP", "check edge cases")
        self.assertTrue(ok)
        self.assertEqual(reason, "steered")
        self.assertEqual(created_harnesses[0].steered_messages, ["check edge cases"])

        stopped = await watcher.stop_project("RUNP")
        self.assertTrue(stopped)
        self.assertTrue(created_harnesses[0].stopped)
        self.assertFalse(watcher.is_project_running("RUNP"))
        task_after = load_task("RUNP", "RUNP-0")
        self.assertEqual(task_after.status, "failed")
        self.assertEqual(task_after.assignee, "Admin")

    async def test_non_steerable_harness_reports_not_steerable(self):
        create_project("CODEXP", "Non-steerable", "Goal", get_default_worker_prompt())
        reg = HarnessRegistry()
        reg.register("agy", lambda **kw: _SlowSteerableHarness(delay=0.3, steerable=False))
        router = HarnessRouter(registry=reg, state_manager=self.state)
        watcher = AsyncWatcher(router=router, state_manager=self.state, max_concurrent_workers=1)

        await watcher.trigger_project("CODEXP")
        await asyncio.sleep(0.05)
        ok, reason = await watcher.steer_project("CODEXP", "hello")
        self.assertFalse(ok)
        self.assertEqual(reason, "not_steerable")

        # Let step complete naturally and verify task wasn't left broken
        t = load_task("CODEXP", "CODEXP-0")
        t.status = "complete"
        t.assignee = "Admin"
        save_task("CODEXP", t)
        await watcher.stop()


if __name__ == "__main__":
    unittest.main()
