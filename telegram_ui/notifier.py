"""Real-time Telegram notification streamer and error-log uploader for TodoX workers."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from harness.router import StepResult
from harness.types import ErrorCategory, HarnessEvent, HarnessProfile
from orchestrator.models import Task

from .client import TelegramAPIError, TelegramClient, format_exception_brief
from .formatter import (
    escape_html,
    format_harness_tool_event,
    format_worker_final_card,
    format_worker_live_card,
    looks_like_raw_html_document,
    wrap_raw_html_for_mobile,
)
from .keyboards import build_worker_done_keyboard, build_worker_running_keyboard

logger = logging.getLogger(__name__)


@dataclass
class LiveStepTracker:
    """Tracks live Telegram message IDs and streaming state for one worker step."""

    pid: str
    tid: str
    harness: str
    model: Optional[str]
    preset: str
    started_at: float = field(default_factory=time.time)
    last_edit_at: float = 0.0
    message_ids: Dict[int, int] = field(default_factory=dict)
    tool_events: List[str] = field(default_factory=list)
    current_text: str = ""
    last_rendered: str = ""
    stopped: bool = False


class WorkerNotifier:
    """Streams live worker tool/text events and delivers completion or failure cards."""

    def __init__(
        self,
        client: Optional[TelegramClient],
        notify_chat_ids: Sequence[int],
        stream_throttle_seconds: float = 1.5,
    ) -> None:
        self.client = client
        self.notify_chat_ids = list(dict.fromkeys(int(cid) for cid in notify_chat_ids))
        self.stream_throttle_seconds = max(0.0, float(stream_throttle_seconds))
        self._chat_edit_backoff_until: Dict[int, float] = {}

    def has_targets(self) -> bool:
        """Return True when both a TelegramClient and at least one chat ID are configured."""
        return self.client is not None and len(self.notify_chat_ids) > 0

    async def start_step(self, pid: str, tid: str, profile: HarnessProfile) -> LiveStepTracker:
        """Send initial live progress card for a worker step."""
        tracker = LiveStepTracker(
            pid=pid,
            tid=tid,
            harness=profile.harness,
            model=profile.model,
            preset=profile.permission_preset.value,
        )
        if not self.has_targets():
            return tracker

        assert self.client is not None
        initial_text = format_worker_live_card(
            pid=pid,
            tid=tid,
            harness=tracker.harness,
            model=tracker.model,
            preset=tracker.preset,
            tool_events=[],
            current_text="",
            elapsed_seconds=0.0,
            status_label="Starting step...",
        )
        markup = build_worker_running_keyboard(pid)
        for chat_id in self.notify_chat_ids:
            try:
                res = await self.client.send_message(
                    chat_id=chat_id,
                    text=initial_text,
                    reply_markup=markup,
                    disable_notification=True,
                )
                msg_id = res.get("message_id")
                if msg_id:
                    tracker.message_ids[chat_id] = int(msg_id)
            except Exception as exc:
                logger.warning("Failed to send live step start card to %s: %s", chat_id, format_exception_brief(exc))

        tracker.last_rendered = initial_text
        tracker.last_edit_at = time.time()
        return tracker

    async def on_event(self, tracker: LiveStepTracker, event: HarnessEvent) -> None:
        """Update tracker buffers on a HarnessEvent and push throttled live card edits."""
        if tracker.stopped:
            return

        if event.type == "tool":
            line = format_harness_tool_event(event)
            if tracker.tool_events and tracker.tool_events[-1].endswith("...") and event.tool_state != "ACTIVE":
                tracker.tool_events[-1] = line
            else:
                tracker.tool_events.append(line)
        elif event.type == "text_delta" and event.text_delta:
            tracker.current_text += event.text_delta
        elif event.type == "result" and event.response and not tracker.current_text:
            tracker.current_text = event.response

        if not self.has_targets() or not tracker.message_ids:
            return

        now = time.time()
        elapsed = now - tracker.started_at
        if self.stream_throttle_seconds > 0.0:
            if elapsed >= 90.0:
                effective_throttle = max(self.stream_throttle_seconds, 4.0)
            elif elapsed >= 30.0:
                effective_throttle = max(self.stream_throttle_seconds, 2.5)
            else:
                effective_throttle = self.stream_throttle_seconds
        else:
            effective_throttle = 0.0

        if (now - tracker.last_edit_at) < effective_throttle:
            return

        rendered = format_worker_live_card(
            pid=tracker.pid,
            tid=tracker.tid,
            harness=tracker.harness,
            model=tracker.model,
            preset=tracker.preset,
            tool_events=tracker.tool_events,
            current_text=tracker.current_text,
            elapsed_seconds=elapsed,
            status_label="Running...",
        )
        if rendered == tracker.last_rendered:
            return

        assert self.client is not None
        markup = build_worker_running_keyboard(tracker.pid)
        any_attempted = False
        for chat_id, msg_id in list(tracker.message_ids.items()):
            if now < self._chat_edit_backoff_until.get(chat_id, 0.0):
                continue
            any_attempted = True
            try:
                await self.client.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=rendered,
                    reply_markup=markup,
                )
            except TelegramAPIError as exc:
                if exc.error_code == 429 and exc.retry_after:
                    backoff_until = time.time() + float(exc.retry_after)
                    self._chat_edit_backoff_until[chat_id] = max(
                        self._chat_edit_backoff_until.get(chat_id, 0.0),
                        backoff_until,
                    )
                logger.warning("Live card edit failed for %s/%s: %s", chat_id, msg_id, format_exception_brief(exc))
            except Exception as exc:
                logger.debug("Live card edit skipped for %s/%s: %s", chat_id, msg_id, format_exception_brief(exc))

        if any_attempted:
            tracker.last_rendered = rendered
            tracker.last_edit_at = now

    async def finish_step(
        self,
        tracker: LiveStepTracker,
        result: StepResult,
        task_after: Optional[Task] = None,
    ) -> None:
        """Replace live progress card with final summary and attach HTML report or error log."""
        is_cancelled = result.error_category == ErrorCategory.CANCELLED or tracker.stopped
        if is_cancelled:
            tracker.stopped = True

        if not self.has_targets():
            return

        assert self.client is not None
        task_status = task_after.status if task_after else ("complete" if result.success else "failed")
        task_assignee = task_after.assignee if task_after else "Admin"
        latest_comment = ""
        if task_after and task_after.comments:
            latest_comment = task_after.comments[-1].content

        raw_html_source = (
            latest_comment
            if looks_like_raw_html_document(latest_comment)
            else (result.response or "")
        )
        raw_html_response = (not is_cancelled) and looks_like_raw_html_document(raw_html_source)
        auto_export_filename = f"{tracker.pid}_{tracker.tid}_report.html" if raw_html_response else None

        final_text = format_worker_final_card(
            pid=tracker.pid,
            tid=tracker.tid,
            result=result,
            task_status=task_status,
            task_assignee=task_assignee,
            latest_comment=latest_comment,
            formatted_tool_events=tracker.tool_events,
            auto_export_filename=auto_export_filename,
        )
        has_error = (
            not result.success
            and not is_cancelled
            and not result.fallback_switched
            and not result.backoff_triggered
        )
        markup = build_worker_done_keyboard(
            pid=tracker.pid,
            tid=tracker.tid,
            task_status=task_status,
            has_error=has_error,
        )

        for chat_id in self.notify_chat_ids:
            msg_id = tracker.message_ids.get(chat_id)
            if is_cancelled and not msg_id:
                continue
            try:
                if msg_id:
                    await self.client.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=final_text,
                        reply_markup=markup,
                    )
                else:
                    await self.client.send_message(
                        chat_id=chat_id,
                        text=final_text,
                        reply_markup=markup,
                    )
            except TelegramAPIError as exc:
                if exc.error_code == 429 and exc.retry_after:
                    backoff_until = time.time() + float(exc.retry_after)
                    self._chat_edit_backoff_until[chat_id] = max(
                        self._chat_edit_backoff_until.get(chat_id, 0.0),
                        backoff_until,
                    )
                logger.warning("Failed to deliver final card to %s: %s", chat_id, format_exception_brief(exc))
            except Exception as exc:
                logger.warning("Failed to deliver final card to %s: %s", chat_id, format_exception_brief(exc))

        if is_cancelled:
            logger.info("Suppressed partial report/error log upload for %s/%s after /stop.", tracker.pid, tracker.tid)
            return

        if raw_html_response and auto_export_filename:
            wrapped_html = wrap_raw_html_for_mobile(
                title=f"{tracker.pid} / {tracker.tid} Report",
                raw_html_content=raw_html_source,
                metadata={
                    "model": result.model or tracker.model or "default",
                    "project": tracker.pid,
                },
            )
            for chat_id in self.notify_chat_ids:
                try:
                    await self.client.send_document(
                        chat_id=chat_id,
                        document_content=wrapped_html,
                        filename=auto_export_filename,
                        caption=f"📄 <b>{escape_html(tracker.pid)} / {escape_html(tracker.tid)} HTML Report</b>",
                    )
                except Exception as exc:
                    logger.warning("Failed to send HTML report document to %s: %s", chat_id, format_exception_brief(exc))

        if not result.success and result.output_lines:
            last_150 = "\n".join(line.rstrip("\n") for line in result.output_lines[-150:])
            html_doc = (
                "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1.0'>"
                f"<title>{escape_html(tracker.pid)} Error Log</title></head>"
                "<body style='background:#0f172a;color:#e2e8f0;padding:16px;font-family:monospace;'>"
                f"<h3>{escape_html(tracker.pid)} / {escape_html(tracker.tid)} Output Log</h3>"
                f"<pre style='overflow-x:auto;white-space:pre-wrap;'>{html.escape(last_150)}</pre>"
                "</body></html>"
            )
            for chat_id in self.notify_chat_ids:
                try:
                    await self.client.send_document(
                        chat_id=chat_id,
                        document_content=html_doc,
                        filename=f"{tracker.pid}_error_log.html",
                        caption=f"📄 <b>{escape_html(tracker.pid)}</b> recent output log ({len(result.output_lines[-150:])} lines)",
                    )
                except Exception as exc:
                    logger.warning("Failed to send error log document to %s: %s", chat_id, format_exception_brief(exc))

    async def send_notice(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: bool = False,
    ) -> None:
        """Send a standalone notice message to all configured notification chats."""
        if not self.has_targets():
            return
        assert self.client is not None
        for chat_id in self.notify_chat_ids:
            try:
                await self.client.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                )
            except Exception as exc:
                logger.warning("Failed to send notice to %s: %s", chat_id, format_exception_brief(exc))


if __name__ == "__main__":
    class _FakeClient:
        def __init__(self) -> None:
            self.sent: List[Any] = []
            self.edited: List[Any] = []
            self.docs: List[Any] = []

        async def send_message(self, **kwargs: Any) -> Dict[str, Any]:
            self.sent.append(kwargs)
            return {"message_id": len(self.sent)}

        async def edit_message_text(self, **kwargs: Any) -> Dict[str, Any]:
            self.edited.append(kwargs)
            return {"message_id": kwargs.get("message_id")}

        async def send_document(self, **kwargs: Any) -> Dict[str, Any]:
            self.docs.append(kwargs)
            return {"message_id": 99}

    async def _main() -> None:
        fc = _FakeClient()
        notifier = WorkerNotifier(client=fc, notify_chat_ids=[12345], stream_throttle_seconds=0.2)
        tracker = await notifier.start_step("DEMO", "DEMO-1", HarnessProfile())
        res = StepResult(success=True, harness="agy", model="gemini-3.8-flash", duration_seconds=1.5)
        await notifier.finish_step(tracker, res, Task(id="DEMO-1", status="complete", assignee="Admin"))
        assert len(fc.sent) == 1 and len(fc.edited) == 1
        print("telegram_ui.notifier self-test passed!")

    asyncio.run(_main())
