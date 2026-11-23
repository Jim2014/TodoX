"""Async Telegram bot orchestrator for TodoX composing command, callback, voice, and VOP mixins."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from harness import HarnessRouter, create_default_registry
from orchestrator.models import Task
from orchestrator.state import SystemState, state as default_state
from orchestrator.storage import add_comment, get_data_dir, load_projects, load_task
from orchestrator.voice import synthesize_voice_ogg, transcribe_voice

from .callbacks import TelegramCallbacksMixin
from .client import TelegramAPIError, TelegramClient, format_exception_brief
from .commands import TelegramCommandsMixin
from .formatter import escape_html, format_pinned_dashboard
from .keyboards import BOT_COMMANDS, build_pinned_banner_keyboard
from .notifier import WorkerNotifier
from .voice_handlers import TelegramVoiceMixin
from .vop import TelegramVopMixin, parse_vop_args

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


class TodoXTelegramBot(
    TelegramCommandsMixin,
    TelegramCallbacksMixin,
    TelegramVoiceMixin,
    TelegramVopMixin,
):
    """Async Telegram C&C interface supporting inline cards, voice/chat routing, live streaming, and VOP batch mode."""

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

    async def process_update(self, update: Dict[str, Any]) -> None:
        """Authenticate and route one Telegram update dict."""
        if "callback_query" in update:
            await self.handle_callback_query(update["callback_query"])
            return

        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id = int(chat_id)
        message_id = message.get("message_id")

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


if __name__ == "__main__":
    assert parse_allowed_chat_ids("100, 200", "300") == {100, 200, 300}
    bot = TodoXTelegramBot(bot_token="123456:DUMMY_TOKEN_VALUE", allowed_chat_ids={100})
    assert bot.is_token_valid()
    assert bot.is_chat_authorized(100)
    assert not bot.is_chat_authorized(999)
    print("telegram_ui.bot self-test passed!")
