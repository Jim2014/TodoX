import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.router import StepResult
from harness.types import CanonicalTool, ErrorCategory, HarnessEvent, HarnessProfile, HealthStatus, ModelInfo, UsageMetrics
from telegram_ui.client import TelegramAPIError, TelegramClient, redact_bot_token, strip_telegram_html
from telegram_ui.file_renderers import format_file_size, is_binary_file, render_file_to_html, render_markdown_to_html
from telegram_ui.formatter import (
    balance_telegram_html,
    build_auto_export_preview,
    format_harness_tool_event,
    format_pinned_dashboard,
    format_worker_final_card,
    format_worker_live_card,
    looks_like_raw_html_document,
    markdown_to_telegram_html,
    mask_streaming_protocol_and_html,
    split_text_chunks,
    wrap_raw_html_for_mobile,
)
from telegram_ui.keyboards import (
    build_effort_keyboard,
    build_files_keyboard,
    build_harness_keyboard,
    build_help_keyboard,
    build_model_keyboard,
    build_permissions_keyboard,
    build_pinned_banner_keyboard,
    build_project_card,
    build_projects_keyboard,
    build_task_card,
    build_tasks_keyboard,
    build_worker_done_keyboard,
    build_worker_running_keyboard,
)
from harness.types import HarnessProfile, HealthStatus, ModelInfo
from orchestrator.html_generator import generate_markdown_viewer_html
from orchestrator.models import ProjectSummary, Task


