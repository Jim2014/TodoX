import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.registry import HarnessRegistry
from harness.router import HarnessRouter, StepResult
from harness.types import (
    CanonicalTool,
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HarnessProfile,
    HealthStatus,
    ModelInfo,
    UsageMetrics,
)
from orchestrator.models import Task
from orchestrator.state import SystemState
from orchestrator.storage import create_project, create_task, get_default_worker_prompt, load_task
from telegram_ui.bot import TodoXTelegramBot
from telegram_ui.client import TelegramAPIError
from telegram_ui.notifier import WorkerNotifier


class _MockTelegramClient:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.sent_documents = []
        self.callback_answers = []
        self.reactions = []
        self.pinned = []
        self.unpinned = []
        self.unpinned_all = []
        self.commands = []
        self.closed = False

    async def get_me(self):
        return {"id": 999, "username": "todox_test_bot"}

    async def set_my_commands(self, commands, **kwargs):
        self.commands = commands
        return True

    async def set_chat_menu_button(self, **kwargs):
        return True

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return {"message_id": len(self.sent_messages)}

    async def edit_message_text(self, **kwargs):
        self.edited_messages.append(kwargs)
        return {"message_id": kwargs.get("message_id")}

    async def send_document(self, **kwargs):
        self.sent_documents.append(kwargs)
        return {"message_id": 1000 + len(self.sent_documents)}

    async def answer_callback_query(self, callback_query_id=None, **kwargs):
        kwargs["callback_query_id"] = callback_query_id
        self.callback_answers.append(kwargs)
        return True

    async def set_message_reaction(self, chat_id=None, message_id=None, reaction=None, **kwargs):
        self.reactions.append({"chat_id": chat_id, "message_id": message_id, "reaction": reaction, **kwargs})
        return True

    async def pin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        self.pinned.append({"chat_id": chat_id, "message_id": message_id, **kwargs})
        return True

    async def unpin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        self.unpinned.append({"chat_id": chat_id, "message_id": message_id, **kwargs})
        return True

    async def unpin_all_chat_messages(self, chat_id=None, **kwargs):
        self.unpinned_all.append(chat_id)
        return True

    async def send_chat_action(self, chat_id=None, action="typing", **kwargs):
        return True

    async def close(self):
        self.closed = True


class _StubHarness(BaseHarness):
    @property
    def capabilities(self):
        return HarnessCapabilities(name="agy", display_name="Agy Stub", supports_steering=True)

    async def check_health(self):
        return HealthStatus(harness="agy", available=True, version="1.5.14", message="OK")

    async def list_models(self, force_refresh=False):
        return [ModelInfo(id="gemini-3.8-flash", name="Gemini 3.8 Flash", harness="agy")]

    async def run_stream(self, request):
        yield HarnessEvent(type="result", harness="agy", status="SUCCESS", response="Repaired")

    async def stop(self):
        return None


