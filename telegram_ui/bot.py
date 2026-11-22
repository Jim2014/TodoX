"""Async Telegram bot orchestrator for TodoX with interactive controls and VOP compatibility."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import platform
import shlex
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from harness import (
    HarnessProfile,
    HarnessRouter,
    RunRequest,
    create_default_registry,
    format_event_log_line,
)
from orchestrator.briefing import build_project_briefing, generate_ai_project_briefing
from orchestrator.chat_router import handle_project_chat
from orchestrator.models import Task
from orchestrator.state import SystemState, state as default_state
from orchestrator.storage import (
    add_comment,
    create_project,
    create_task,
    delete_comment,
    delete_project,
    delete_task,
    get_data_dir,
    get_default_worker_prompt,
    load_projects,
    load_task,
    update_comment,
    update_project,
    update_task,
)
from orchestrator.voice import distill_spoken_briefing, synthesize_voice_ogg, transcribe_voice

from .client import TelegramAPIError, TelegramClient, format_exception_brief
from .file_renderers import render_file_to_html
from .formatter import escape_html, format_pinned_dashboard, markdown_to_telegram_html, wrap_raw_html_for_mobile
from .keyboards import (
    BOT_COMMANDS,
    build_effort_keyboard,
    build_files_keyboard,
    build_harness_keyboard,
    build_help_keyboard,
    build_help_text,
    build_model_keyboard,
    build_permissions_keyboard,
    build_pinned_banner_keyboard,
    build_project_briefing_card,
    build_project_card,
    build_projects_keyboard,
    build_task_card,
    build_tasks_keyboard,
)
from .notifier import WorkerNotifier

logger = logging.getLogger(__name__)


def parse_allowed_chat_ids(allowed_raw: str = "", admin_raw: str = "") -> Set[int]:
    """Parse authorized Telegram chat IDs from ALLOWED_CHAT_IDS and ADMIN_CHAT_ID."""
    result: Set[int] = set()
    for raw in (allowed_raw, admin_raw):
        if not raw:
            continue
        for token in raw.split(","):
            cleaned = token.strip()
            if cleaned:
                try:
                    result.add(int(cleaned))
                except ValueError:
                    pass
    return result


def parse_vop_args(arg_list: Sequence[str]) -> Dict[str, Any]:
    """Parse POSIX-style '-key value' tokens used by legacy VOP commands."""
    parsed: Dict[str, Any] = {}
    idx = 0
    while idx < len(arg_list):
        token = arg_list[idx]
        if token.startswith("-"):
            key = token[1:]
            if idx + 1 < len(arg_list) and not arg_list[idx + 1].startswith("-"):
                parsed[key] = arg_list[idx + 1]
                idx += 2
            else:
                parsed[key] = True
                idx += 1
        else:
            idx += 1
    return parsed


class TodoXTelegramBot:
    """Async Telegram C&C interface supporting inline cards, live worker streaming, and VOP batch mode."""

    def __init__(
        self,
        bot_token: str,
        allowed_chat_ids: Optional[Set[int]] = None,
        allow_all_chats: bool = False,
        registry: Any = None,
        router: Optional[HarnessRouter] = None,
        watcher: Any = None,
        state_manager: Optional[SystemState] = None,
        client: Optional[TelegramClient] = None,
        notifier: Optional[WorkerNotifier] = None,
        stream_throttle_seconds: float = 1.5,
    ) -> None:
        self.bot_token = bot_token
        self.allowed_chat_ids: Set[int] = set(allowed_chat_ids or ())
        self.allow_all_chats = bool(allow_all_chats)
        self.state_manager = state_manager or default_state
        self.registry = registry or create_default_registry()
        self.router = router or HarnessRouter(registry=self.registry, state_manager=self.state_manager)
        self.watcher = watcher
        self.client = client or TelegramClient(bot_token=bot_token)
        self.notifier = notifier or WorkerNotifier(
            client=self.client,
            notify_chat_ids=sorted(self.allowed_chat_ids),
            stream_throttle_seconds=stream_throttle_seconds,
        )

        self.running = False
        self.bot_info: Dict[str, Any] = {}
        self.pinned_message_ids: Dict[int, int] = {}
        self._pending_comment_target: Dict[int, Tuple[str, str]] = {}
        self._card_targets: Dict[Tuple[int, int], Tuple[str, str]] = {}
        self._file_lists_cache: Dict[str, List[str]] = {}
        self._chat_edit_backoff_until: Dict[int, float] = {}
        self._active_project_by_chat: Dict[int, str] = self._load_chat_state()
        self._intent_classifier: Optional[Any] = None
        self._vop_handlers: Dict[str, Any] = {
            "/new-project": self._vop_new_project,
            "/new-task": self._vop_new_task,
            "/new-comment": self._vop_new_comment,
            "/update-project": self._vop_update_project,
            "/update-task": self._vop_update_task,
            "/update-comment": self._vop_update_comment,
            "/del-project": self._vop_del_project,
            "/del-task": self._vop_del_task,
            "/del-comment": self._vop_del_comment,
            "/get-project": self._vop_get_project,
            "/get-task": self._vop_get_task,
            "/get-file": self._vop_get_file,
            "/fix-data": self._vop_fix_data,
        }

    def is_token_valid(self) -> bool:
        """Validate Telegram bot token format and reject placeholders."""
        if not self.bot_token:
            return False
        if self.bot_token.lower() in ("your_telegram_bot_token_here", "placeholder", "none"):
            return False
        parts = self.bot_token.split(":", 1)
        return len(parts) == 2 and parts[0].isdigit() and len(parts[1]) >= 10

    def is_chat_authorized(self, chat_id: int) -> bool:
        """Enforce deny-by-default authorization unless ALLOW_ALL_CHATS is explicitly enabled."""
        if self.allow_all_chats:
            return True
        return int(chat_id) in self.allowed_chat_ids

    def _record_card_target(self, chat_id: int, msg: Optional[Dict[str, Any]], pid: str, tid: str) -> None:
        """Associate a sent/edited card message_id with (pid, tid) for direct reply-to-comment UX."""
        if not isinstance(msg, dict) or not msg.get("message_id"):
            return
        self._card_targets[(chat_id, int(msg["message_id"]))] = (pid, tid)
        if len(self._card_targets) > 250:
            oldest_key = next(iter(self._card_targets))
            self._card_targets.pop(oldest_key, None)

    async def _send_or_edit(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        edit_message_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Edit an existing bot message or send a new message safely."""
        try:
            if edit_message_id:
                await self.client.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                return {"message_id": edit_message_id}
            return await self.client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        except asyncio.CancelledError:
            raise
        except TelegramAPIError as exc:
            if exc.error_code == 429 and exc.retry_after:
                backoff_until = time.time() + float(exc.retry_after)
                self._chat_edit_backoff_until[chat_id] = max(
                    self._chat_edit_backoff_until.get(chat_id, 0.0),
                    backoff_until,
                )
            logger.warning("Telegram delivery error for chat %s: %s", chat_id, format_exception_brief(exc))
            return None
        except Exception as exc:
            logger.warning("Telegram delivery error for chat %s: %s", chat_id, format_exception_brief(exc))
            return None

    async def _safe_send(self, chat_id: int, text: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            return await self.client.send_message(chat_id=chat_id, text=text, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram send_message failed for chat %s: %s", chat_id, format_exception_brief(exc))
            return None

    def _load_project_subtasks(self, pid: str) -> List[Task]:
        tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
        subtasks: List[Task] = []
        if not os.path.isdir(tasks_dir):
            return subtasks
        for tid in sorted(os.listdir(tasks_dir)):
            if tid.startswith("."):
                continue
            task = load_task(pid, tid)
            if task:
                subtasks.append(task)
        return subtasks

    def _scan_project_files(self, pid: str) -> List[str]:
        project_dir = Path(get_data_dir()) / "projects" / pid
        rel_files: List[str] = []
        if project_dir.is_dir():
            for path in sorted(project_dir.rglob("*")):
                if path.is_file() and path.name != ".DS_Store" and ".git" not in path.parts:
                    rel_files.append(path.relative_to(project_dir).as_posix())
        self._file_lists_cache[pid] = rel_files
        return rel_files

    def _running_pids(self) -> List[str]:
        if self.watcher is not None and hasattr(self.watcher, "running_pids"):
            return list(self.watcher.running_pids())
        return []

    def _render_dashboard_payload(self) -> Tuple[str, Dict[str, Any]]:
        st = self.state_manager.get_state()
        projects = load_projects()
        paused_global = self.state_manager.is_watcher_paused()
        paused_projects = self.state_manager.get_paused_projects()
        text = format_pinned_dashboard(
            state_dict=st,
            running_pids=self._running_pids(),
            paused_global=paused_global,
            paused_projects=paused_projects,
            project_count=len(projects),
        )
        return text, build_pinned_banner_keyboard(paused_global)

    async def update_pinned_banner(self, chat_id: int, force_repin: bool = False) -> Optional[int]:
        """Update or manually pin the live TodoX orchestrator dashboard banner."""
        text, markup = self._render_dashboard_payload()
        existing_id = self.pinned_message_ids.get(chat_id)
        if existing_id and not force_repin:
            res = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=existing_id)
            if res:
                return existing_id

        if existing_id and force_repin:
            await self.client.unpin_chat_message(chat_id=chat_id, message_id=existing_id)

        sent = await self._safe_send(chat_id, text, reply_markup=markup, disable_notification=True)
        if sent and sent.get("message_id"):
            msg_id = int(sent["message_id"])
            self.pinned_message_ids[chat_id] = msg_id
            await self.client.pin_chat_message(chat_id=chat_id, message_id=msg_id, disable_notification=True)
            return msg_id
        return None

    async def setup_bot_commands(self) -> bool:
        """Register English slash commands menu with Telegram."""
        ok = await self.client.set_my_commands(BOT_COMMANDS)
        await self.client.set_chat_menu_button(menu_button={"type": "commands"})
        return ok

    async def _notify_startup(self) -> None:
        """Send concise startup greeting without auto-pinning messages."""
        for chat_id in sorted(self.allowed_chat_ids):
            st = self.state_manager.get_state()
            active_pid = self._get_active_project(chat_id)
            active_line = (
                f"\n• <b>Active Project:</b> <code>{escape_html(active_pid)}</code>"
                if active_pid
                else ""
            )
            await self._safe_send(
                chat_id,
                "🚀 <b>TodoX Orchestrator Online</b>\n"
                f"• <b>Harness:</b> <code>{escape_html(st['default_harness'])}</code> "
                f"(<code>{escape_html(st['default_model'])}</code>)\n"
                f"• <b>Permissions:</b> <code>{escape_html(st['default_permission_preset'])}</code>"
                f"{active_line}\n"
                "Use /projects or /use &lt;PID&gt; to switch projects and chat/speak directly.",
            )

    async def start(self) -> None:
        """Run the async Telegram long-polling loop with 409 backoff."""
        if not self.is_token_valid():
            print("Telegram token not set or invalid. Bot loop will not start.")
            return

        try:
            self.bot_info = await self.client.get_me()
            username = self.bot_info.get("username", "TodoXBot")
            print(f"🤖 Connected to Telegram as @{username} (ID: {self.bot_info.get('id')})")
            await self.setup_bot_commands()
        except Exception as exc:
            logger.error("Failed to authenticate Telegram bot: %s", format_exception_brief(exc))
            return

        self.running = True
        await self._notify_startup()

        offset: Optional[int] = None
        consecutive_errors = 0

        try:
            while self.running:
                try:
                    updates = await self.client.get_updates(offset=offset, timeout=30, raise_on_error=True)
                    consecutive_errors = 0
                    for update in updates:
                        uid = update.get("update_id")
                        if uid is not None:
                            offset = int(uid) + 1
                        task = asyncio.create_task(self.process_update(update))
                        task.add_done_callback(
                            lambda t: logger.error("Update task error: %s", t.exception())
                            if not t.cancelled() and t.exception()
                            else None
                        )
                except asyncio.CancelledError:
                    break
                except TelegramAPIError as exc:
                    consecutive_errors += 1
                    wait_s = min(5 * consecutive_errors, 60) if exc.error_code == 409 else min(2**consecutive_errors, 30)
                    logger.warning("Telegram API error in getUpdates (attempt %d): %s", consecutive_errors, exc)
                    await asyncio.sleep(wait_s)
                except Exception as exc:
                    consecutive_errors += 1
                    wait_s = min(2**consecutive_errors, 30)
                    logger.warning("getUpdates error (attempt %d): %s", consecutive_errors, format_exception_brief(exc))
                    await asyncio.sleep(wait_s)
        finally:
            await self.client.close()

    async def stop(self) -> None:
        """Stop the polling loop and close the HTTP client."""
        self.running = False
        await self.client.close()

    def _chat_state_path(self) -> Path:
        """Return path for persisting active project selection per Telegram chat."""
        return Path(get_data_dir()) / "telegram_chat_state.json"

    def _load_chat_state(self) -> Dict[int, str]:
        """Load persisted active project mapping {chat_id: pid}."""
        path = self._chat_state_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                result: Dict[int, str] = {}
                for k, v in raw.items():
                    if v:
                        try:
                            result[int(k)] = str(v).strip().upper()
                        except (TypeError, ValueError):
                            pass
                return result
        except Exception as exc:
            logger.warning("Failed to load Telegram chat state: %s", exc)
        return {}

    def _save_chat_state(self) -> None:
        """Persist active project mapping atomically."""
        path = self._chat_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            payload = {str(k): v for k, v in self._active_project_by_chat.items()}
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Failed to save Telegram chat state: %s", exc)

    def _get_active_project(self, chat_id: int) -> Optional[str]:
        """Return the active project ID for chat_id if it still exists."""
        cid = int(chat_id)
        pid = self._active_project_by_chat.get(cid)
        if not pid:
            return None
        if load_task(pid, f"{pid}-0") is None:
            self._active_project_by_chat.pop(cid, None)
            self._save_chat_state()
            return None
        return pid

    def _set_active_project(self, chat_id: int, pid: Optional[str]) -> None:
        """Persist or clear the active project ID for chat_id."""
        cid = int(chat_id)
        if not pid:
            self._active_project_by_chat.pop(cid, None)
        else:
            self._active_project_by_chat[cid] = str(pid).strip().upper()
        self._save_chat_state()

    async def _send_project_picker(
        self,
        chat_id: int,
        reply_to_message_id: Optional[int] = None,
        notice: str = "",
    ) -> None:
        """Send interactive project list so the user can choose an active workspace."""
        projects = load_projects()
        text, markup = build_projects_keyboard(
            projects=projects,
            running_pids=self._running_pids(),
            paused_projects=self.state_manager.get_paused_projects(),
        )
        prefix = f"{notice}\n\n" if notice else ""
        hint = "Tap a project below or send <code>/use &lt;PID&gt;</code> to start chatting or sending voice notes.\n\n"
        await self._safe_send(
            chat_id,
            f"{prefix}{hint}{text}",
            reply_markup=markup,
            reply_to_message_id=reply_to_message_id,
        )

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

    async def _cmd_use(
        self,
        chat_id: int,
        arg: str = "",
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Switch the active project for natural-language text and voice messages."""
        raw = arg.strip()
        if not raw:
            cur = self._get_active_project(chat_id)
            notice = f"📌 Current active project: <code>{escape_html(cur)}</code>" if cur else "📌 Select an active project:"
            await self._send_project_picker(chat_id, reply_to_message_id=reply_to_message_id, notice=notice)
            return
        token = raw.split()[0].upper()
        if token in ("NONE", "CLEAR", "OFF"):
            self._set_active_project(chat_id, None)
            await self._safe_send(
                chat_id,
                "✅ Cleared active project selection for this chat.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        if load_task(token, f"{token}-0") is None:
            await self._send_project_picker(
                chat_id,
                reply_to_message_id=reply_to_message_id,
                notice=f"⚠️ Project <code>{escape_html(token)}</code> was not found.",
            )
            return
        await self._open_project_briefing(chat_id, token)

    async def _cmd_summary(
        self,
        chat_id: int,
        arg: str = "",
        reply_to_message_id: Optional[int] = None,
        edit_message_id: Optional[int] = None,
    ) -> None:
        """Generate a fresh read-only AI executive summary for the target or active project."""
        clean_pid = arg.strip().split()[0].upper() if arg.strip() else (self._get_active_project(chat_id) or "")
        if not clean_pid:
            await self._send_project_picker(
                chat_id,
                reply_to_message_id=reply_to_message_id,
                notice="📌 Select a project first to generate an AI summary.",
            )
            return
        if load_task(clean_pid, f"{clean_pid}-0") is None:
            await self._safe_send(
                chat_id,
                f"⚠️ Project <code>{escape_html(clean_pid)}</code> not found.",
                reply_to_message_id=reply_to_message_id,
            )
            return

        target_edit_id = edit_message_id
        if target_edit_id is None:
            wait_msg = await self._safe_send(
                chat_id,
                f"✨ Generating executive briefing for <code>{escape_html(clean_pid)}</code>...",
                reply_to_message_id=reply_to_message_id,
            )
            if isinstance(wait_msg, dict) and wait_msg.get("message_id"):
                target_edit_id = int(wait_msg["message_id"])

        briefing = await generate_ai_project_briefing(clean_pid, router=self.router)
        if not briefing:
            await self._safe_send(
                chat_id,
                f"⚠️ Failed to build briefing for <code>{escape_html(clean_pid)}</code>.",
                reply_to_message_id=reply_to_message_id,
            )
            return
        briefing["running"] = clean_pid in self._running_pids()
        briefing["paused"] = self.state_manager.is_project_paused(clean_pid)
        self._set_active_project(chat_id, clean_pid)
        text, markup = build_project_briefing_card(briefing)
        sent = await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=target_edit_id)
        self._record_card_target(chat_id, sent, clean_pid, f"{clean_pid}-0")

    async def _handle_active_project_chat(
        self,
        chat_id: int,
        pid: str,
        text: str,
        message_id: Optional[int] = None,
        transcript: Optional[str] = None,
        reply_with_voice: bool = False,
    ) -> None:
        """Route a plain text or transcribed voice message through the shared project chat router."""
        if message_id:
            await self.client.set_message_reaction(chat_id, int(message_id), "👀")

        res = await handle_project_chat(
            pid=pid,
            message=text,
            source="telegram_voice" if reply_with_voice else "telegram",
            auto_run=True,
            router=self.router,
            watcher=self.watcher,
            state_manager=self.state_manager,
            intent_classifier=self._intent_classifier,
        )

        parts: List[str] = []
        if transcript:
            parts.append(f"🎙️ <i>\"{escape_html(transcript)}\"</i>")
        parts.append(markdown_to_telegram_html(str(res.get("reply") or "Done.")))
        reply_html = "\n\n".join(parts)

        buttons: List[List[Dict[str, str]]] = []
        if res.get("tid"):
            tid = str(res["tid"])
            buttons.append(
                [{"text": f"📋 Open {tid}", "callback_data": f"task:open:{pid}:{tid}"[:64]}]
            )
        row_nav = [
            {"text": f"📌 {pid} Briefing", "callback_data": f"proj:open:{pid}"[:64]},
            {"text": "✨ AI Summary", "callback_data": f"proj:summary:{pid}"[:64]},
        ]
        if not reply_with_voice:
            row_nav.append({"text": "🔊 Voice Brief", "callback_data": f"proj:voice:{pid}"[:64]})
        buttons.append(row_nav)

        sent = await self._safe_send(
            chat_id,
            reply_html,
            reply_to_message_id=message_id,
            reply_markup={"inline_keyboard": buttons},
        )
        self._record_card_target(chat_id, sent, pid, str(res.get("tid") or f"{pid}-0"))
        if message_id:
            await self.client.set_message_reaction(chat_id, int(message_id), "✅")

        if reply_with_voice:
            spoken = distill_spoken_briefing(str(res.get("spoken_reply") or res.get("reply") or ""))
            if spoken:
                voice_path = await synthesize_voice_ogg(spoken)
                if voice_path and voice_path.exists():
                    try:
                        await self.client.send_voice(
                            chat_id=chat_id,
                            voice=voice_path,
                            reply_to_message_id=message_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to send voice reply for chat %s: %s", chat_id, exc)

    async def _handle_voice_message(
        self,
        chat_id: int,
        message: Dict[str, Any],
        message_id: Optional[int] = None,
    ) -> None:
        """Download, transcribe, and route a Telegram voice or audio message."""
        voice_obj = message.get("voice") or message.get("audio") or {}
        file_id = str(voice_obj.get("file_id") or "").strip()
        if not file_id:
            return

        if message_id:
            await self.client.set_message_reaction(chat_id, int(message_id), "👀")

        incoming_dir = Path(get_data_dir()) / "voice_cache" / "incoming"
        local_audio: Optional[Path] = None
        try:
            local_audio, _ = await self.client.download_file_by_id(file_id, incoming_dir, filename_prefix="tg_voice")
            transcript, _lang = await transcribe_voice(local_audio)
        except Exception as exc:
            await self._safe_send(
                chat_id,
                f"⚠️ Voice transcription failed: <code>{escape_html(format_exception_brief(exc))}</code>",
                reply_to_message_id=message_id,
            )
            return
        finally:
            if local_audio and local_audio.exists():
                try:
                    local_audio.unlink()
                except OSError:
                    pass

        cleaned = (transcript or "").strip()
        if not cleaned:
            await self._safe_send(
                chat_id,
                "🤔 No clear speech was detected in that voice note. Please try again.",
                reply_to_message_id=message_id,
            )
            return

        active_pid = self._get_active_project(chat_id)
        if not active_pid:
            await self._send_project_picker(
                chat_id,
                reply_to_message_id=message_id,
                notice=f"🎙️ I heard: <i>\"{escape_html(cleaned)}\"</i>\n\nPlease select an active project first:",
            )
            return

        await self._handle_active_project_chat(
            chat_id=chat_id,
            pid=active_pid,
            text=cleaned,
            message_id=message_id,
            transcript=cleaned,
            reply_with_voice=True,
        )

    async def process_update(self, update: Dict[str, Any]) -> None:
        """Authenticate and route one Telegram update dict."""
        if "callback_query" in update:
            await self.handle_callback_query(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return

        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        if not chat_id:
            return
        chat_id = int(chat_id)

        if not self.is_chat_authorized(chat_id):
            logger.warning("Rejected unauthorized Telegram message from chat_id=%s", chat_id)
            await self._safe_send(
                chat_id,
                "⛔ <b>Access Denied</b>\n"
                f"Your Chat ID (<code>{chat_id}</code>) is not authorized in <code>ALLOWED_CHAT_IDS</code>.",
                reply_to_message_id=message_id,
            )
            return

        voice_obj = message.get("voice") or message.get("audio")
        if isinstance(voice_obj, dict) and voice_obj.get("file_id"):
            await self._handle_voice_message(chat_id, message, message_id)
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/"):
            if self._is_vop_batch_text(text):
                await self._handle_vop_lines(chat_id, text, message_id)
                return

            parts = text.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1].strip() if len(parts) > 1 else ""
            await self.dispatch_slash_command(chat_id, cmd, arg, message_id)
            return

        # Plain-text message: check reply-to-card or pending comment prompt
        target: Optional[Tuple[str, str]] = None
        reply_msg = message.get("reply_to_message")
        if isinstance(reply_msg, dict) and reply_msg.get("message_id"):
            target = self._card_targets.get((chat_id, int(reply_msg["message_id"])))
        if target is None:
            target = self._pending_comment_target.pop(chat_id, None)

        if target is not None:
            pid, tid = target
            if message_id:
                await self.client.set_message_reaction(chat_id, int(message_id), "👀")
            updated = add_comment(pid, tid, "Admin", text)
            if updated:
                if message_id:
                    await self.client.set_message_reaction(chat_id, int(message_id), "✅")
                sent = await self._safe_send(
                    chat_id,
                    f"💬 <b>Comment Added to {escape_html(tid)}</b>\n\n"
                    f"<i>{escape_html(text[:300])}</i>",
                    reply_to_message_id=message_id,
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {"text": f"🤖 Assign AI to {tid}", "callback_data": f"task:ai:{pid}:{tid}"[:64]},
                                {"text": f"📋 View {tid}", "callback_data": f"task:open:{pid}:{tid}"[:64]},
                            ]
                        ]
                    },
                )
                self._record_card_target(chat_id, sent, pid, tid)
            else:
                await self._safe_send(
                    chat_id,
                    f"⚠️ Task <code>{escape_html(tid)}</code> no longer exists.",
                    reply_to_message_id=message_id,
                )
            return

        active_pid = self._get_active_project(chat_id)
        if active_pid:
            await self._handle_active_project_chat(
                chat_id=chat_id,
                pid=active_pid,
                text=text,
                message_id=message_id,
                reply_with_voice=False,
            )
            return

        await self._send_project_picker(
            chat_id,
            reply_to_message_id=message_id,
            notice="📌 No active project selected for conversational chat.",
        )

    def _is_vop_batch_text(self, text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith("/")]
        if not lines:
            return False
        first_cmd = lines[0].split(maxsplit=1)[0].split("@")[0].lower()
        return first_cmd in self._vop_handlers or len(lines) > 1

    async def dispatch_slash_command(
        self,
        chat_id: int,
        cmd: str,
        arg: str = "",
        message_id: Optional[int] = None,
    ) -> None:
        """Route interactive slash commands."""
        clean = cmd.lower().split("@")[0].lstrip("/")
        if clean in ("start", "help"):
            await self._cmd_help(chat_id)
        elif clean == "projects":
            await self._cmd_projects(chat_id)
        elif clean == "use":
            await self._cmd_use(chat_id, arg, reply_to_message_id=message_id)
        elif clean == "summary":
            await self._cmd_summary(chat_id, arg, reply_to_message_id=message_id)
        elif clean == "status":
            await self._cmd_status(chat_id)
        elif clean == "pin":
            await self.update_pinned_banner(chat_id, force_repin=True)
        elif clean == "unpin":
            ok = await self.client.unpin_all_chat_messages(chat_id)
            self.pinned_message_ids.pop(chat_id, None)
            await self._safe_send(
                chat_id,
                "✅ Unpinned all pinned messages in this chat." if ok else "⚠️ Could not unpin messages in this chat.",
                reply_to_message_id=message_id,
            )
        elif clean == "harness":
            await self._cmd_harness(chat_id, arg)
        elif clean == "model":
            await self._cmd_model(chat_id, arg)
        elif clean == "effort":
            await self._cmd_effort(chat_id, arg)
        elif clean in ("permissions", "perm", "perms"):
            await self._cmd_permissions(chat_id, arg)
        elif clean == "pause":
            await self._cmd_pause(chat_id, arg)
        elif clean == "resume":
            await self._cmd_resume(chat_id, arg)
        elif clean == "stop":
            await self._cmd_stop(chat_id, arg)
        elif clean == "steer":
            await self._cmd_steer(chat_id, arg, message_id)
        elif clean == "output":
            await self._cmd_output(chat_id, arg)
        elif clean in ("fix_data", "fix-data"):
            await self._cmd_fix_data(chat_id)
        elif clean == "sys":
            await self._cmd_sys(chat_id)
        elif clean == "upmodel":
            await self._cmd_upmodel(chat_id, arg)
        elif clean == "cancel":
            self._pending_comment_target.pop(chat_id, None)
            await self._safe_send(chat_id, "✅ Cleared any pending comment input.", reply_to_message_id=message_id)
        elif clean == "restart":
            await self._cmd_restart(chat_id)
        else:
            await self._safe_send(
                chat_id,
                f"❓ Unknown command <code>/{escape_html(clean)}</code>. Send /help to see all commands.",
                reply_to_message_id=message_id,
            )

    async def _cmd_help(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        await self._send_or_edit(
            chat_id,
            build_help_text(),
            reply_markup=build_help_keyboard(),
            edit_message_id=edit_message_id,
        )

    async def _cmd_projects(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        projects = load_projects()
        text, markup = build_projects_keyboard(
            projects=projects,
            running_pids=self._running_pids(),
            paused_projects=self.state_manager.get_paused_projects(),
        )
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_status(self, chat_id: int, edit_message_id: Optional[int] = None) -> None:
        text, markup = self._render_dashboard_payload()
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_harness(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        target = arg.strip().lower()
        if target:
            if not self.registry.has(target):
                await self._safe_send(
                    chat_id,
                    f"⚠️ Unknown harness <code>{escape_html(target)}</code>. "
                    f"Available: {', '.join(self.registry.list_names())}",
                )
                return
            self.state_manager.set_harness(target)

        health_map = await self.registry.check_all_health()
        st = self.state_manager.get_state()
        text, markup = build_harness_keyboard(health_map, st["default_harness"], st["default_model"])
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_model(
        self,
        chat_id: int,
        arg: str = "",
        edit_message_id: Optional[int] = None,
        force_refresh: bool = False,
    ) -> None:
        st = self.state_manager.get_state()
        cur_harness = st["default_harness"]
        if arg.strip():
            val = arg.strip()
            if val.isdigit():
                self.state_manager.set_model_index(int(val))
            else:
                self.state_manager.set_harness(cur_harness, val)
            st = self.state_manager.get_state()

        models = []
        if self.registry.has(st["default_harness"]):
            instance = self.registry.create(st["default_harness"])
            models = await instance.list_models(force_refresh=force_refresh)

        text, markup = build_model_keyboard(
            models=models,
            current_harness=st["default_harness"],
            current_model=st["default_model"],
            fallback_chain=st["fallback_chain"],
            model_index=st["model_index"],
        )
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_effort(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        if arg.strip():
            if not self.state_manager.set_effort(arg.strip()):
                await self._safe_send(chat_id, "⚠️ Invalid effort. Choose: low, medium, high, xhigh.")
                return
        cur = self.state_manager.get_state()["default_effort"]
        text, markup = build_effort_keyboard(cur)
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_permissions(self, chat_id: int, arg: str = "", edit_message_id: Optional[int] = None) -> None:
        if arg.strip():
            self.state_manager.set_permission_preset(arg.strip())
        cur = self.state_manager.get_state()["default_permission_preset"]
        text, markup = build_permissions_keyboard(cur)
        await self._send_or_edit(chat_id, text, reply_markup=markup, edit_message_id=edit_message_id)

    async def _cmd_pause(self, chat_id: int, arg: str = "") -> None:
        pid = arg.strip().upper()
        if pid:
            if not load_task(pid, f"{pid}-0"):
                await self._safe_send(chat_id, f"⚠️ Project <code>{escape_html(pid)}</code> not found.")
                return
            self.state_manager.pause_project(pid)
            await self._safe_send(chat_id, f"⏸️ Paused autonomous scheduling for <code>{escape_html(pid)}</code>.")
        else:
            self.state_manager.set_watcher_paused(True)
            await self._safe_send(chat_id, "⏸️ Global watcher scheduling paused. Use /resume to continue.")

    async def _cmd_resume(self, chat_id: int, arg: str = "") -> None:
        pid = arg.strip().upper()
        if pid:
            self.state_manager.resume_project(pid)
            await self._safe_send(chat_id, f"▶️ Resumed scheduling for <code>{escape_html(pid)}</code>.")
        else:
            self.state_manager.set_watcher_paused(False)
            await self._safe_send(chat_id, "▶️ Global watcher scheduling resumed.")

    async def _cmd_stop(self, chat_id: int, arg: str = "") -> None:
        if self.watcher is None:
            await self._safe_send(chat_id, "⚠️ Async watcher is not attached.")
            return

        pid = arg.strip().upper()
        running = self._running_pids()
        if not pid:
            if not running:
                await self._safe_send(chat_id, "ℹ️ No project worker is currently running.")
                return
            stopped = []
            for r_pid in running:
                if await self.watcher.stop_project(r_pid):
                    stopped.append(r_pid)
            await self._safe_send(chat_id, f"🛑 Stopped running worker(s): <code>{escape_html(', '.join(stopped))}</code>")
            return

        if await self.watcher.stop_project(pid):
            await self._safe_send(chat_id, f"🛑 Stop signal sent to worker <code>{escape_html(pid)}</code>.")
        else:
            await self._safe_send(chat_id, f"ℹ️ Project <code>{escape_html(pid)}</code> is not currently running.")

    async def _cmd_steer(self, chat_id: int, arg: str = "", message_id: Optional[int] = None) -> None:
        raw = arg.strip()
        if not raw:
            await self._safe_send(chat_id, "Usage: <code>/steer [PID] &lt;instruction&gt;</code>", reply_to_message_id=message_id)
            return

        running = self._running_pids()
        parts = raw.split(maxsplit=1)
        cand_pid = parts[0].upper()
        if len(parts) == 2 and load_task(cand_pid, f"{cand_pid}-0"):
            pid, msg_text = cand_pid, parts[1].strip()
        elif len(running) == 1:
            pid, msg_text = running[0], raw
        else:
            await self._safe_send(
                chat_id,
                "⚠️ Specify project ID when steering: <code>/steer &lt;PID&gt; &lt;instruction&gt;</code>",
                reply_to_message_id=message_id,
            )
            return

        if self.watcher is not None:
            ok, reason = await self.watcher.steer_project(pid, msg_text)
            if ok:
                await self._safe_send(
                    chat_id,
                    f"🎯 Steered guidance into active worker <code>{escape_html(pid)}</code>.",
                    reply_to_message_id=message_id,
                )
                return

        # Graceful fallback: record steering note as an Admin comment for the next step.
        add_comment(pid, f"{pid}-0", "Admin (Steer)", msg_text)
        await self._safe_send(
            chat_id,
            f"📥 Queued steering note as Admin comment on <code>{escape_html(pid)}-0</code> for the next step.",
            reply_to_message_id=message_id,
        )

    async def _cmd_output(self, chat_id: int, arg: str = "") -> None:
        num_lines = 25
        if arg.strip():
            try:
                num_lines = max(1, min(200, int(arg.strip())))
            except ValueError:
                await self._safe_send(chat_id, "⚠️ Usage: /output [number_of_lines]")
                return

        log_path = os.path.join(get_data_dir(), "latest_worker_output.log")
        if not os.path.isfile(log_path):
            await self._safe_send(chat_id, "ℹ️ No worker output log found yet.")
            return

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-num_lines:]).strip()
        if not tail:
            await self._safe_send(chat_id, "ℹ️ Output log is empty.")
            return
        if len(tail) > 3500:
            tail = "..." + tail[-3500:]
        await self._safe_send(chat_id, f"<pre><code class=\"language-text\">{html.escape(tail)}</code></pre>")

    async def _cmd_sys(self, chat_id: int) -> None:
        current = self.state_manager.get_state()
        chain = self.state_manager.get_fallback_chain()
        models_list = "\n".join(
            f"{'✅' if i == current['model_index'] else '⬜'} [{i}] <code>{escape_html(entry['harness'])}</code> / "
            f"<code>{escape_html(entry['model'])}</code>"
            for i, entry in enumerate(chain)
        )
        text = (
            "⚙️ <b>System State</b>\n\n"
            f"⏳ <b>Started:</b> <code>{escape_html(str(current.get('start_time', 'Unknown')))}</code>\n"
            f"🔌 <b>Default Harness:</b> <code>{escape_html(current['default_harness'])}</code>\n"
            f"🤖 <b>Current Model:</b> <code>{escape_html(current['default_model'])}</code> (Index: {current['model_index']})\n"
            f"⚡ <b>Effort:</b> <code>{escape_html(current['default_effort'])}</code> | "
            f"<b>Preset:</b> <code>{escape_html(current['default_permission_preset'])}</code>\n"
            f"⏱️ <b>Watcher Interval:</b> <code>{current['interval_seconds']}s</code>\n\n"
            f"<b>Fallback Chain:</b>\n{models_list}"
        )
        await self._safe_send(chat_id, text)

    async def _cmd_upmodel(self, chat_id: int, arg: str = "") -> None:
        if not arg.strip():
            await self._safe_send(chat_id, "Usage: <code>/upmodel &lt;index&gt;</code>")
            return
        try:
            idx = int(arg.strip())
        except ValueError:
            await self._safe_send(chat_id, "⚠️ Index must be an integer.")
            return
        if self.state_manager.set_model_index(idx):
            await self._safe_send(
                chat_id,
                f"✅ Model updated to index {idx} (<code>{escape_html(self.state_manager.get_current_model())}</code>).",
            )
        else:
            await self._safe_send(
                chat_id,
                f"⚠️ Invalid index. Choose between 0 and {len(self.state_manager.get_all_models()) - 1}.",
            )

    async def _cmd_fix_data(self, chat_id: int) -> None:
        data_dir = get_data_dir()
        projects_dir = os.path.join(data_dir, "projects")
        if not os.path.isdir(projects_dir):
            await self._safe_send(chat_id, "⚠️ Data directory not found.")
            return

        errors: List[Tuple[str, str]] = []
        for pid in sorted(os.listdir(projects_dir)):
            proj_dir = os.path.join(projects_dir, pid)
            if not os.path.isdir(proj_dir):
                continue
            pjson = os.path.join(proj_dir, "project.json")
            if os.path.isfile(pjson):
                try:
                    with open(pjson, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as exc:
                    errors.append((pjson, str(exc)))
            tdir = os.path.join(proj_dir, "tasks")
            if os.path.isdir(tdir):
                for tid in sorted(os.listdir(tdir)):
                    tjson = os.path.join(tdir, tid, "task.json")
                    if os.path.isfile(tjson):
                        try:
                            with open(tjson, "r", encoding="utf-8") as f:
                                json.load(f)
                        except Exception as exc:
                            errors.append((tjson, str(exc)))

        if not errors:
            await self._safe_send(chat_id, "✅ All JSON files across all projects are valid.")
            return

        harness_name = self.state_manager.get_current_harness()
        model_name = self.state_manager.get_current_model()
        await self._safe_send(
            chat_id,
            f"🛠️ Found {len(errors)} JSON error(s). Invoking AI harness (<code>{escape_html(harness_name)}</code>) to repair...",
        )
        details = "\n".join(f"- {path}: {err}" for path, err in errors)
        prompt = (
            "Inspect and repair the following malformed TodoX JSON files:\n"
            f"{details}\n\n"
            "Ensure every file is valid JSON matching the TodoX 9-field schema."
        )
        profile = HarnessProfile(harness=harness_name, model=model_name)
        res = await self.router.run_step(RunRequest(prompt=prompt, working_dir=data_dir, model=model_name), profile=profile)
        if res.success:
            await self._safe_send(chat_id, f"✅ AI harness (<code>{escape_html(harness_name)}</code>) finished repairs.")
        else:
            await self._safe_send(
                chat_id,
                f"❌ AI harness repair failed: <code>{escape_html(res.error_message or res.status)}</code>",
            )

    async def _cmd_restart(self, chat_id: int) -> None:
        await self._safe_send(chat_id, "🔄 Restarting TodoX orchestrator...")
        if platform.system() == "Windows":
            subprocess.Popen(["cmd.exe", "/c", "restart.bat"])
        else:
            subprocess.Popen(["bash", "restart.sh"])

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
            voice_path = await synthesize_voice_ogg(spoken)
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

    # ------------------------------------------------------------------
    # Legacy VOP Multi-Line Batch Commands (100% Backwards-Compatible)
    # ------------------------------------------------------------------

    async def _handle_vop_lines(self, chat_id: int, text: str, message_id: Optional[int]) -> None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("/"):
                continue
            try:
                tokens = shlex.split(line)
            except Exception as exc:
                await self._safe_send(chat_id, f"⚠️ Error parsing line <code>{escape_html(line)}</code>: {escape_html(str(exc))}")
                continue
            if not tokens:
                continue

            cmd = tokens[0].split("@")[0].lower()
            if cmd in self._vop_handlers:
                args = parse_vop_args(tokens[1:])
                await self._vop_handlers[cmd](chat_id, args)
            else:
                arg_str = line.partition(" ")[2].strip()
                await self.dispatch_slash_command(chat_id, cmd, arg_str, message_id)

    async def _vop_new_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        title = str(args.get("title", "New Project"))
        desc = str(args.get("des", ""))
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        if load_task(pid, f"{pid}-0"):
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} already exists.")
            return
        create_project(pid, title, desc, get_default_worker_prompt())
        await self._safe_send(chat_id, f"✅ Project <code>{escape_html(pid)}</code> created: {escape_html(title)}")

    async def _vop_new_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        ptid = str(args.get("ptid", "")).upper()
        title = str(args.get("title", "New Task"))
        desc = str(args.get("des", ""))
        assignee = str(args.get("assignee", "AI"))
        status = str(args.get("status", "not_started"))
        if not pid or not ptid:
            await self._safe_send(chat_id, "Error: -pid and -ptid are required.")
            return
        task = create_task(pid, ptid, title, desc, assignee, status)
        if task:
            await self._safe_send(chat_id, f"✅ Task <code>{escape_html(task.id)}</code> created under <code>{escape_html(ptid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Parent task {escape_html(ptid)} not found.")

    async def _vop_new_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        msg = str(args.get("msg", ""))
        if not pid or not tid or not msg:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -msg are required.")
            return
        if add_comment(pid, tid, "Admin", msg):
            await self._safe_send(chat_id, f"✅ Comment added to <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_update_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        field = str(args.get("field", "")).lower()
        val = args.get("val", "")
        if not pid or not field:
            await self._safe_send(chat_id, "Error: -pid and -field are required.")
            return
        if field == "des":
            field = "descriptions"
        if update_project(pid, **{field: val}):
            await self._safe_send(chat_id, f"✅ Project <code>{escape_html(pid)}</code> updated ({escape_html(field)}).")
        else:
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")

    async def _vop_update_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        field = str(args.get("field", "")).lower()
        val = args.get("val", "")
        if not pid or not tid or not field:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -field are required.")
            return
        if field == "des":
            field = "descriptions"
        if update_task(pid, tid, **{field: val}):
            await self._safe_send(chat_id, f"✅ Task <code>{escape_html(tid)}</code> updated ({escape_html(field)}).")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_update_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        cidx = args.get("cidx")
        val = str(args.get("val", ""))
        if not pid or not tid or cidx is None or not val:
            await self._safe_send(chat_id, "Error: -pid, -tid, -cidx, and -val are required.")
            return
        try:
            idx = int(cidx)
        except ValueError:
            await self._safe_send(chat_id, "Error: -cidx must be an integer.")
            return
        if update_comment(pid, tid, idx, val):
            await self._safe_send(chat_id, f"✅ Comment {idx} updated in <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} or comment index not found.")

    async def _vop_del_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        if delete_project(pid):
            await self._safe_send(chat_id, f"🗑️ Project <code>{escape_html(pid)}</code> deleted.")
        else:
            await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")

    async def _vop_del_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        if not pid or not tid:
            await self._safe_send(chat_id, "Error: -pid and -tid are required.")
            return
        if delete_task(pid, tid):
            await self._safe_send(chat_id, f"🗑️ Task <code>{escape_html(tid)}</code> deleted.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} not found.")

    async def _vop_del_comment(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        cidx = args.get("cidx")
        if not pid or not tid or cidx is None:
            await self._safe_send(chat_id, "Error: -pid, -tid, and -cidx are required.")
            return
        try:
            idx = int(cidx)
        except ValueError:
            await self._safe_send(chat_id, "Error: -cidx must be an integer.")
            return
        if delete_comment(pid, tid, idx):
            await self._safe_send(chat_id, f"🗑️ Comment {idx} deleted from <code>{escape_html(tid)}</code>.")
        else:
            await self._safe_send(chat_id, f"Error: Task {escape_html(tid)} or comment index not found.")

    async def _vop_get_project(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        if not pid:
            await self._safe_send(chat_id, "Error: -pid is required.")
            return
        from orchestrator.html_generator import generate_project_html

        try:
            html_bytes = generate_project_html(pid)
            if html_bytes:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                await self.client.send_document(
                    chat_id=chat_id,
                    document_content=html_bytes,
                    filename=f"project_{pid}_{ts}.html",
                )
            else:
                await self._safe_send(chat_id, f"Error: Project {escape_html(pid)} not found.")
        except Exception as exc:
            await self._safe_send(chat_id, f"Error generating HTML: {escape_html(str(exc))}")

    async def _vop_get_task(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        tid = str(args.get("tid", "")).upper()
        if not pid or not tid:
            await self._safe_send(chat_id, "Error: -pid and -tid are required.")
            return
        await self._open_task_card(chat_id, pid, tid)

    async def _vop_get_file(self, chat_id: int, args: Dict[str, Any]) -> None:
        pid = str(args.get("pid", "")).upper()
        rel_path = str(args.get("path", ""))
        if not pid or not rel_path:
            await self._safe_send(chat_id, "Error: -pid and -path are required.")
            return
        await self._send_project_file(chat_id, pid, rel_path)

    async def _vop_fix_data(self, chat_id: int, args: Dict[str, Any]) -> None:
        await self._cmd_fix_data(chat_id)


if __name__ == "__main__":
    assert parse_allowed_chat_ids("100, 200", "300") == {100, 200, 300}
    bot = TodoXTelegramBot(bot_token="123456:DUMMY_TOKEN_VALUE", allowed_chat_ids={100})
    assert bot.is_token_valid()
    assert bot.is_chat_authorized(100)
    assert not bot.is_chat_authorized(999)
    print("telegram_ui.bot self-test passed!")
