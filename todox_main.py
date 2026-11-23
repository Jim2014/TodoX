"""Unified asyncio entrypoint for TodoX Orchestrator, Telegram UI, and optional Web Panel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# Prevent crash when running via pythonw on Windows
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from harness import HarnessRouter, create_default_registry
from orchestrator.demo import seed_demo_workspace
from orchestrator.state import state
from orchestrator.watcher import AsyncWatcher
from telegram_ui import TelegramClient, TodoXTelegramBot, WorkerNotifier, parse_allowed_chat_ids
from web.app import run_web


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _configure_demo_environment(data_dir: Optional[str] = None, port: Optional[int] = None) -> str:
    """Configure isolated zero-credential demo workspace and MockHarness defaults."""
    demo_dir = os.path.abspath(data_dir) if data_dir else tempfile.mkdtemp(prefix="todox_demo_")
    os.makedirs(demo_dir, exist_ok=True)
    os.environ["DATA_DIR"] = demo_dir
    os.environ["TODOX_DEMO_MODE"] = "1"
    os.environ["TG_TOKEN"] = ""
    os.environ["WEB_ENABLED"] = "true"
    os.environ["DEFAULT_HARNESS"] = "mock"
    os.environ["DEFAULT_MODEL"] = "mock-agent-v1"
    if port is not None:
        os.environ["WEB_PORT"] = str(int(port))

    state._state_file = os.path.join(demo_dir, "system_state.json")
    state._load()
    if not os.path.isdir(os.path.join(demo_dir, "projects", "AUTH")):
        seed_demo_workspace(demo_dir)
    return demo_dir


async def async_main(
    demo: bool = False,
    port: Optional[int] = None,
    data_dir: Optional[str] = None,
) -> None:
    """Initialize services and run AsyncWatcher + TodoXTelegramBot concurrently."""
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if demo:
        demo_dir = _configure_demo_environment(data_dir=data_dir, port=port)
        state.reset_model()
        state.set_harness("mock", "mock-agent-v1")
    else:
        demo_dir = ""
        if data_dir:
            abs_dir = os.path.abspath(data_dir)
            os.environ["DATA_DIR"] = abs_dir
            state._state_file = os.path.join(abs_dir, "system_state.json")
            state._load()
        if port is not None:
            os.environ["WEB_PORT"] = str(int(port))
        state.reset_model()

    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.set_start_time(start_time_str)

    print("--------------------------------------------------")
    print(f"TodoX Orchestrator System Starting at {start_time_str}...")
    if demo:
        print(f"DEMO MODE active (zero credentials, MockHarness simulator, workspace={demo_dir})")
    print("--------------------------------------------------")

    web_process: Optional[object] = None
    if _env_bool("WEB_ENABLED", True):
        web_process = run_web()
        web_port = os.environ.get("WEB_PORT", "8899")
        print(f"Web UI Dashboard running on http://localhost:{web_port} ...")
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
    parser = argparse.ArgumentParser(description="TodoX Autonomous AI Project & Task Orchestrator")
    parser.add_argument("--demo", action="store_true", help="Launch zero-config Web Kanban demo with MockHarness")
    parser.add_argument("--port", type=int, default=None, help="Override WEB_PORT (default 8899)")
    parser.add_argument("--data-dir", type=str, default=None, help="Override DATA_DIR path")
    args = parser.parse_args()
    try:
        asyncio.run(async_main(demo=args.demo, port=args.port, data_dir=args.data_dir))
    except KeyboardInterrupt:
        print("TodoX stopped by keyboard interrupt.")


if __name__ == "__main__":
    main()
