"""Async project watcher with per-project locks, concurrency limit, stop/steer control, and auto-commit."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from harness.base import BaseHarness
from harness.registry import create_default_registry
from harness.router import HarnessRouter

from .state import SystemState, state as default_state
from .storage import get_data_dir, load_projects, load_task
from .worker import execute_ai_task, execute_ai_task_async


def _project_has_active_ai_work(pid: str) -> bool:
    """Return True if root PID-0 or any subtask in project pid is assigned to AI and in_progress."""
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)
    if not os.path.exists(project_dir):
        return False

    root_task = load_task(pid, f"{pid}-0")
    if root_task and root_task.assignee == "AI" and root_task.status == "in_progress":
        return True

    tasks_dir = os.path.join(project_dir, "tasks")
    if os.path.exists(tasks_dir):
        for tid in sorted(os.listdir(tasks_dir)):
            task = load_task(pid, tid)
            if task and task.assignee == "AI" and task.status == "in_progress":
                return True
    return False


def scan_and_trigger() -> None:
    """Legacy synchronous scan-and-trigger pass."""
    projects = load_projects()
    for pid in projects.keys():
        if _project_has_active_ai_work(pid):
            execute_ai_task(pid)


def check_and_commit_data(data_dir: str) -> None:
    """Commit any uncommitted changes inside DATA_DIR when DATA_DIR is a git repository."""
    if not os.path.exists(data_dir):
        return

    git_dir = os.path.join(data_dir, ".git")
    if not os.path.exists(git_dir):
        return

    try:
        status_output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=data_dir,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if status_output.strip():
            subprocess.run(["git", "add", "."], cwd=data_dir, check=True)
            commit_message = f"Auto-commit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            subprocess.run(["git", "commit", "-m", commit_message], cwd=data_dir, check=True)
            print(f"Committed changes in data directory: {commit_message}")
    except subprocess.CalledProcessError as exc:
        print(f"Failed to check or commit data directory: {exc}")
    except Exception as exc:
        print(f"Error during auto-commit: {exc}")


def start_watcher() -> None:
    """Legacy blocking background watcher loop."""
    print("Starting legacy orchestrator watcher loop")
    last_commit_time = time.time()

    while True:
        try:
            scan_and_trigger()
        except Exception as exc:
            print(f"Watcher error: {exc}")

        current_time = time.time()
        if (current_time - last_commit_time) >= 300:
            try:
                check_and_commit_data(get_data_dir())
            except Exception as exc:
                print(f"Auto-commit error: {exc}")
            last_commit_time = current_time

        time.sleep(default_state.get_interval())


class AsyncWatcher:
    """Concurrent asyncio project scheduler with per-project locks and live stop/steer control."""

    def __init__(
        self,
        router: Optional[HarnessRouter] = None,
        notifier: Any = None,
        state_manager: Optional[SystemState] = None,
        max_concurrent_workers: int = 2,
        commit_interval_seconds: float = 300.0,
    ) -> None:
        self.state_manager = state_manager or default_state
        self.router = router or HarnessRouter(registry=create_default_registry(), state_manager=self.state_manager)
        self.notifier = notifier
        self.max_concurrent_workers = max(1, int(max_concurrent_workers))
        self.commit_interval_seconds = max(10.0, float(commit_interval_seconds))

        self._semaphore = asyncio.Semaphore(self.max_concurrent_workers)
        self._project_locks: Dict[str, asyncio.Lock] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._running_harnesses: Dict[str, BaseHarness] = {}
        self._stop_event = asyncio.Event()
        self.running = False

    def _get_project_lock(self, pid: str) -> asyncio.Lock:
        lock = self._project_locks.get(pid)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[pid] = lock
        return lock

    def running_pids(self) -> List[str]:
        """Return sorted list of project IDs currently executing a worker step."""
        active = [pid for pid, task in self._running_tasks.items() if not task.done()]
        return sorted(active)

    def is_project_running(self, pid: str) -> bool:
        """Return True if a worker step is currently active for pid."""
        task = self._running_tasks.get(pid.strip().upper())
        return task is not None and not task.done()

    def find_eligible_projects(self) -> List[str]:
        """Return eligible project IDs respecting global pause, per-project pause, and running flags."""
        if self.state_manager.is_watcher_paused():
            return []

        eligible: List[str] = []
        for pid in sorted(load_projects().keys()):
            if self.state_manager.is_project_paused(pid):
                continue
            if self.is_project_running(pid):
                continue
            if _project_has_active_ai_work(pid):
                eligible.append(pid)
        return eligible

    async def _run_project_step(self, pid: str) -> None:
        lock = self._get_project_lock(pid)
        async with lock:
            async with self._semaphore:
                def _on_harness_started(harness: BaseHarness) -> None:
                    self._running_harnesses[pid] = harness

                try:
                    await execute_ai_task_async(
                        pid=pid,
                        router=self.router,
                        notifier=self.notifier,
                        on_harness_started=_on_harness_started,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[{pid}] AsyncWatcher worker error: {exc}")
                finally:
                    self._running_harnesses.pop(pid, None)
                    self._running_tasks.pop(pid, None)

    async def trigger_project(self, pid: str) -> bool:
        """Schedule an immediate worker step for pid unless one is already running."""
        clean_pid = pid.strip().upper()
        if self.is_project_running(clean_pid):
            return False

        task = asyncio.create_task(self._run_project_step(clean_pid))
        self._running_tasks[clean_pid] = task
        return True

    async def scan_once(self) -> List[str]:
        """Scan projects once and spawn background tasks for all eligible projects."""
        triggered: List[str] = []
        for pid in self.find_eligible_projects():
            if await self.trigger_project(pid):
                triggered.append(pid)
        return triggered

    async def stop_project(self, pid: str) -> bool:
        """Stop an active project worker subprocess and cancel its task."""
        clean_pid = pid.strip().upper()
        stopped_any = False

        harness = self._running_harnesses.get(clean_pid)
        if harness is not None:
            try:
                await harness.stop()
                stopped_any = True
            except Exception:
                pass

        task = self._running_tasks.get(clean_pid)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            stopped_any = True

        return stopped_any

    async def steer_project(self, pid: str, message: str) -> Tuple[bool, str]:
        """Steer a running project harness mid-flight if supported."""
        clean_pid = pid.strip().upper()
        harness = self._running_harnesses.get(clean_pid)
        if harness is None:
            return False, "not_running"
        if not harness.capabilities.supports_steering or not harness.is_running():
            return False, "not_steerable"
        ok = await harness.steer(message)
        return ok, "steered" if ok else "steer_failed"

    async def run_loop(self) -> None:
        """Run the async watcher scheduling loop until stop() is called."""
        self.running = True
        self._stop_event.clear()
        last_commit_time = time.time()
        print(f"Starting AsyncWatcher loop (max_concurrent_workers={self.max_concurrent_workers})")

        while self.running and not self._stop_event.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                print(f"AsyncWatcher scan error: {exc}")

            now = time.time()
            if (now - last_commit_time) >= self.commit_interval_seconds:
                try:
                    await asyncio.to_thread(check_and_commit_data, get_data_dir())
                except Exception as exc:
                    print(f"AsyncWatcher auto-commit error: {exc}")
                last_commit_time = now

            interval = self.state_manager.get_interval()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """Stop the watcher loop and terminate any active worker tasks."""
        self.running = False
        self._stop_event.set()
        for pid in list(self._running_tasks.keys()):
            await self.stop_project(pid)


if __name__ == "__main__":
    watcher = AsyncWatcher(max_concurrent_workers=2)
    assert watcher.running_pids() == []
    print("orchestrator.watcher self-test passed!")
