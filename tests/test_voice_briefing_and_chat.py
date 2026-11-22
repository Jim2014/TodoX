"""Isolated tests for voice STT/TTS, project briefings, conversational router, Telegram voice/chat, and Web APIs."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.registry import HarnessRegistry
from harness.router import HarnessRouter
from harness.types import HarnessCapabilities, HarnessEvent, HealthStatus, ModelInfo
import orchestrator.briefing as briefing_mod
from orchestrator.briefing import build_project_briefing, generate_ai_project_briefing, save_cached_briefing
import orchestrator.chat_router as chat_router_mod
from orchestrator.chat_router import handle_project_chat
from orchestrator.state import SystemState
from orchestrator.storage import add_comment, create_project, create_task, get_default_worker_prompt, load_task
import orchestrator.voice as voice_mod
from orchestrator.voice import detect_spoken_language, distill_spoken_briefing
import telegram_ui.bot as bot_mod
from telegram_ui.bot import TodoXTelegramBot
from telegram_ui.client import TelegramAPIError, TelegramClient
import web.api as web_api_mod
from web.app import app


def _patch_all(targets: list[tuple[object, str]], replacement: object) -> ExitStack:
    """Patch a symbol on orchestrator and any module that imported it by value."""
    stack = ExitStack()
    for mod, attr in targets:
        if hasattr(mod, attr):
            stack.enter_context(patch.object(mod, attr, replacement))
    return stack


class _MockVoiceTelegramClient:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []
        self.sent_documents = []
        self.sent_voices = []
        self.callback_answers = []
        self.reactions = []
        self.downloaded_file_ids = []
        self.next_downloaded_bytes = b"OggS_DUMMY_AUDIO"

    async def get_me(self):
        return {"id": 999, "username": "todox_test_bot"}

    async def set_my_commands(self, commands, **kwargs):
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

    async def send_voice(self, **kwargs):
        self.sent_voices.append(kwargs)
        return {"message_id": 2000 + len(self.sent_voices)}

    async def download_file_by_id(self, file_id: str, destination_dir, filename_prefix: str = "voice"):
        self.downloaded_file_ids.append(file_id)
        dest_dir = Path(destination_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{filename_prefix}_{len(self.downloaded_file_ids)}.ogg"
        out.write_bytes(self.next_downloaded_bytes)
        return out, {"file_id": file_id, "file_path": f"voice/{out.name}"}

    async def answer_callback_query(self, callback_query_id=None, **kwargs):
        self.callback_answers.append({"callback_query_id": callback_query_id, **kwargs})
        return True

    async def set_message_reaction(self, chat_id=None, message_id=None, reaction=None, **kwargs):
        self.reactions.append({"chat_id": chat_id, "message_id": message_id, "reaction": reaction})
        return True

    async def send_chat_action(self, chat_id=None, action="typing", **kwargs):
        return True

    async def close(self):
        return None


class _SummaryStubHarness(BaseHarness):
    def __init__(self, summary_text: str = "Executive briefing: ALPHA is on track with tests passing."):
        self.summary_text = summary_text

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name="agy", display_name="Summary Stub", supports_steering=True)

    async def check_health(self) -> HealthStatus:
        return HealthStatus(harness="agy", available=True, message="OK")

    async def list_models(self, force_refresh: bool = False):
        return [ModelInfo(id="gemini-3.8-flash", name="Gemini 3.8 Flash", harness="agy")]

    async def run_stream(self, request):
        yield HarnessEvent(type="text_delta", harness="agy", text_delta=self.summary_text)
        yield HarnessEvent(type="result", harness="agy", status="SUCCESS", response=self.summary_text)

    async def stop(self) -> None:
        return None


class TestVoiceBriefingAndChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self._tmpdir.name
        self.state = SystemState(state_file=os.path.join(self._tmpdir.name, "system_state.json"))
        self.reg = HarnessRegistry()
        self.reg.register("agy", lambda **kw: _SummaryStubHarness())
        self.router = HarnessRouter(registry=self.reg, state_manager=self.state)

    def tearDown(self):
        if self._prev_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._prev_data_dir
        self._tmpdir.cleanup()

    async def test_voice_helpers_stt_and_tts_mocked(self):
        self.assertEqual(detect_spoken_language("Check ticket status and summarize."), "en")
        self.assertEqual(detect_spoken_language("\u8bf7\u603b\u7ed3\u5f53\u524d\u9879\u76ee\u8fdb\u5ea6"), "zh")

        distilled = distill_spoken_briefing(
            "# Headline\n- Finished **parser** [link](https://example.com)\n```py\nx = 1\n```\nNext: run tests."
        )
        self.assertNotIn("**", distilled)
        self.assertNotIn("```", distilled)
        self.assertIn("Finished parser link", distilled)
        self.assertLessEqual(len(distill_spoken_briefing("word " * 400, max_chars=90)), 93)

        # Mock faster_whisper + edge_tts + ffmpeg cleanly without network or model downloads
        for cache_attr in ("_WHISPER_CACHE", "_MODEL_CACHE", "_whisper_models"):
            cache_obj = getattr(voice_mod, cache_attr, None)
            if isinstance(cache_obj, dict):
                cache_obj.clear()

        class _FakeSeg:
            def __init__(self, text: str):
                self.text = text

        class _FakeInfo:
            language = "en"

        class _FakeWhisperModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                return [_FakeSeg("add unit tests"), _FakeSeg("for the parser")], _FakeInfo()

        class _FakeCommunicate:
            def __init__(self, text: str, voice: str):
                self.text = text
                self.voice = voice

            async def save(self, path: str):
                Path(path).write_bytes(b"ID3_FAKE_MP3_" + self.voice.encode("ascii", "ignore"))

        async def _fake_subproc(*args, **kwargs):
            out_path = Path(args[-1])
            out_path.write_bytes(b"OggS_FAKE_OPUS")
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        dummy_audio = Path(self._tmpdir.name) / "input.ogg"
        dummy_audio.write_bytes(b"OggS_INPUT")

        fake_fw = types.SimpleNamespace(WhisperModel=_FakeWhisperModel)
        fake_edge = types.SimpleNamespace(Communicate=_FakeCommunicate)
        with patch.dict("sys.modules", {"faster_whisper": fake_fw, "edge_tts": fake_edge}):
            with patch("orchestrator.voice.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"):
                with patch("orchestrator.voice.asyncio.create_subprocess_exec", side_effect=_fake_subproc):
                    text, lang = await voice_mod.transcribe_voice(dummy_audio, model_size="tiny")
                    self.assertEqual(text, "add unit tests for the parser")
                    self.assertEqual(lang, "en")

                    mp3_path = await voice_mod.synthesize_voice_mp3(
                        "Brief update ready.",
                        output_path=Path(self._tmpdir.name) / "out.mp3",
                    )
                    self.assertIsNotNone(mp3_path)
                    self.assertTrue(mp3_path.exists())
                    self.assertTrue(mp3_path.read_bytes().startswith(b"ID3_FAKE_MP3"))

                    ogg_path = await voice_mod.synthesize_voice_ogg(
                        "Brief update ready.",
                        output_path=Path(self._tmpdir.name) / "out.ogg",
                    )
                    self.assertIsNotNone(ogg_path)
                    self.assertTrue(ogg_path.exists())
                    self.assertTrue(ogg_path.read_bytes().startswith(b"OggS_FAKE_OPUS"))

    async def test_two_layer_project_briefing_lifecycle(self):
        create_project("ALPHA", "Alpha App", "Build core orchestrator", get_default_worker_prompt())
        b0 = build_project_briefing("ALPHA")
        self.assertIsNotNone(b0)
        self.assertEqual(b0["summary_source"], "project_goal")
        self.assertIn("Build core orchestrator", b0["summary_text"])

        create_task("ALPHA", "ALPHA-0", "Task One", "Implement parser", assignee="Admin", status="need_confirm")
        add_comment("ALPHA", "ALPHA-1", "AI", "Parser implemented and verified with edge tests.")
        b1 = build_project_briefing("ALPHA")
        self.assertEqual(b1["summary_source"], "latest_ticket")
        self.assertEqual(b1["latest_ticket_update"]["tid"], "ALPHA-1")
        self.assertIn("Parser implemented", b1["summary_text"])
        self.assertEqual(b1["attention_tasks"][0]["id"], "ALPHA-1")

        save_cached_briefing("ALPHA", "Worker completed ALPHA-1 cleanly.", source="worker_step")
        b2 = build_project_briefing("ALPHA")
        self.assertEqual(b2["summary_source"], "worker_step")
        self.assertIn("Worker completed ALPHA-1 cleanly", b2["summary_text"])

        b3 = await generate_ai_project_briefing("ALPHA", router=self.router)
        self.assertEqual(b3["summary_source"], "ai_refresh")
        self.assertFalse(b3["is_ai_summary_stale"])
        self.assertIn("ALPHA is on track", b3["summary_text"])

        # Adding a newer comment marks the cached AI executive summary stale while still keeping it visible
        await asyncio.sleep(0.02)
        add_comment("ALPHA", "ALPHA-1", "Admin", "Please also verify empty file edge cases.")
        b4 = build_project_briefing("ALPHA")
        self.assertEqual(b4["summary_source"], "ai_refresh")
        self.assertTrue(b4["is_ai_summary_stale"])

    async def test_shared_project_chat_router_intents(self):
        create_project("ALPHA", "Alpha App", "Core features", get_default_worker_prompt())
        create_task("ALPHA", "ALPHA-0", "Review Me", "Check output", assignee="Admin", status="need_confirm")
        watcher = AsyncMock()
        watcher.running_pids = lambda: []
        watcher.is_project_running = MagicMock(return_value=False)
        watcher.trigger_project = AsyncMock(return_value=True)
        watcher.stop_project = AsyncMock(return_value=True)
        watcher.steer_project = AsyncMock(return_value=(True, "steered"))

        # 1. Status/briefing request (English & CJK)
        res_status = await handle_project_chat("ALPHA", "What is the current status?", watcher=watcher, state_manager=self.state)
        self.assertTrue(res_status["ok"])
        self.assertEqual(res_status["action"], "briefing")

        res_zh = await handle_project_chat(
            "ALPHA",
            "\u603b\u7ed3\u4e00\u4e0b\u5f53\u524d\u9879\u76ee\u8fdb\u5ea6",
            watcher=watcher,
            state_manager=self.state,
        )
        self.assertEqual(res_zh["action"], "briefing")

        # 2. Approve need_confirm ticket
        res_approve = await handle_project_chat("ALPHA", "approve ALPHA-1", watcher=watcher, state_manager=self.state)
        self.assertEqual(res_approve["action"], "approve")
        self.assertEqual(res_approve["tid"], "ALPHA-1")
        self.assertEqual(load_task("ALPHA", "ALPHA-1").status, "complete")

        # 3. Create & run new ticket
        res_create = await handle_project_chat(
            "ALPHA",
            "Create a task to add keyboard shortcuts",
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
        )
        self.assertEqual(res_create["action"], "create_and_run")
        new_tid = res_create["tid"]
        self.assertEqual(new_tid, "ALPHA-2")
        t2 = load_task("ALPHA", "ALPHA-2")
        self.assertEqual(t2.status, "in_progress")
        self.assertEqual(t2.assignee, "AI")
        watcher.trigger_project.assert_awaited_with("ALPHA")

        # 4. Comment on existing ticket & re-run
        res_comment = await handle_project_chat(
            "ALPHA",
            "On ALPHA-2 please make sure Escape closes modals",
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
        )
        self.assertEqual(res_comment["action"], "comment_and_run")
        self.assertIn("Escape closes modals", load_task("ALPHA", "ALPHA-2").comments[-1].content)

        # 5. Stop & steer active worker
        watcher.is_project_running = MagicMock(return_value=True)
        res_steer = await handle_project_chat(
            "ALPHA",
            "steer run unit tests next",
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
        )
        self.assertEqual(res_steer["action"], "steer")
        watcher.steer_project.assert_awaited()

        res_stop = await handle_project_chat(
            "ALPHA",
            "stop worker",
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
        )
        self.assertEqual(res_stop["action"], "stop")
        watcher.stop_project.assert_awaited_with("ALPHA")
        watcher.is_project_running = MagicMock(return_value=False)

        # 6. Custom injectable intent classifier
        async def _custom_classifier(pid, message, tasks_summary, is_running):
            return {"intent": "create_and_run", "title": "Add CSV export", "description": "Support UTF-8 CSV export"}

        res_custom = await handle_project_chat(
            "ALPHA",
            "We need spreadsheet downloads too",
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
            intent_classifier=_custom_classifier,
        )
        self.assertEqual(res_custom["action"], "create_and_run")
        self.assertEqual(res_custom["tid"], "ALPHA-3")
        self.assertEqual(load_task("ALPHA", "ALPHA-3").title, "Add CSV export")

    async def test_telegram_client_download_and_send_voice_fallback(self):
        client = TelegramClient("123456:DUMMY_TOKEN")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.aclose = AsyncMock()

        get_file_resp = MagicMock()
        get_file_resp.status_code = 200
        get_file_resp.json.return_value = {"ok": True, "result": {"file_id": "v1", "file_path": "voice/v1.ogg"}}

        dl_resp = MagicMock()
        dl_resp.status_code = 200
        dl_resp.content = b"OggS_DOWNLOADED_BYTES"
        dl_resp.raise_for_status = MagicMock()

        html_err_resp = MagicMock()
        html_err_resp.status_code = 400
        html_err_resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}

        ok_voice_resp = MagicMock()
        ok_voice_resp.status_code = 200
        ok_voice_resp.json.return_value = {"ok": True, "result": {"message_id": 77}}

        mock_http.post = AsyncMock(side_effect=[get_file_resp, html_err_resp, ok_voice_resp])
        mock_http.get = AsyncMock(return_value=dl_resp)
        client._client = mock_http

        saved_path, meta = await client.download_file_by_id("v1", Path(self._tmpdir.name) / "dl")
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.read_bytes(), b"OggS_DOWNLOADED_BYTES")
        self.assertEqual(meta["file_path"], "voice/v1.ogg")

        res = await client.send_voice(
            chat_id=100,
            voice=b"OggS_OUT",
            caption="<b>Broken<tag</b>",
        )
        self.assertEqual(res["message_id"], 77)
        self.assertEqual(mock_http.post.await_count, 3)

    async def test_telegram_bot_use_summary_voice_and_callbacks(self):
        create_project("ALPHA", "Alpha Bot", "Automate builds", get_default_worker_prompt())
        create_task("ALPHA", "ALPHA-0", "Confirm Step", "Wait for review", assignee="Admin", status="need_confirm")

        client = _MockVoiceTelegramClient()
        watcher = AsyncMock()
        watcher.running_pids = lambda: []
        watcher.is_project_running = MagicMock(return_value=False)
        watcher.trigger_project = AsyncMock(return_value=True)
        watcher.stop_project = AsyncMock(return_value=True)
        watcher.steer_project = AsyncMock(return_value=(True, "steered"))

        bot = TodoXTelegramBot(
            bot_token="123456:DUMMY_TOKEN_FOR_TESTS",
            allowed_chat_ids={100},
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
            client=client,
        )

        # 1. Plain text before /use prompts project picker instead of creating a ticket
        await bot.process_update({"message": {"message_id": 1, "chat": {"id": 100}, "text": "add export button"}})
        self.assertIsNone(bot._get_active_project(100))
        self.assertIn("/use", client.sent_messages[-1]["text"])
        self.assertIsNone(load_task("ALPHA", "ALPHA-2"))

        # 2. /use ALPHA opens Briefing Card and persists active project across restarts
        await bot.process_update({"message": {"message_id": 2, "chat": {"id": 100}, "text": "/use ALPHA"}})
        self.assertEqual(bot._get_active_project(100), "ALPHA")
        self.assertIn("Project ALPHA", client.sent_messages[-1]["text"])
        self.assertIn("ACTIVE", client.sent_messages[-1]["text"])

        reloaded_bot = TodoXTelegramBot(
            bot_token="123456:DUMMY_TOKEN_FOR_TESTS",
            allowed_chat_ids={100},
            router=self.router,
            watcher=watcher,
            state_manager=self.state,
            client=client,
        )
        self.assertEqual(reloaded_bot._get_active_project(100), "ALPHA")

        # 3. Conversational text while active creates & triggers ticket
        await bot.process_update({"message": {"message_id": 3, "chat": {"id": 100}, "text": "add CSV export support"}})
        self.assertIsNotNone(load_task("ALPHA", "ALPHA-2"))
        self.assertIn("ALPHA-2", client.sent_messages[-1]["text"])

        # 4. /summary invokes AI router & updates message
        await bot.process_update({"message": {"message_id": 4, "chat": {"id": 100}, "text": "/summary"}})
        self.assertIn("ALPHA is on track", client.edited_messages[-1]["text"])

        # 5. Voice note download -> STT -> chat router -> OGG TTS reply
        dummy_ogg = Path(self._tmpdir.name) / "reply.ogg"
        dummy_ogg.write_bytes(b"OggS_REPLY")
        with _patch_all(
            [(voice_mod, "transcribe_voice"), (bot_mod, "transcribe_voice")],
            AsyncMock(return_value=("what is the current status?", "en")),
        ), _patch_all(
            [(voice_mod, "synthesize_voice_ogg"), (bot_mod, "synthesize_voice_ogg")],
            AsyncMock(return_value=dummy_ogg),
        ):
            await bot.process_update(
                {
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 100},
                        "voice": {"file_id": "tg_file_1", "duration": 2},
                    }
                }
            )
            self.assertEqual(client.downloaded_file_ids[-1], "tg_file_1")
            self.assertGreaterEqual(len(client.sent_voices), 1)
            self.assertIn("what is the current status?", client.sent_messages[-1]["text"])

            # 6. Inline callbacks: proj:voice:, task:approve:, proj:card:
            before_voices = len(client.sent_voices)
            await bot.process_update(
                {
                    "callback_query": {
                        "id": "cq_voice",
                        "from": {"id": 100},
                        "data": "proj:voice:ALPHA",
                        "message": {"message_id": 10, "chat": {"id": 100}},
                    }
                }
            )
            self.assertEqual(len(client.sent_voices), before_voices + 1)

        await bot.process_update(
            {
                "callback_query": {
                    "id": "cq_approve",
                    "from": {"id": 100},
                    "data": "task:approve:ALPHA:ALPHA-1",
                    "message": {"message_id": 10, "chat": {"id": 100}},
                }
            }
        )
        self.assertEqual(load_task("ALPHA", "ALPHA-1").status, "complete")

        await bot.process_update(
            {
                "callback_query": {
                    "id": "cq_card",
                    "from": {"id": 100},
                    "data": "proj:card:ALPHA",
                    "message": {"message_id": 10, "chat": {"id": 100}},
                }
            }
        )
        self.assertIn("Export HTML", str(client.edited_messages[-1]["reply_markup"]))

    def test_web_api_briefing_audio_chat_and_voice_endpoints(self):
        create_project("ALPHA", "Alpha Web", "Build web workspace", get_default_worker_prompt())
        create_task("ALPHA", "ALPHA-0", "Task One", "Implement UI", assignee="Admin", status="not_started")
        client = TestClient(app)

        # 1. GET /api/projects/{pid} embeds briefing & GET /api/projects/{pid}/briefing works
        r_proj = client.get("/api/projects/ALPHA")
        self.assertEqual(r_proj.status_code, 200)
        self.assertIn("briefing", r_proj.json())
        self.assertEqual(r_proj.json()["briefing"]["pid"], "ALPHA")

        r_brf = client.get("/api/projects/ALPHA/briefing")
        self.assertEqual(r_brf.status_code, 200)
        self.assertEqual(client.get("/api/projects/MISSING/briefing").status_code, 404)

        # 2. POST /api/projects/{pid}/briefing/refresh
        refreshed_payload = {**r_brf.json(), "summary_source": "ai_refresh", "summary_text": "Refreshed AI brief"}
        with _patch_all(
            [(briefing_mod, "generate_ai_project_briefing"), (web_api_mod, "generate_ai_project_briefing")],
            AsyncMock(return_value=refreshed_payload),
        ):
            r_ref = client.post("/api/projects/ALPHA/briefing/refresh", json={})
            self.assertEqual(r_ref.status_code, 200)
            self.assertEqual(r_ref.json()["summary_text"], "Refreshed AI brief")

        # 3. GET /api/projects/{pid}/briefing/audio
        dummy_mp3 = Path(self._tmpdir.name) / "brief.mp3"
        dummy_mp3.write_bytes(b"ID3_BRIEF_AUDIO")
        with _patch_all(
            [(voice_mod, "synthesize_voice_mp3"), (web_api_mod, "synthesize_voice_mp3")],
            AsyncMock(return_value=dummy_mp3),
        ):
            r_aud = client.get("/api/projects/ALPHA/briefing/audio")
            self.assertEqual(r_aud.status_code, 200)
            self.assertIn("audio/mpeg", r_aud.headers.get("content-type", ""))
            self.assertEqual(r_aud.content, b"ID3_BRIEF_AUDIO")

        # 4. POST /api/projects/{pid}/chat
        with _patch_all(
            [(chat_router_mod, "_default_llm_classify")],
            AsyncMock(return_value=None),
        ):
            r_chat = client.post("/api/projects/ALPHA/chat", json={"msg": "Create a task for search filter"})
            self.assertEqual(r_chat.status_code, 200)
            chat_json = r_chat.json()
            self.assertEqual(chat_json["action"], "create_and_run")
            self.assertEqual(chat_json["tid"], "ALPHA-2")
            self.assertIsNotNone(load_task("ALPHA", "ALPHA-2"))

        # 5. POST /api/projects/{pid}/voice (multipart audio upload)
        with _patch_all(
            [(voice_mod, "transcribe_voice"), (web_api_mod, "transcribe_voice")],
            AsyncMock(return_value=("create a task for dark theme polish", "en")),
        ), _patch_all(
            [(chat_router_mod, "_default_llm_classify")],
            AsyncMock(return_value=None),
        ):
            r_voice = client.post(
                "/api/projects/ALPHA/voice",
                files={"file": ("mic.webm", b"OggS_MIC_AUDIO", "audio/webm")},
            )
            self.assertEqual(r_voice.status_code, 200)
            vj = r_voice.json()
            self.assertEqual(vj["transcript"], "create a task for dark theme polish")
            self.assertEqual(vj["action"], "create_and_run")
            self.assertEqual(vj["tid"], "ALPHA-3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
