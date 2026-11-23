"""Isolated tests for event_bus, modern Kanban Web UI, REST APIs, and SSE streaming."""

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
from harness.types import CanonicalTool, HarnessCapabilities, HarnessEvent, HealthStatus, ModelInfo
from orchestrator.event_bus import (
    clear_stale_worker_runtime,
    drain_control_commands,
    enqueue_control_command,
    get_worker_runtime,
    mark_worker_finished,
    mark_worker_started,
    publish_event,
    read_recent_events,
    update_worker_activity,
)
from orchestrator.state import SystemState
from orchestrator.storage import create_project, create_task, get_default_worker_prompt, load_task
from orchestrator.watcher import AsyncWatcher
import orchestrator.worker as worker_module
from web.app import app


class _SlowStreamingStubHarness(BaseHarness):
    def __init__(self, events=None, steerable=True):
        self._events = list(events or [])
        self._steerable = steerable
        self.steered_messages = []
        self.stopped = False
        self._running = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name="agy", display_name="Stub", supports_steering=self._steerable)

    def is_running(self) -> bool:
        return self._running

    async def check_health(self) -> HealthStatus:
        return HealthStatus(harness="agy", available=True, message="ok")

    async def list_models(self, force_refresh: bool = False):
        return [ModelInfo(id="gemini-3.8-flash", name="Gemini 3.8 Flash", harness="agy")]

    async def steer(self, message: str) -> bool:
        if not self._steerable or not self._running:
            return False
        self.steered_messages.append(message)
        return True

    async def run_stream(self, request):
        self._running = True
        try:
            for ev in self._events:
                yield ev
        finally:
            self._running = False

    async def stop(self) -> None:
        self.stopped = True
        self._running = False