class TestTelegramClientAndFormatter(unittest.IsolatedAsyncioTestCase):
    def test_token_redaction(self):
        masked = redact_bot_token("POST https://api.telegram.org/bot123456:SECRET_ABC_123/sendMessage")
        self.assertNotIn("SECRET_ABC_123", masked)
        self.assertIn("bot<REDACTED>", masked)

    async def test_client_retries_429_409_html_fallback_and_docs(self):
        client = TelegramClient("123456:DUMMY_SECRET_TOKEN")

        # 1. Transient RequestError retry on idempotent editMessageText
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.aclose = AsyncMock()
        ok_resp = MagicMock()
        ok_resp.json.return_value = {"ok": True, "result": {"message_id": 10}}
        ok_resp.status_code = 200
        mock_http.post = AsyncMock(side_effect=[httpx.ConnectError("transient"), ok_resp])
        client._client = mock_http

        res = await client.edit_message_text(chat_id=100, message_id=10, text="Updated")
        self.assertEqual(res["message_id"], 10)
        self.assertEqual(mock_http.post.call_count, 2)

        # 2. HTTP 429 retry_after handling
        r429 = MagicMock()
        r429.status_code = 429
        r429.json.return_value = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0.01}}
        r_ok = MagicMock()
        r_ok.status_code = 200
        r_ok.json.return_value = {"ok": True, "result": {"message_id": 11}}
        mock_http.post = AsyncMock(side_effect=[r429, r_ok])

        sent = await client.send_message(chat_id=100, text="Hello")
        self.assertEqual(sent["message_id"], 11)

        # 3. 409 Conflict propagates when raise_on_error=True
        r409 = MagicMock()
        r409.status_code = 409
        r409.json.return_value = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates"}
        mock_http.post = AsyncMock(return_value=r409)
        with self.assertRaises(TelegramAPIError) as ctx:
            await client.get_updates(raise_on_error=True)
        self.assertEqual(ctx.exception.error_code, 409)

        # 4. HTML parse error falls back to plain text
        r_bad_html = MagicMock()
        r_bad_html.status_code = 400
        r_bad_html.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
        mock_http.post = AsyncMock(side_effect=[r_bad_html, r_ok])
        sent2 = await client.send_message(chat_id=100, text="<b>unclosed")
        self.assertEqual(sent2["message_id"], 11)
        second_payload = mock_http.post.call_args_list[1].kwargs["json"]
        self.assertNotIn("parse_mode", second_payload)

        # 5. "message is not modified" treated as no-op None
        r_not_mod = MagicMock()
        r_not_mod.status_code = 400
        r_not_mod.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
        mock_http.post = AsyncMock(return_value=r_not_mod)
        self.assertIsNone(await client.edit_message_text(chat_id=100, message_id=10, text="Same"))

        # 6. send_document MIME and upload
        mock_http.post = AsyncMock(return_value=r_ok)
        doc_res = await client.send_document(chat_id=100, document_content=b"<html></html>", filename="report.html")
        self.assertEqual(doc_res["message_id"], 11)
        await client.close()

    def test_formatter_cards_and_html_balancing(self):
        balanced = balance_telegram_html("<b>hello <i>world")
        self.assertTrue(balanced.endswith("</i></b>"))

        md = "# Section\nUse `agy` now.\n\n| Key | Val |\n| --- | --- |\n| Harness | agy |\n\n```python\nprint(1)\n```"
        tg_html = markdown_to_telegram_html(md)
        self.assertIn("<b>Section</b>", tg_html)
        self.assertIn("<code>agy</code>", tg_html)
        self.assertIn("• <b>Harness</b>: agy", tg_html)
        self.assertIn('<pre><code class="language-python">', tg_html)

        ev = HarnessEvent(
            type="tool",
            harness="agy",
            canonical_tool=CanonicalTool.EDIT,
            tool_state="DONE",
            duration_seconds=0.8,
            tool_info={"summary": "worker.py"},
        )
        t_str = format_harness_tool_event(ev)
        self.assertIn("worker.py", t_str)

        live_card = format_worker_live_card(
            "ALPHA", "ALPHA-1", "agy", "gemini-3.8-flash", "auto", [t_str], "Running **tests**", elapsed_seconds=12.4
        )
        self.assertIn("<blockquote expandable>", live_card)
        self.assertLessEqual(len(live_card), 4000)

        res = StepResult(
            success=True,
            harness="agy",
            model="gemini-3.8-flash",
            duration_seconds=5.2,
            usage=UsageMetrics(total_tokens=2400, thinking_tokens=400),
            tool_events=[t_str],
        )
        final_card = format_worker_final_card("ALPHA", "ALPHA-1", res, "complete", "Admin", latest_comment="All green")
        self.assertIn("Worker Step Completed", final_card)
        self.assertIn("2.4k", final_card)

        dash = format_pinned_dashboard(
            {"default_harness": "agy", "default_model": "default", "default_effort": "high", "default_permission_preset": "auto", "interval_seconds": 5},
            running_pids=["ALPHA"],
            paused_global=False,
            paused_projects=["BETA"],
            project_count=2,
        )
        self.assertIn("ALPHA", dash)
        self.assertIn("BETA", dash)

        chunks = split_text_chunks("word " * 1200, chunk_size=1000)
        self.assertGreater(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch), 1020)

    def test_offline_file_renderers_have_zero_cdn_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = Path(tmpdir) / "notes.md"
            md_path.write_text("# Title\n\n| A | B |\n| - | - |\n| 1 | 2 |\n", encoding="utf-8")
            md_bytes = render_file_to_html(md_path, "notes.md", pid="DEMO")
            self.assertIsNotNone(md_bytes)
            md_str = md_bytes.decode("utf-8")
            self.assertNotIn("http://", md_str)
            self.assertNotIn("https://", md_str)
            self.assertIn("<td>1</td>", md_str)

            py_path = Path(tmpdir) / "app.py"
            py_path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            py_bytes = render_file_to_html(py_path, "app.py", pid="DEMO")
            self.assertIsNotNone(py_bytes)
            py_str = py_bytes.decode("utf-8")
            self.assertNotIn("http://", py_str)
            self.assertNotIn("https://", py_str)
            self.assertIn("hl-kw", py_str)

            bin_path = Path(tmpdir) / "img.png"
            bin_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
            self.assertTrue(is_binary_file(bin_path))
            self.assertIsNone(render_file_to_html(bin_path, "img.png", pid="DEMO"))
            self.assertEqual(format_file_size(1536), "1.5 KB")

            gen_bytes = generate_markdown_viewer_html("DEMO", "notes.md", "# Offline\nOK").decode("utf-8")
            self.assertNotIn("cdn.jsdelivr.net", gen_bytes)
            self.assertNotIn("cdnjs.cloudflare.com", gen_bytes)

    def test_all_keyboards_callback_data_under_64_bytes(self):
        profile = HarnessProfile(harness="agy", model="gemini-3.8-flash", effort="high")
        task = Task(id="VERYLONGPID-1", title="Subtask", descriptions="Desc", status="in_progress", assignee="AI")
        root = Task(id="VERYLONGPID-0", title="Root", descriptions="Root", status="in_progress", assignee="AI")
        projs = {"ALPHA": ProjectSummary(id="ALPHA", title="Alpha", status="in_progress", assignee="AI")}

        _, k1 = build_projects_keyboard(projs, ["ALPHA"], [])
        _, k2 = build_project_card("VERYLONGPID", root, [task], True, False, profile)
        _, k3 = build_tasks_keyboard("VERYLONGPID", [task])
        _, k4 = build_task_card("VERYLONGPID", task, profile)
        _, k5 = build_files_keyboard("VERYLONGPID", ["src/main.py", "README.md"], page=0)
        _, k6 = build_harness_keyboard({"agy": HealthStatus("agy", True, version="1.5.14")}, "agy", "default")
        _, k7 = build_model_keyboard(
            [ModelInfo(id="very-long-model-name-with-lots-of-tokens-and-variants-1234567890", name="Long Model", harness="agy")],
            "agy",
            "default",
            [{"harness": "agy", "model": "default"}],
            0,
        )
        _, k8 = build_effort_keyboard("high")
        _, k9 = build_permissions_keyboard("auto")

        all_kbs = [
            build_help_keyboard(),
            k1, k2, k3, k4, k5, k6, k7, k8, k9,
            build_worker_running_keyboard("VERYLONGPID"),
            build_worker_done_keyboard("VERYLONGPID", "VERYLONGPID-1", "need_confirm", False),
            build_pinned_banner_keyboard(True),
        ]
        for kb in all_kbs:
            for row in kb["inline_keyboard"]:
                for btn in row:
                    self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64, btn["callback_data"])

    async def test_429_fast_abort_vs_retry_and_html_strip_fallback(self):
        client = TelegramClient("123456:DUMMY_SECRET_TOKEN")
        mock_http = MagicMock()
        mock_http.is_closed = False
        mock_http.aclose = AsyncMock()
        client._client = mock_http

        # 1. Large 429 retry_after (196s) on non-blocking editMessageText fails fast without sleeping
        r_big_429 = MagicMock()
        r_big_429.status_code = 429
        r_big_429.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 196},
        }
        mock_http.post = AsyncMock(return_value=r_big_429)
        with unittest.mock.patch("telegram_ui.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(TelegramAPIError) as exc_info:
                await client._post("editMessageText", {"chat_id": 100, "message_id": 1, "text": "hi"})
            self.assertEqual(exc_info.exception.error_code, 429)
            self.assertEqual(exc_info.exception.retry_after, 196.0)
            mock_sleep.assert_not_awaited()

        # 2. Small 429 retry_after (1s) waits once and succeeds
        r_small_429 = MagicMock()
        r_small_429.status_code = 429
        r_small_429.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 1},
        }
        r_ok = MagicMock()
        r_ok.status_code = 200
        r_ok.json.return_value = {"ok": True, "result": {"message_id": 55}}
        mock_http.post = AsyncMock(side_effect=[r_small_429, r_ok])
        with unittest.mock.patch("telegram_ui.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await client._post("sendMessage", {"chat_id": 100, "text": "ok"})
            self.assertEqual(res["message_id"], 55)
            mock_sleep.assert_awaited_once_with(1.0)

        # 3. HTML parse fallback strips HTML tags and unescapes entities so <b> never leaks
        r_bad_html = MagicMock()
        r_bad_html.status_code = 400
        r_bad_html.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
        mock_http.post = AsyncMock(side_effect=[r_bad_html, r_ok])
        await client.send_message(
            chat_id=100,
            text="<b>Worker Completed</b>\n<blockquote expandable>&lt;&lt;EOF &amp;&amp;</blockquote>",
        )
        fallback_payload = mock_http.post.call_args_list[1].kwargs["json"]
        self.assertNotIn("parse_mode", fallback_payload)
        self.assertNotIn("<b>", fallback_payload["text"])
        self.assertNotIn("<blockquote", fallback_payload["text"])
        self.assertIn("Worker Completed", fallback_payload["text"])
        self.assertIn("<<EOF &&", fallback_payload["text"])
        await client.close()

    def test_tool_html_escaping_tag_repair_raw_html_masking_and_mobile_wrap(self):
        # 1. Final card MUST escape raw <<'EOF' and > /dev/null && in tool_events
        go_tools = [
            "[tool:shell] DONE: node - <<'EOF'\nconst fs = require('fs');\n(2.8s)",
            "[tool:shell] DONE: python -m json.tool /tmp/a.json > /dev/null && echo ok (1.5s)",
        ]
        res = StepResult(
            success=True,
            harness="agy",
            model="gemini-3.8-flash",
            duration_seconds=370.0,
            tool_events=go_tools,
            response="Done",
        )
        final_card = format_worker_final_card(
            "GO",
            "GO-5",
            res,
            "complete",
            "Admin",
            latest_comment="Updated `files/go_game.html` with `<meta name=\"viewport\">`.",
        )
        self.assertNotIn("<<", final_card)
        self.assertNotIn("&&", final_card)
        self.assertIn("&lt;&lt;", final_card)
        self.assertIn("&gt;", final_card)
        self.assertIn("&amp;&amp;", final_card)

        # 2. Misnested and stray closing tags are repaired cleanly
        self.assertEqual(balance_telegram_html("<b>foo <i>bar</b> baz</i>"), "<b>foo <i>bar</i></b><i> baz</i>")
        stray_repaired = balance_telegram_html("hello </b> world")
        self.assertNotIn("</b>", stray_repaired)
        self.assertIn("hello", stray_repaired)

        # 3. Streaming masks in-flight [AGY_FILE], [AGY_VOICE], [AGY_ACTIONS], and raw HTML
        stream_proto = (
            "Checking **TodoX** runtime.\n\n"
            "[AGY_FILE]\nfilename: todox.html\n---\n"
            "<!DOCTYPE html><html><body><div class=\"card\"><ul><li>Long HTML body"
        )
        masked_live = format_worker_live_card("GO", "GO-5", "agy", "gemini-3.8-flash", "auto", [], stream_proto)
        self.assertIn("Generating standalone HTML report attachment", masked_live)
        self.assertNotIn("<div class=", masked_live)
        self.assertNotIn("<li>", masked_live)

        raw_html = "Summary below:\n\n<div class=\"card\">\n  <div class=\"card-title\">Status</div>\n  <ul><li>Ok</li></ul>"
        self.assertTrue(looks_like_raw_html_document(raw_html))
        self.assertNotIn("<div", mask_streaming_protocol_and_html(raw_html))

        # 4. Worst-case '<'-heavy live card still stays <= 4000 chars
        huge_lt_body = "<a_lot_of_brackets> " * 600
        bounded_live = format_worker_live_card("GO", "GO-5", "agy", "default", "auto", [], huge_lt_body)
        self.assertLessEqual(len(bounded_live), 4000)

        # 5. Preview condensation and offline mobile wrapper (zero CDN URLs)
        long_html = (
            "<!DOCTYPE html><html><head><title>TodoX Audit</title></head>"
            "<body><div class='card'><ul><li>Line details</li></ul></div>" * 10
            + "</body></html>"
        )
        preview = build_auto_export_preview(long_html, "GO_GO-5_report.html")
        self.assertLess(len(preview), 700)
        self.assertNotIn("<div", preview)
        self.assertIn("TodoX Audit", preview)

        wrapped_frag = wrap_raw_html_for_mobile("Fragment", "<div><p>Offline check</p></div>", {"project": "GO"})
        self.assertIn("<!DOCTYPE html>", wrapped_frag)
        self.assertNotIn("http://", wrapped_frag)
        self.assertNotIn("https://", wrapped_frag)


if __name__ == "__main__":
    unittest.main()
