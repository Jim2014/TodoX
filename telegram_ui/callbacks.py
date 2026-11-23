"""Inline callback query handlers, card builders, and file delivery mixin."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from orchestrator.briefing import build_project_briefing
from orchestrator.storage import get_data_dir, load_task, update_project, update_task
from orchestrator.voice import distill_spoken_briefing

from .file_renderers import render_file_to_html
from .formatter import escape_html, wrap_raw_html_for_mobile
from .keyboards import (
    build_files_keyboard,
    build_project_briefing_card,
    build_project_card,
    build_task_card,
    build_tasks_keyboard,
)

logger = logging.getLogger(__name__)


class TelegramCallbacksMixin:
    """Implements Telegram inline keyboard callbacks and HTML file delivery."""

    async def _open_project_briefing(
        self,
        chat_id: int,
        pid: str,
        edit_message_id: Optional[int] = None,
    ) -> None:
        """Activate a project for this chat and send its cached briefing card."""
        clean_pid = pid.strip().upper()
        briefing = build_project_briefing(clean_pid)
        if not briefing:
            await self._safe_send(chat_id, f"⚠️ Project <code>{escape_html(clean_pid)}</code> not found.")
            return
        briefing["running"] = clean_pid in self._running_pids()
        briefing["paused"] = self.state_manager.is_project_paused(clean_pid)
        self._set_active_project(chat_id, clean_pid)
        text, markup = build_project_briefing_card(briefing)
        sent = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)
        self._record_card_target(chat_id, sent, clean_pid, f"{clean_pid}-0")

    async def _open_project_card(self, chat_id: int, pid: str, edit_message_id: Optional[int] = None) -> None:
        root_task = load_task(pid, f"{pid}-0")
        if not root_task:
            await self._safe_send(chat_id, f"⚠️ Project <code>{escape_html(pid)}</code> not found.")
            return
        subtasks = self._load_project_subtasks(pid)
        profile = self.router.resolve_profile(project_task=root_task)
        text, markup = build_project_card(
            pid=pid,
            root_task=root_task,
            subtasks=subtasks,
            is_running=pid in self._running_pids(),
            is_paused=self.state_manager.is_project_paused(pid),
            effective_profile=profile,
        )
        sent = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)
        self._record_card_target(chat_id, sent, pid, f"{pid}-0")

    async def _open_task_card(self, chat_id: int, pid: str, tid: str, edit_message_id: Optional[int] = None) -> None:
        root_task = load_task(pid, f"{pid}-0")
        task = load_task(pid, tid)
        if not task:
            await self._safe_send(chat_id, f"⚠️ Task <code>{escape_html(tid)}</code> not found.")
            return
        profile = self.router.resolve_profile(project_task=root_task, active_task=task)
        text, markup = build_task_card(pid=pid, task=task, effective_profile=profile)
        sent = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)
        self._record_card_target(chat_id, sent, pid, tid)

    async def handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        """Process inline keyboard callbacks with strict auth enforcement."""
        import telegram_ui.bot as _bot_mod

        cq_id = str(callback_query.get("id", ""))
        data = str(callback_query.get("data", ""))
        msg = callback_query.get("message") or {}
        chat_id = msg.get("chat", {}).get("id") or callback_query.get("from", {}).get("id")
        msg_id = msg.get("message_id")
        if not chat_id:
            return
        chat_id = int(chat_id)

        if not self.is_chat_authorized(chat_id):
            await self.client.answer_callback_query(cq_id, text="Access denied", show_alert=True)
            return

        if data == "help:show":
            await self.client.answer_callback_query(cq_id)
            await self._cmd_help(chat_id, edit_message_id=msg_id)
        elif data == "status:show":
            await self.client.answer_callback_query(cq_id, text="Refreshed")
            await self._cmd_status(chat_id, edit_message_id=msg_id)
        elif data == "projects:list":
            await self.client.answer_callback_query(cq_id)
            await self._cmd_projects(chat_id, edit_message_id=msg_id)
        elif data.startswith("proj:open:"):
            pid = data.split(":", 2)[2]
            await self.client.answer_callback_query(cq_id, text=f"Switched to {pid}")
            await self._open_project_briefing(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:card:"):
            pid = data.split(":", 2)[2]
            await self.client.answer_callback_query(cq_id)
            await self._open_project_card(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:summary:"):
            pid = data.split(":", 2)[2]
            await self.client.answer_callback_query(cq_id, text=f"Refreshing AI summary for {pid}...")
            await self._cmd_summary(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:voice:"):
            pid = data.split(":", 2)[2]
            briefing = build_project_briefing(pid)
            if not briefing:
                await self.client.answer_callback_query(cq_id, text=f"Project {pid} not found", show_alert=True)
                return
            await self.client.answer_callback_query(cq_id, text=f"Synthesizing voice briefing for {pid}...")
            spoken = distill_spoken_briefing(str(briefing.get("spoken_text") or briefing.get("summary_text") or ""))
            voice_path = await _bot_mod.synthesize_voice_ogg(spoken)
            if voice_path and voice_path.exists():
                try:
                    await self.client.send_voice(
                        chat_id=chat_id,
                        voice=voice_path,
                        caption=f"🔊 <b>Project {escape_html(pid)} Briefing</b>",
                        reply_to_message_id=msg_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to send voice brief for %s: %s", pid, exc)
            else:
                await self._safe_send(
                    chat_id,
                    f"⚠️ Voice synthesis unavailable right now for <code>{escape_html(pid)}</code>.",
                    reply_to_message_id=msg_id,
                )
        elif data.startswith("proj:tasks:"):
            pid = data.split(":", 2)[2]
            await self.client.answer_callback_query(cq_id)
            subtasks = self._load_project_subtasks(pid)
            text, markup = build_tasks_keyboard(pid, subtasks)
            await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=msg_id)
        elif data.startswith("proj:run:"):
            pid = data.split(":", 2)[2]
            update_project(pid, status="in_progress", assignee="AI")
            self.state_manager.resume_project(pid)
            if self.watcher is not None:
                asyncio.create_task(self.watcher.trigger_project(pid))
            await self.client.answer_callback_query(cq_id, text=f"Assigned {pid} to AI")
            await self._open_project_card(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:pause:"):
            pid = data.split(":", 2)[2]
            self.state_manager.pause_project(pid)
            await self.client.answer_callback_query(cq_id, text=f"Paused {pid}")
            await self._open_project_card(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:resume:"):
            pid = data.split(":", 2)[2]
            self.state_manager.resume_project(pid)
            await self.client.answer_callback_query(cq_id, text=f"Resumed {pid}")
            await self._open_project_card(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("proj:html:"):
            pid = data.split(":", 2)[2]
            await self.client.answer_callback_query(cq_id, text="Generating HTML report...")
            from orchestrator.html_generator import generate_project_html

            html_bytes = generate_project_html(pid)
            if html_bytes:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await self.client.send_document(
                    chat_id=chat_id,
                    document_content=html_bytes,
                    filename=f"project_{pid}_{ts}.html",
                    caption=f"📄 <b>Project {escape_html(pid)} Report</b>",
                )
            else:
                await self._safe_send(chat_id, f"⚠️ Project <code>{escape_html(pid)}</code> not found.")
        elif data.startswith("proj:files:"):
            parts = data.split(":", 3)
            pid = parts[2]
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await self.client.answer_callback_query(cq_id)
            rel_files = self._scan_project_files(pid)
            text, markup = build_files_keyboard(pid, rel_files, page=page)
            await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=msg_id)
        elif data.startswith("file:get:"):
            parts = data.split(":", 3)
            pid = parts[2]
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
            files = self._file_lists_cache.get(pid) or self._scan_project_files(pid)
            if 0 <= idx < len(files):
                rel = files[idx]
                await self.client.answer_callback_query(cq_id, text=f"Sending {Path(rel).name}")
                await self._send_project_file(chat_id, pid, rel)
            else:
                await self.client.answer_callback_query(cq_id, text="File index expired, refreshing...", show_alert=False)
                rel_files = self._scan_project_files(pid)
                text, markup = build_files_keyboard(pid, rel_files, page=0)
                await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=msg_id)
        elif data.startswith("task:open:"):
            _, _, pid, tid = data.split(":", 3)
            await self.client.answer_callback_query(cq_id)
            await self._open_task_card(chat_id, pid, tid, edit_message_id=msg_id)
        elif data.startswith("task:ai:"):
            _, _, pid, tid = data.split(":", 3)
            update_task(pid, tid, status="in_progress", assignee="AI")
            self.state_manager.resume_project(pid)
            if self.watcher is not None:
                asyncio.create_task(self.watcher.trigger_project(pid))
            await self.client.answer_callback_query(cq_id, text=f"Handed {tid} to AI")
            await self._open_task_card(chat_id, pid, tid, edit_message_id=msg_id)
        elif data.startswith("task:admin:"):
            _, _, pid, tid = data.split(":", 3)
            update_task(pid, tid, assignee="Admin")
            await self.client.answer_callback_query(cq_id, text=f"Assigned {tid} to Admin")
            await self._open_task_card(chat_id, pid, tid, edit_message_id=msg_id)
        elif data.startswith("task:done:"):
            _, _, pid, tid = data.split(":", 3)
            update_task(pid, tid, status="complete", assignee="Admin")
            await self.client.answer_callback_query(cq_id, text=f"Marked {tid} complete")
            await self._open_task_card(chat_id, pid, tid, edit_message_id=msg_id)
        elif data.startswith("task:approve:"):
            _, _, pid, tid = data.split(":", 3)
            update_task(pid, tid, status="complete", assignee="Admin")
            await self.client.answer_callback_query(cq_id, text=f"Approved {tid}")
            await self._open_project_briefing(chat_id, pid, edit_message_id=msg_id)
        elif data.startswith("comment:prompt:"):
            _, _, pid, tid = data.split(":", 3)
            self._pending_comment_target[chat_id] = (pid, tid)
            await self.client.answer_callback_query(cq_id)
            await self._safe_send(
                chat_id,
                f"💬 Send your next message to add an Admin comment to <code>{escape_html(tid)}</code> "
                "(or send /cancel to abort).",
            )
        elif data in ("harness:show", "harness:refresh"):
            await self.client.answer_callback_query(cq_id)
            await self._cmd_harness(chat_id, edit_message_id=msg_id)
        elif data.startswith("harness:set:"):
            h_name = data.split(":", 2)[2]
            if self.registry.has(h_name):
                self.state_manager.set_harness(h_name)
                await self.client.answer_callback_query(cq_id, text=f"Switched harness to {h_name}")
            else:
                await self.client.answer_callback_query(cq_id, text=f"Unknown harness {h_name}", show_alert=True)
            await self._cmd_harness(chat_id, edit_message_id=msg_id)
        elif data in ("model:show", "model:refresh"):
            await self.client.answer_callback_query(cq_id)
            await self._cmd_model(chat_id, edit_message_id=msg_id, force_refresh=(data == "model:refresh"))
        elif data.startswith("model:idx:"):
            idx = int(data.split(":", 2)[2])
            ok = self.state_manager.set_model_index(idx)
            await self.client.answer_callback_query(cq_id, text="Model updated" if ok else "Invalid index")
            await self._cmd_model(chat_id, edit_message_id=msg_id)
        elif data.startswith("model:id:"):
            m_id = data.split(":", 2)[2]
            cur_h = self.state_manager.get_current_harness()
            self.state_manager.set_harness(cur_h, m_id)
            await self.client.answer_callback_query(cq_id, text=f"Model set to {m_id}")
            await self._cmd_model(chat_id, edit_message_id=msg_id)
        elif data == "effort:show":
            await self.client.answer_callback_query(cq_id)
            await self._cmd_effort(chat_id, edit_message_id=msg_id)
        elif data.startswith("effort:set:"):
            eff = data.split(":", 2)[2]
            self.state_manager.set_effort(eff)
            await self.client.answer_callback_query(cq_id, text=f"Effort set to {eff}")
            await self._cmd_effort(chat_id, edit_message_id=msg_id)
        elif data == "perm:show":
            await self.client.answer_callback_query(cq_id)
            await self._cmd_permissions(chat_id, edit_message_id=msg_id)
        elif data.startswith("perm:set:"):
            preset = data.split(":", 2)[2]
            norm = self.state_manager.set_permission_preset(preset)
            await self.client.answer_callback_query(cq_id, text=f"Permission preset: {norm}")
            await self._cmd_permissions(chat_id, edit_message_id=msg_id)
        elif data == "watcher:pause":
            self.state_manager.set_watcher_paused(True)
            await self.client.answer_callback_query(cq_id, text="Watcher paused")
            await self._cmd_status(chat_id, edit_message_id=msg_id)
        elif data == "watcher:resume":
            self.state_manager.set_watcher_paused(False)
            await self.client.answer_callback_query(cq_id, text="Watcher resumed")
            await self._cmd_status(chat_id, edit_message_id=msg_id)
        elif data.startswith("worker:stop:"):
            pid = data.split(":", 2)[2]
            stopped = bool(self.watcher and await self.watcher.stop_project(pid))
            await self.client.answer_callback_query(
                cq_id,
                text=f"Stopped {pid}" if stopped else f"{pid} is not running",
            )
        elif data.startswith("worker:log:"):
            await self.client.answer_callback_query(cq_id)
            await self._cmd_output(chat_id, "40")
        elif data == "fixdata:run":
            await self.client.answer_callback_query(cq_id, text="Running JSON validation...")
            await self._cmd_fix_data(chat_id)
        else:
            await self.client.answer_callback_query(cq_id, text="Unsupported action")

    async def _send_project_file(self, chat_id: int, pid: str, rel_path: str) -> None:
        proj_dir = (Path(get_data_dir()) / "projects" / pid).resolve()
        target = (proj_dir / rel_path).resolve()
        if not target.is_relative_to(proj_dir) or not target.is_file():
            await self._safe_send(chat_id, f"⚠️ File <code>{escape_html(rel_path)}</code> not found in {escape_html(pid)}.")
            return

        await self.client.send_chat_action(chat_id, "upload_document")
        if target.suffix.lower() in (".html", ".htm"):
            raw_html = target.read_text(encoding="utf-8", errors="replace")
            low = raw_html.lower()
            if "<!doctype html" not in low and "<html" not in low:
                raw_html = wrap_raw_html_for_mobile(title=target.name, raw_html_content=raw_html)
            await self.client.send_document(
                chat_id=chat_id,
                document_content=raw_html.encode("utf-8"),
                filename=target.name,
                caption=f"🌐 <code>{escape_html(rel_path)}</code>",
            )
            return

        rendered_html = render_file_to_html(target, rel_path=rel_path, pid=pid)
        if rendered_html is not None:
            await self.client.send_document(
                chat_id=chat_id,
                document_content=rendered_html,
                filename=f"{target.name}.html",
                caption=f"📄 <code>{escape_html(rel_path)}</code>",
            )
        else:
            await self.client.send_document(chat_id=chat_id, document_content=target, filename=target.name)


if __name__ == "__main__":
    assert hasattr(TelegramCallbacksMixin, "handle_callback_query")
    print("telegram_ui.callbacks self-test passed!")