class TestWebKanbanAndSSE(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        self._prev_tg_token = os.environ.pop("TG_TOKEN", None)
        self._prev_admin_chat = os.environ.pop("ADMIN_CHAT_ID", None)
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.test_state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))
        self.client = TestClient(app)

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        if self._prev_tg_token is not None:
            os.environ["TG_TOKEN"] = self._prev_tg_token
        else:
            os.environ.pop("TG_TOKEN", None)
        if self._prev_admin_chat is not None:
            os.environ["ADMIN_CHAT_ID"] = self._prev_admin_chat
        else:
            os.environ.pop("ADMIN_CHAT_ID", None)
        self._tmpdir.cleanup()

    def test_event_bus_lifecycle_and_control_queue(self):
        mark_worker_started("GO", "GO-5", "agy", "gemini-3.8-flash", "auto")
        update_worker_activity("GO", last_tool="read_file go_game.html", tool_count=3)
        rt = get_worker_runtime()
        self.assertIn("GO", rt)
        self.assertEqual(rt["GO"]["tool_count"], 3)

        publish_event("tool", pid="GO", tid="GO-5", harness="agy", message="read_file ok")
        publish_event("tool", pid="OTHER", tid="OTHER-1", harness="agy", message="ignore me")
        publish_event("board_change", message="global notice")

        go_events = read_recent_events(limit=20, pid="GO")
        self.assertEqual(len(go_events), 2)
        self.assertEqual(go_events[0]["pid"], "GO")
        self.assertEqual(go_events[1]["message"], "global notice")

        mark_worker_finished("GO")
        self.assertEqual(get_worker_runtime(), {})

        mark_worker_started("STALE", "STALE-1", "agy", "default", "auto")
        clear_stale_worker_runtime(active_pids=[])
        self.assertEqual(get_worker_runtime(), {})

        enqueue_control_command("stop", "GO")
        enqueue_control_command("steer", "GO", "Check responsive canvas")
        cmds = drain_control_commands()
        self.assertEqual([c["action"] for c in cmds], ["stop", "steer"])
        self.assertEqual(drain_control_commands(), [])

    def test_kanban_rest_apis_sse_static_and_file_security(self):
        # 1. Create project + subtasks with numeric sorting (GO-2 before GO-10)
        resp = self.client.post(
            "/api/projects",
            json={"pid": "GO", "title": "Go <script>alert(1)</script>", "desc": "Board game"},
        )
        self.assertEqual(resp.status_code, 200)

        t1 = self.client.post(
            "/api/tasks/GO/GO-0",
            json={"title": "Backlog ticket", "desc": "Details", "status": "not_started", "assignee": "Admin"},
        )
        self.assertEqual(t1.status_code, 200)
        tid1 = t1.json()["tid"]

        t2 = self.client.post(
            "/api/tasks/GO/GO-0",
            json={
                "title": "Review ticket",
                "desc": "Needs approval",
                "status": "need_confirm",
                "assignee": "Admin",
                "harness_config": {"harness": "agy", "model": "gemini-3.1-pro"},
            },
        )
        self.assertEqual(t2.status_code, 200)
        tid2 = t2.json()["tid"]

        # 2. Enriched project list & detail endpoints
        list_res = self.client.get("/api/projects")
        self.assertEqual(list_res.status_code, 200)
        projs = list_res.json()["projects"]
        self.assertEqual(len(projs), 1)
        self.assertEqual(projs[0]["total_tasks"], 2)
        self.assertEqual(projs[0]["task_counts"]["need_confirm"], 1)

        detail_res = self.client.get("/api/projects/GO")
        self.assertEqual(detail_res.status_code, 200)
        detail = detail_res.json()
        self.assertEqual([t["id"] for t in detail["tasks"]], [tid1, tid2])
        self.assertEqual(detail["task_profiles"][tid2]["model"], "gemini-3.1-pro")

        # 3. Update + clear harness_config on ticket
        upd = self.client.put(
            f"/api/tasks/GO/{tid1}",
            json={"status": "in_progress", "assignee": "AI", "harness_config": {"effort": "xhigh"}},
        )
        self.assertEqual(upd.status_code, 200)
        self.assertEqual(load_task("GO", tid1).harness_config.effort, "xhigh")

        clr = self.client.put(f"/api/tasks/GO/{tid1}", json={"harness_config": {"clear": True}})
        self.assertEqual(clr.status_code, 200)
        self.assertIsNone(load_task("GO", tid1).harness_config)

        # 4. Run, Approve, Stop, Steer, Harnesses, System State APIs
        run_res = self.client.post(f"/api/tasks/GO/{tid1}/run")
        self.assertEqual(run_res.status_code, 200)
        self.assertEqual(load_task("GO", tid1).assignee, "AI")
        self.assertEqual(load_task("GO", tid1).status, "in_progress")

        app_res = self.client.post(f"/api/tasks/GO/{tid2}/approve", json={"comment": "Looks great"})
        self.assertEqual(app_res.status_code, 200)
        t2_after = load_task("GO", tid2)
        self.assertEqual(t2_after.status, "complete")
        self.assertEqual(t2_after.assignee, "Admin")
        self.assertEqual(t2_after.comments[-1].content, "Looks great")

        stop_res = self.client.post("/api/workers/GO/stop")
        self.assertEqual(stop_res.status_code, 200)
        steer_res = self.client.post("/api/workers/GO/steer", json={"msg": "Add tests"})
        self.assertEqual(steer_res.status_code, 200)
        queued_actions = [c["action"] for c in drain_control_commands()]
        self.assertEqual(queued_actions, ["trigger", "stop", "steer"])

        empty_steer = self.client.post("/api/workers/GO/steer", json={"msg": "   "})
        self.assertEqual(empty_steer.status_code, 400)

        h_res = self.client.get("/api/harnesses")
        self.assertEqual(h_res.status_code, 200)
        self.assertIn("models", h_res.json())

        # 5. HTML shells & static assets (offline only, no unescaped XSS)
        for route in ("/", "/project/GO", f"/task/GO/{tid1}"):
            html_res = self.client.get(route)
            self.assertEqual(html_res.status_code, 200)
            self.assertIn("/static/app.js", html_res.text)
            self.assertIn("/static/styles.css", html_res.text)
            self.assertNotIn("<script>alert(1)</script>", html_res.text)
            self.assertNotIn("https://", html_res.text)

        self.assertEqual(self.client.get("/project/NOPE").status_code, 404)
        self.assertEqual(self.client.get("/task/GO/GO-999").status_code, 404)

        css_res = self.client.get("/static/styles.css")
        self.assertEqual(css_res.status_code, 200)
        self.assertIn("kanban-board", css_res.text)
        js_res = self.client.get("/static/app.js")
        self.assertEqual(js_res.status_code, 200)
        self.assertIn("EventSource", js_res.text)

        # 6. SSE stream & recent events
        recent_res = self.client.get("/api/events/recent?pid=GO&limit=20")
        self.assertEqual(recent_res.status_code, 200)
        self.assertTrue(len(recent_res.json()["events"]) >= 2)

        sse_res = self.client.get("/api/events/stream?pid=GO&once=true")
        self.assertEqual(sse_res.status_code, 200)
        self.assertIn("text/event-stream", sse_res.headers["content-type"])
        self.assertIn("event: snapshot", sse_res.text)

        # 7. Workspace files preview + traversal defense
        proj_files_dir = os.path.join(self._tmpdir.name, "projects", "GO", "files")
        with open(os.path.join(proj_files_dir, "notes.md"), "w", encoding="utf-8") as f:
            f.write("# Notes\n- Passed")
        with open(os.path.join(proj_files_dir, "go_game.html"), "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><title>Go Board</title><body>OK</body></html>")
        with open(os.path.join(proj_files_dir, "engine.py"), "w", encoding="utf-8") as f:
            f.write("def play():\n    return 1\n")

        md_resp = self.client.get("/project/GO/files/files/notes.md")
        self.assertEqual(md_resp.status_code, 200)
        self.assertIn("<h1>Notes</h1>", md_resp.text)
        self.assertNotIn("cdn.jsdelivr.net", md_resp.text)

        html_game_resp = self.client.get("/project/GO/files/files/go_game.html")
        self.assertEqual(html_game_resp.status_code, 200)
        self.assertIn("<title>Go Board</title>", html_game_resp.text)

        py_preview = self.client.get("/project/GO/files/files/engine.py?preview=1")
        self.assertEqual(py_preview.status_code, 200)
        self.assertIn("hl-kw", py_preview.text)

        trav_resp = self.client.get("/project/GO/files/%2e%2e/%2e%2e/system_state.json")
        self.assertEqual(trav_resp.status_code, 403)

    async def test_worker_publishes_live_events_and_watcher_drains_controls(self):
        create_project("LIVE", "Live Stream Test", "Verify events", get_default_worker_prompt())
        drain_control_commands()

        stub = _SlowStreamingStubHarness(
            events=[
                HarnessEvent(type="init", harness="agy", session_id="s-live"),
                HarnessEvent(
                    type="tool",
                    harness="agy",
                    canonical_tool=CanonicalTool.READ,
                    tool_state="DONE",
                    duration_seconds=0.3,
                    tool_info={"summary": "project.json"},
                ),
                HarnessEvent(type="text_delta", harness="agy", text_delta="Implementing Kanban UI...\n"),
                HarnessEvent(type="result", harness="agy", status="SUCCESS", response="Done", duration_seconds=0.8),
            ]
        )
        reg = HarnessRegistry()
        reg.register("agy", lambda **kw: stub)
        router = HarnessRouter(registry=reg, state_manager=self.test_state)

        with unittest.mock.patch.object(worker_module, "state", self.test_state):
            res = await worker_module.execute_ai_task_async("LIVE", router=router)

        self.assertTrue(res.success)
        # Runtime entry cleared on finish
        self.assertNotIn("LIVE", get_worker_runtime())

        events = read_recent_events(limit=20, pid="LIVE")
        types = [e["type"] for e in events]
        self.assertIn("step_start", types)
        self.assertIn("tool", types)
        self.assertIn("text_delta", types)
        self.assertIn("step_finish", types)
        self.assertIn("board_change", types)

        # Verify AsyncWatcher.process_control_commands executes steer fallback comment
        watcher = AsyncWatcher(router=router, state_manager=self.test_state, max_concurrent_workers=1)
        enqueue_control_command("steer", "LIVE", "Prioritize mobile responsiveness")
        handled = await watcher.process_control_commands()
        self.assertEqual(handled, 1)
        root_after = load_task("LIVE", "LIVE-0")
        self.assertIn("Prioritize mobile responsiveness", root_after.comments[-1].content)


if __name__ == "__main__":
    unittest.main()