class TestTelegramBotAndNotifier(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))
        self.reg = HarnessRegistry()
        self.reg.register("agy", lambda **kw: _StubHarness())
        self.router = HarnessRouter(registry=self.reg, state_manager=self.state)
        self.client = _MockTelegramClient()
        self.watcher = AsyncMock()
        self.watcher.running_pids = lambda: ["ALPHA"]
        self.watcher.stop_project = AsyncMock(return_value=True)
        self.watcher.steer_project = AsyncMock(return_value=(True, "steered"))
        self.watcher.trigger_project = AsyncMock(return_value=True)

        self.bot = TodoXTelegramBot(
            bot_token="123456:DUMMY_TOKEN_FOR_TESTS",
            allowed_chat_ids={100},
            allow_all_chats=False,
            registry=self.reg,
            router=self.router,
            watcher=self.watcher,
            state_manager=self.state,
            client=self.client,
        )

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        self._tmpdir.cleanup()

    async def test_deny_by_default_authorization_gate(self):
        await self.bot.process_update(
            {"message": {"message_id": 1, "chat": {"id": 999}, "text": "/projects"}}
        )
        self.assertEqual(len(self.client.sent_messages), 1)
        self.assertIn("Access Denied", self.client.sent_messages[0]["text"])

        await self.bot.process_update(
            {
                "callback_query": {
                    "id": "cq1",
                    "from": {"id": 999},
                    "data": "projects:list",
                    "message": {"message_id": 1, "chat": {"id": 999}},
                }
            }
        )
        self.assertEqual(self.client.callback_answers[-1]["text"], "Access denied")

    async def test_interactive_commands_callbacks_files_and_reply_comment(self):
        create_project("ALPHA", "Alpha Proj", "Build v2", get_default_worker_prompt())
        create_task("ALPHA", "ALPHA-0", "Task One", "Implement feature", assignee="Admin", status="need_confirm")
        notes_path = os.path.join(self._tmpdir.name, "projects", "ALPHA", "files", "notes.md")
        with open(notes_path, "w", encoding="utf-8") as f:
            f.write("# Notes\nAll good.")

        # 1. /help and /projects
        await self.bot.process_update({"message": {"message_id": 2, "chat": {"id": 100}, "text": "/help"}})
        self.assertIn("TodoX Orchestrator Command Center", self.client.sent_messages[-1]["text"])

        await self.bot.process_update({"message": {"message_id": 3, "chat": {"id": 100}, "text": "/projects"}})
        self.assertIn("ALPHA", self.client.sent_messages[-1]["text"])

        # 2. Open project card via callback, then reply directly to add a comment
        await self.bot.process_update(
            {
                "callback_query": {
                    "id": "cq2",
                    "from": {"id": 100},
                    "data": "proj:open:ALPHA",
                    "message": {"message_id": 10, "chat": {"id": 100}},
                }
            }
        )
        self.assertIn("Project ALPHA", self.client.edited_messages[-1]["text"])

        await self.bot.process_update(
            {
                "message": {
                    "message_id": 11,
                    "chat": {"id": 100},
                    "text": "Please focus on test coverage",
                    "reply_to_message": {"message_id": 10},
                }
            }
        )
        root_after = load_task("ALPHA", "ALPHA-0")
        self.assertEqual(root_after.comments[-1].content, "Please focus on test coverage")

        # 3. Browse files and download rendered HTML
        await self.bot.process_update(
            {
                "callback_query": {
                    "id": "cq3",
                    "from": {"id": 100},
                    "data": "proj:files:ALPHA:0",
                    "message": {"message_id": 10, "chat": {"id": 100}},
                }
            }
        )
        files = self.bot._file_lists_cache["ALPHA"]
        md_idx = files.index("files/notes.md")
        await self.bot.process_update(
            {
                "callback_query": {
                    "id": "cq4",
                    "from": {"id": 100},
                    "data": f"file:get:ALPHA:{md_idx}",
                    "message": {"message_id": 10, "chat": {"id": 100}},
                }
            }
        )
        self.assertEqual(self.client.sent_documents[-1]["filename"], "notes.md.html")

        # 4. Harness, model, effort, permissions, pause, resume, stop, steer, pin
        await self.bot.process_update({"message": {"message_id": 12, "chat": {"id": 100}, "text": "/effort xhigh"}})
        self.assertEqual(self.state.get_state()["default_effort"], "xhigh")

        await self.bot.process_update({"message": {"message_id": 13, "chat": {"id": 100}, "text": "/permissions accept-edits"}})
        self.assertEqual(self.state.get_state()["default_permission_preset"], "accept-edits")

        await self.bot.process_update({"message": {"message_id": 14, "chat": {"id": 100}, "text": "/pause ALPHA"}})
        self.assertTrue(self.state.is_project_paused("ALPHA"))

        await self.bot.process_update({"message": {"message_id": 15, "chat": {"id": 100}, "text": "/resume ALPHA"}})
        self.assertFalse(self.state.is_project_paused("ALPHA"))

        await self.bot.process_update({"message": {"message_id": 16, "chat": {"id": 100}, "text": "/steer ALPHA run pytest"}})
        self.watcher.steer_project.assert_awaited_with("ALPHA", "run pytest")

        await self.bot.process_update({"message": {"message_id": 17, "chat": {"id": 100}, "text": "/stop ALPHA"}})
        self.watcher.stop_project.assert_awaited_with("ALPHA")

        await self.bot.process_update({"message": {"message_id": 18, "chat": {"id": 100}, "text": "/pin"}})
        self.assertEqual(len(self.client.pinned), 1)
        await self.bot.process_update({"message": {"message_id": 19, "chat": {"id": 100}, "text": "/pin"}})
        self.assertEqual(len(self.client.pinned), 2)
        self.assertEqual(len(self.client.unpinned), 1)
        await self.bot.process_update({"message": {"message_id": 20, "chat": {"id": 100}, "text": "/unpin"}})
        self.assertEqual(self.client.unpinned_all, [100])

    async def test_startup_notification_does_not_auto_pin(self):
        self.client.sent_messages.clear()
        self.client.pinned.clear()
        await self.bot._notify_startup()
        self.assertEqual(len(self.client.sent_messages), 1)
        self.assertIn("TodoX Orchestrator Online", self.client.sent_messages[0]["text"])
        self.assertEqual(len(self.client.pinned), 0)

    async def test_legacy_vop_batch_commands_compatibility(self):
        batch = (
            '/new-project -pid VOP -title "VOP Test" -des "Compat check"\n'
            '/new-task -pid VOP -ptid VOP-0 -title "Step 1" -des "Details" -assignee AI -status in_progress\n'
            '/new-comment -pid VOP -tid VOP-1 -msg "Looks good"'
        )
        await self.bot.process_update({"message": {"message_id": 30, "chat": {"id": 100}, "text": batch}})
        t = load_task("VOP", "VOP-1")
        self.assertIsNotNone(t)
        self.assertEqual(t.title, "Step 1")
        self.assertEqual(t.comments[0].content, "Looks good")

    async def test_worker_notifier_live_streaming_and_error_document(self):
        notifier = WorkerNotifier(client=self.client, notify_chat_ids=[100], stream_throttle_seconds=0.0)
        tracker = await notifier.start_step("BETA", "BETA-1", HarnessProfile())
        self.assertEqual(len(self.client.sent_messages), 1)

        await notifier.on_event(
            tracker,
            HarnessEvent(
                type="tool",
                harness="agy",
                canonical_tool=CanonicalTool.SHELL,
                tool_state="DONE",
                duration_seconds=0.5,
                tool_info={"summary": "pytest"},
            ),
        )
        await notifier.on_event(tracker, HarnessEvent(type="text_delta", harness="agy", text_delta="Running tests..."))
        self.assertGreaterEqual(len(self.client.edited_messages), 1)

        failed = StepResult(
            success=False,
            harness="agy",
            model="gemini-3.8-flash",
            status="ERROR",
            error_category=ErrorCategory.PROCESS_CRASH,
            error_message="boom",
            duration_seconds=3.1,
            usage=UsageMetrics(total_tokens=200),
            output_lines=["line 1", "Traceback: boom"],
        )
        await notifier.finish_step(tracker, failed, Task(id="BETA-1", status="failed", assignee="Admin"))
        self.assertIn("Worker Step Failed", self.client.edited_messages[-1]["text"])
        self.assertEqual(self.client.sent_documents[-1]["filename"], "BETA_error_log.html")

    async def test_notifier_429_backoff_raw_html_export_and_stop_suppression(self):
        # 1. Per-chat 429 backoff skips intermediate edits without stalling, then exports raw HTML
        class _RateLimitedClient(_MockTelegramClient):
            def __init__(self):
                super().__init__()
                self.edit_calls = 0

            async def edit_message_text(self, **kwargs):
                self.edit_calls += 1
                if self.edit_calls == 1:
                    raise TelegramAPIError(429, "Too Many Requests", retry_after=60.0)
                return await super().edit_message_text(**kwargs)

        rl_client = _RateLimitedClient()
        notifier = WorkerNotifier(client=rl_client, notify_chat_ids=[100], stream_throttle_seconds=0.0)
        tracker = await notifier.start_step("GO", "GO-5", HarnessProfile())

        # First streaming edit hits 429 (retry_after=60s) -> sets backoff
        await notifier.on_event(tracker, HarnessEvent(type="text_delta", harness="agy", text_delta="Part 1"))
        # Second streaming edit must be skipped cleanly due to backoff
        await notifier.on_event(tracker, HarnessEvent(type="text_delta", harness="agy", text_delta="<div class='card'>"))
        self.assertEqual(rl_client.edit_calls, 1)
        self.assertGreater(notifier._chat_edit_backoff_until[100], 0.0)

        # Final step still delivers concise card + wrapped offline HTML report document
        raw_html = (
            "<!DOCTYPE html><html><head><title>GO Mobile Report</title></head><body>"
            + "<div class='card'><ul><li>Board ok</li></ul></div>" * 20
            + "</body></html>"
        )
        res = StepResult(
            success=True,
            harness="agy",
            model="gemini-3.8-flash",
            duration_seconds=4.0,
            response=raw_html,
            tool_events=["[tool:shell] DONE: node - <<'EOF'\nconsole.log(1)\n(1.0s)"],
        )
        await notifier.finish_step(tracker, res, Task(id="GO-5", status="complete", assignee="Admin"))
        self.assertEqual(rl_client.edit_calls, 2)
        final_text = rl_client.edited_messages[-1]["text"]
        self.assertNotIn("<div class=", final_text)
        self.assertIn("GO Mobile Report", final_text)
        self.assertIn("Worker Step Completed", final_text)
        self.assertEqual(rl_client.sent_documents[-1]["filename"], "GO_GO-5_report.html")

        # 2. /stop cancellation suppresses partial raw HTML delivery and attaches zero error logs
        stop_client = _MockTelegramClient()
        stop_notifier = WorkerNotifier(client=stop_client, notify_chat_ids=[100], stream_throttle_seconds=0.0)
        stop_tracker = await stop_notifier.start_step("GO", "GO-6", HarnessProfile())
        cancel_res = StepResult(
            success=False,
            harness="agy",
            status="CANCELLED",
            error_category=ErrorCategory.CANCELLED,
            error_message="Execution stopped by Admin.",
            output_lines=["partial raw noise"],
        )
        await stop_notifier.finish_step(stop_tracker, cancel_res, Task(id="GO-6", status="failed", assignee="Admin"))
        self.assertTrue(stop_tracker.stopped)
        self.assertEqual(len(stop_client.sent_documents), 0)
        self.assertIn("Worker Step Stopped", stop_client.edited_messages[-1]["text"])

    async def test_send_project_file_serves_html_directly_without_double_wrapping(self):
        create_project("WEB", "Web Game", "Serve HTML", get_default_worker_prompt())
        html_path = os.path.join(self._tmpdir.name, "projects", "WEB", "files", "go_game.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><head><title>Go Game</title></head><body><h1>Go</h1></body></html>")

        self.bot._scan_project_files("WEB")
        files = self.bot._file_lists_cache["WEB"]
        html_idx = files.index("files/go_game.html")
        await self.bot.process_update(
            {
                "callback_query": {
                    "id": "cq-html",
                    "from": {"id": 100},
                    "data": f"file:get:WEB:{html_idx}",
                    "message": {"message_id": 20, "chat": {"id": 100}},
                }
            }
        )
        self.assertEqual(self.client.sent_documents[-1]["filename"], "go_game.html")
        self.assertIn(b"<title>Go Game</title>", self.client.sent_documents[-1]["document_content"])


if __name__ == "__main__":
    unittest.main()
