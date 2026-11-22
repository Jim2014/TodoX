"""Async Telegram Bot API client using httpx with token redaction and safe retries."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_BOT_TOKEN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TG_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|a|tg-spoiler|tg-emoji)(?:\s+[^>]*)?>",
    re.IGNORECASE,
)

_RETRYABLE_IDEMPOTENT_METHODS = frozenset(
    {
        "editMessageText",
        "editMessageReplyMarkup",
        "setMessageReaction",
        "sendChatAction",
        "answerCallbackQuery",
        "getMe",
        "getFile",
    }
)
_TRANSIENT_RETRY_DELAY_SECONDS = 0.3
_NON_BLOCKING_STREAM_METHODS = frozenset(
    {
        "editMessageText",
        "editMessageReplyMarkup",
        "sendChatAction",
        "setMessageReaction",
    }
)
_MAX_INLINE_429_SLEEP_SECONDS = 5.0
_MAX_CRITICAL_429_SLEEP_SECONDS = 30.0


def strip_telegram_html(text: str) -> str:
    """Strip Telegram HTML formatting tags and unescape entities for plain-text fallback."""
    if not text:
        return ""
    without_tags = _TG_HTML_TAG_RE.sub("", text)
    return html.unescape(without_tags)


def redact_bot_token(text: str) -> str:
    """Mask Telegram bot tokens inside URLs or error strings before logging."""
    if not text:
        return ""
    return _BOT_TOKEN_URL_RE.sub("bot<REDACTED>", text)


def format_exception_brief(exc: BaseException) -> str:
    """Format an exception safely with its class name and any bot token redacted."""
    msg = redact_bot_token(str(exc).strip())
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class TelegramAPIError(Exception):
    """Exception raised when Telegram Bot API returns ok=false."""

    def __init__(self, error_code: int, description: str, retry_after: Optional[float] = None) -> None:
        self.error_code = int(error_code)
        self.description = redact_bot_token(str(description))
        self.retry_after = retry_after
        super().__init__(f"Telegram API Error {self.error_code}: {self.description}")


class TelegramClient:
    """Lightweight async Telegram Bot API client backed by a shared httpx.AsyncClient."""

    def __init__(self, bot_token: str, base_url: str = "https://api.telegram.org") -> None:
        self.bot_token = bot_token
        clean_base = base_url.rstrip("/")
        self.base_url = f"{clean_base}/bot{bot_token}"
        self.file_base_url = f"{clean_base}/file/bot{bot_token}"
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        """Return or lazily create the shared httpx.AsyncClient."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _post(
        self,
        method: str,
        payload: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Any:
        """Execute a Telegram Bot API POST call with 429 handling and idempotent retries."""
        client = await self.get_client()
        url = f"{self.base_url}/{method}"
        kwargs: Dict[str, Any] = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout

        max_attempts = 2 if method in _RETRYABLE_IDEMPOTENT_METHODS else 1
        resp: Optional[httpx.Response] = None
        data: Dict[str, Any] = {}

        for attempt in range(max_attempts):
            try:
                resp = await client.post(url, **kwargs)
                data = resp.json()
                break
            except httpx.RequestError as exc:
                err_desc = format_exception_brief(exc)
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "Transient HTTP error calling %s (%s); retrying once in %.1fs",
                        method,
                        err_desc,
                        _TRANSIENT_RETRY_DELAY_SECONDS,
                    )
                    await asyncio.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                logger.error("HTTP request error calling %s: %s", method, err_desc)
                raise

        if not data.get("ok"):
            err_code = data.get("error_code", resp.status_code if resp is not None else 500)
            desc = redact_bot_token(str(data.get("description", "Unknown error")))

            if err_code == 429:
                params = data.get("parameters", {})
                try:
                    retry_after = max(0.0, float(params.get("retry_after", 1) or 1))
                except (TypeError, ValueError):
                    retry_after = 1.0
                logger.warning("Telegram rate limit (429) on %s; retry after %.1fs", method, retry_after)
                if method in _NON_BLOCKING_STREAM_METHODS and retry_after > _MAX_INLINE_429_SLEEP_SECONDS:
                    raise TelegramAPIError(err_code, desc, retry_after=retry_after)
                if retry_after <= _MAX_CRITICAL_429_SLEEP_SECONDS:
                    await asyncio.sleep(retry_after)
                    resp = await client.post(url, **kwargs)
                    data = resp.json()
                    if data.get("ok"):
                        return data.get("result", {})
                    err_code = data.get("error_code", resp.status_code if resp is not None else 429)
                    desc = redact_bot_token(str(data.get("description", desc)))
                raise TelegramAPIError(err_code, desc, retry_after=retry_after)

            raise TelegramAPIError(err_code, desc)

        return data.get("result", {})

    async def get_me(self) -> Dict[str, Any]:
        """Verify bot token and retrieve bot metadata."""
        res = await self._post("getMe", {})
        return res if isinstance(res, dict) else {}

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """Retrieve file metadata from Telegram."""
        res = await self._post("getFile", {"file_id": file_id})
        return res if isinstance(res, dict) else {}

    async def download_file(
        self,
        file_path: str,
        destination: Path | str,
        timeout: float = 60.0,
    ) -> Path:
        """Download a Telegram file by file_path to local disk."""
        client = await self.get_client()
        url = f"{self.file_base_url}/{file_path.lstrip('/')}"
        dest_path = Path(destination).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            dest_path.write_bytes(resp.content)
            return dest_path
        except httpx.RequestError as exc:
            logger.error("HTTP error downloading Telegram file %s: %s", file_path, format_exception_brief(exc))
            raise

    async def download_file_by_id(
        self,
        file_id: str,
        destination_dir: Path | str,
        filename_prefix: str = "voice",
    ) -> Tuple[Path, Dict[str, Any]]:
        """Resolve Telegram file metadata by ID and download to destination_dir."""
        file_info = await self.get_file(file_id)
        remote_path = str(file_info.get("file_path") or "").strip()
        if not remote_path:
            raise ValueError(f"Missing file_path for Telegram file_id={file_id}")
        ext = Path(remote_path).suffix or ".oga"
        uid = str(file_info.get("file_unique_id") or "audio")
        filename = f"{filename_prefix}_{uid}_{int(time.time())}{ext}"
        dest = Path(destination_dir) / filename
        saved = await self.download_file(remote_path, dest)
        return saved, file_info

    async def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 100,
        timeout: int = 30,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """Long-poll updates from Telegram Bot API."""
        payload: Dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        http_timeout = float(timeout + 15)
        try:
            result = await self._post("getUpdates", payload, timeout=http_timeout)
            return result if isinstance(result, list) else []
        except Exception as exc:
            logger.warning("getUpdates failed: %s", format_exception_brief(exc))
            if raise_on_error:
                raise
            return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Send a chat message, automatically falling back to plain text on HTML entity errors."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if disable_notification:
            payload["disable_notification"] = True

        try:
            res = await self._post("sendMessage", payload)
            return res if isinstance(res, dict) else {}
        except TelegramAPIError as exc:
            lowered = exc.description.lower()
            if parse_mode and ("can't parse entities" in lowered or "tag" in lowered):
                logger.warning("Telegram rejected HTML in sendMessage (%s); falling back to stripped plain text", exc.description)
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                plain_payload["text"] = strip_telegram_html(text)
                res = await self._post("sendMessage", plain_payload)
                return res if isinstance(res, dict) else {}
            raise

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Edit an existing message, treating unchanged content as a no-op."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            res = await self._post("editMessageText", payload)
            return res if isinstance(res, dict) else None
        except TelegramAPIError as exc:
            lowered = exc.description.lower()
            if "message is not modified" in lowered:
                return None
            if parse_mode and ("can't parse entities" in lowered or "tag" in lowered):
                logger.warning("Telegram rejected HTML in editMessageText (%s); falling back to stripped plain text", exc.description)
                plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                plain_payload["text"] = strip_telegram_html(text)
                try:
                    res = await self._post("editMessageText", plain_payload)
                    return res if isinstance(res, dict) else None
                except TelegramAPIError as exc2:
                    if "message is not modified" in exc2.description.lower():
                        return None
                    raise
            raise

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Edit only the inline keyboard attached to an existing message."""
        payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            res = await self._post("editMessageReplyMarkup", payload)
            return res if isinstance(res, dict) else None
        except TelegramAPIError as exc:
            if "message is not modified" in exc.description.lower():
                return None
            raise

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """Acknowledge an inline keyboard callback query."""
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        try:
            res = await self._post("answerCallbackQuery", payload)
            return bool(res)
        except Exception as exc:
            logger.warning("Failed to answer callback query %s: %s", callback_query_id, format_exception_brief(exc))
            return False

    async def send_document(
        self,
        chat_id: int,
        document_content: bytes | str | Path,
        filename: str = "document.html",
        caption: Optional[str] = None,
        parse_mode: Optional[str] = "HTML",
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Upload a document file via multipart/form-data."""
        client = await self.get_client()
        url = f"{self.base_url}/sendDocument"

        if isinstance(document_content, str):
            file_bytes = document_content.encode("utf-8")
        elif isinstance(document_content, Path):
            file_bytes = document_content.read_bytes()
            if not filename or filename == "document.html":
                filename = document_content.name
        else:
            file_bytes = document_content

        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_to_message_id:
            data["reply_to_message_id"] = str(reply_to_message_id)
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        if disable_notification:
            data["disable_notification"] = "true"

        lower_name = filename.lower()
        if lower_name.endswith((".html", ".htm")):
            mime_type = "text/html"
        elif lower_name.endswith((".diff", ".patch")):
            mime_type = "text/x-diff"
        elif lower_name.endswith(".json"):
            mime_type = "application/json"
        elif lower_name.endswith((".txt", ".md", ".log")):
            mime_type = "text/plain"
        else:
            mime_type = "application/octet-stream"

        files = {"document": (filename, file_bytes, mime_type)}
        try:
            resp = await client.post(url, data=data, files=files, timeout=60.0)
            res_json = resp.json()
        except httpx.RequestError as exc:
            logger.error("HTTP error calling sendDocument: %s", format_exception_brief(exc))
            raise

        if not res_json.get("ok"):
            err_code = res_json.get("error_code", resp.status_code)
            desc = redact_bot_token(str(res_json.get("description", "Unknown error")))
            if parse_mode and ("can't parse entities" in desc.lower() or "tag" in desc.lower()):
                logger.warning("Telegram rejected HTML caption in sendDocument (%s); falling back to stripped plain text", desc)
                data.pop("parse_mode", None)
                if caption:
                    data["caption"] = strip_telegram_html(caption)
                resp = await client.post(url, data=data, files=files, timeout=60.0)
                res_json = resp.json()
                if res_json.get("ok"):
                    return res_json.get("result", {})
            raise TelegramAPIError(err_code, desc)

        result = res_json.get("result", {})
        return result if isinstance(result, dict) else {}

    async def send_voice(
        self,
        chat_id: int,
        voice: bytes | str | Path,
        filename: str = "voice.ogg",
        caption: Optional[str] = None,
        parse_mode: Optional[str] = "HTML",
        duration: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Upload a native Telegram voice note (.ogg opus) via multipart/form-data."""
        client = await self.get_client()
        url = f"{self.base_url}/sendVoice"

        if isinstance(voice, Path):
            voice_bytes = voice.read_bytes()
        elif isinstance(voice, str):
            if os.path.isfile(voice):
                voice_bytes = Path(voice).read_bytes()
            else:
                voice_bytes = voice.encode("utf-8")
        else:
            voice_bytes = voice

        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            if parse_mode:
                data["parse_mode"] = parse_mode
        if duration is not None:
            data["duration"] = str(duration)
        if reply_to_message_id:
            data["reply_to_message_id"] = str(reply_to_message_id)
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        if disable_notification:
            data["disable_notification"] = "true"

        files = {"voice": (filename, voice_bytes, "audio/ogg")}
        try:
            resp = await client.post(url, data=data, files=files, timeout=60.0)
            res_json = resp.json()
        except httpx.RequestError as exc:
            logger.error("HTTP error calling sendVoice: %s", format_exception_brief(exc))
            raise

        if not res_json.get("ok"):
            err_code = res_json.get("error_code", resp.status_code)
            desc = redact_bot_token(str(res_json.get("description", "Unknown error")))
            if parse_mode and ("can't parse entities" in desc.lower() or "tag" in desc.lower()):
                data.pop("parse_mode", None)
                if caption:
                    data["caption"] = strip_telegram_html(caption)
                resp = await client.post(url, data=data, files=files, timeout=60.0)
                res_json = resp.json()
                if res_json.get("ok"):
                    return res_json.get("result", {})
            raise TelegramAPIError(err_code, desc)

        result = res_json.get("result", {})
        return result if isinstance(result, dict) else {}

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send chat action indicator (typing, upload_document, etc.)."""
        try:
            await self._post("sendChatAction", {"chat_id": chat_id, "action": action})
            return True
        except Exception:
            return False

    async def set_my_commands(
        self,
        commands: List[Dict[str, str]],
        scope: Optional[Dict[str, Any]] = None,
        language_code: Optional[str] = None,
    ) -> bool:
        """Register Telegram slash commands menu."""
        payload: Dict[str, Any] = {"commands": commands}
        if scope:
            payload["scope"] = scope
        if language_code:
            payload["language_code"] = language_code
        try:
            res = await self._post("setMyCommands", payload)
            return bool(res)
        except Exception as exc:
            logger.warning("Failed to set bot commands: %s", format_exception_brief(exc))
            return False

    async def set_chat_menu_button(
        self,
        chat_id: Optional[int] = None,
        menu_button: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Configure the chat menu button."""
        payload: Dict[str, Any] = {"menu_button": menu_button or {"type": "commands"}}
        if chat_id is not None:
            payload["chat_id"] = chat_id
        try:
            res = await self._post("setChatMenuButton", payload)
            return bool(res)
        except Exception as exc:
            logger.warning("Failed to set chat menu button: %s", format_exception_brief(exc))
            return False

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: Optional[str] = None,
        is_big: bool = False,
    ) -> bool:
        """Set or clear an emoji reaction on a message."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": reaction}] if reaction else [],
        }
        if is_big:
            payload["is_big"] = True
        try:
            res = await self._post("setMessageReaction", payload)
            return bool(res)
        except Exception:
            return False

    async def pin_chat_message(
        self,
        chat_id: int,
        message_id: int,
        disable_notification: bool = True,
    ) -> bool:
        """Pin a message to the top of the chat."""
        try:
            res = await self._post(
                "pinChatMessage",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "disable_notification": disable_notification,
                },
            )
            return bool(res)
        except Exception:
            return False

    async def unpin_chat_message(
        self,
        chat_id: int,
        message_id: Optional[int] = None,
    ) -> bool:
        """Unpin a message from the top of the chat."""
        payload: Dict[str, Any] = {"chat_id": chat_id}
        if message_id is not None:
            payload["message_id"] = message_id
        try:
            res = await self._post("unpinChatMessage", payload)
            return bool(res)
        except Exception:
            return False


if __name__ == "__main__":
    async def _main() -> None:
        redacted = redact_bot_token("https://api.telegram.org/bot123456:ABCDEF_secret_token/getMe")
        assert "<REDACTED>" in redacted
        assert "secret_token" not in redacted
        stripped = strip_telegram_html("<b>Bold</b> &lt;div&gt; <code>x &amp; y</code>")
        assert stripped == "Bold <div> x & y"
        err = TelegramAPIError(429, "Too Many Requests", retry_after=196.0)
        assert err.retry_after == 196.0
        client = TelegramClient("123456:DUMMY_TOKEN")
        assert client.base_url.endswith("/bot123456:DUMMY_TOKEN")
        await client.close()
        print("telegram_ui.client self-test passed!")

    asyncio.run(_main())
