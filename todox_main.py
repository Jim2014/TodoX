"""Unified asyncio entrypoint for TodoX Orchestrator, Telegram UI, and optional Web Panel."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# Prevent crash when running via pythonw on Windows
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from harness import HarnessRouter, create_default_registry
from orchestrator.state import state
from orchestrator.watcher import AsyncWatcher
from telegram_ui import TelegramClient, TodoXTelegramBot, WorkerNotifier, parse_allowed_chat_ids
from web.app import run_web


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def async_main() -> None:
    """Initialize services and run AsyncWatcher + TodoXTelegramBot concurrently."""
    load_dotenv()
    state.reset_model()

    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.set_start_time(start_time_str)

    print("--------------------------------------------------")
    print(f"TodoX Orchestrator System Starting at {start_time_str}...")
    print("--------------------------------------------------")

    web_process: Optional[object] = None
    if _env_bool("WEB_ENABLED", True):
        web_process = run_web()
        web_port = os.environ.get("WEB_PORT", "8899")
        print(f"Web UI Dashboard running on port {web_port}...")
    else:
        print("Web UI Dashboard disabled (WEB_ENABLED=false).")

    tg_token = os.environ.get("TG_TOKEN", "").strip()
    allowed_ids = parse_allowed_chat_ids(
        os.environ.get("ALLOWED_CHAT_IDS", ""),
        os.environ.get("ADMIN_CHAT_ID", ""),
    )
    allow_all = _env_bool("ALLOW_ALL_CHATS", False)
    throttle = float(os.environ.get("STREAM_THROTTLE_SECONDS", "1.5") or "1.5")
    max_workers = int(os.environ.get("MAX_CONCURRENT_WORKERS", "2") or "2")

    registry = create_default_registry()
    router = HarnessRouter(registry=registry, state_manager=state)
    client = TelegramClient(bot_token=tg_token)
    notifier = WorkerNotifier(
        client=client if tg_token else None,
        notify_chat_ids=sorted(allowed_ids),
        stream_throttle_seconds=throttle,
    )
    watcher = AsyncWatcher(
        router=router,
        notifier=notifier if notifier.has_targets() else None,
        state_manager=state,
        max_concurrent_workers=max_workers,
    )
    bot = TodoXTelegramBot(
        bot_token=tg_token,
        allowed_chat_ids=allowed_ids,
        allow_all_chats=allow_all,
        registry=registry,
        router=router,
        watcher=watcher,
        state_manager=state,
        client=client,
        notifier=notifier,
        stream_throttle_seconds=throttle,
    )

    loop = asyncio.get_running_loop()
    stop_triggered = False

    async def _shutdown() -> None:
        nonlocal stop_triggered
        if stop_triggered:
            return
        stop_triggered = True
        print("Shutting down TodoX...")
        await bot.stop()
        await watcher.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(watcher.run_loop())]
    if bot.is_token_valid():
        tasks.append(asyncio.create_task(bot.start()))
    else:
        print("TG_TOKEN not configured or placeholder; running watcher without Telegram bot.")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown()
        if web_process is not None and hasattr(web_process, "terminate"):
            web_process.terminate()


def main() -> None:
    """Synchronous entrypoint for todox_main.py."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("TodoX stopped by keyboard interrupt.")


if __name__ == "__main__":
    main()
