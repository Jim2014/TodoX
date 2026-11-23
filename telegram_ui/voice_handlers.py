"""Per-chat project focus state, voice STT/TTS routing, and active-project chat mixin."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.chat_router import handle_project_chat
from orchestrator.storage import get_data_dir, load_projects, load_task
from orchestrator.voice import distill_spoken_briefing

from .client import format_exception_brief
from .formatter import escape_html, markdown_to_telegram_html
from .keyboards import build_projects_keyboard

logger = logging.getLogger(__name__)


class TelegramVoiceMixin:
    """Provides persistent active-project chat state and voice STT/TTS handling."""

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
        import telegram_ui.bot as _bot_mod

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
                voice_path = await _bot_mod.synthesize_voice_ogg(spoken)
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
        import telegram_ui.bot as _bot_mod

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
            transcript, _lang = await _bot_mod.transcribe_voice(local_audio)
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


if __name__ == "__main__":
    assert hasattr(TelegramVoiceMixin, "_handle_voice_message")
    print("telegram_ui.voice_handlers self-test passed!")
