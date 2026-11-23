"""Zero-credential simulated CLI harness for TodoX `--demo` mode and offline smoke tests."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
from typing import AsyncIterator, List, Optional

from .base import BaseHarness
from .types import (
    CanonicalTool,
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HealthStatus,
    ModelInfo,
    PermissionPreset,
    RunRequest,
    UsageMetrics,
)

DEFAULT_MOCK_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="mock-agent-v1",
        name="Demo Agent v1",
        harness="mock",
        icon="🎭",
        description="Deterministic local simulator for zero-config demos",
    )
]


class MockHarness(BaseHarness):
    """Simulates realistic multi-tool coding turns without API credentials."""

    def __init__(self, step_delay: Optional[float] = None, **kwargs: object) -> None:
        if step_delay is not None:
            self.step_delay = max(0.0, float(step_delay))
        else:
            env_delay = os.environ.get("TODOX_MOCK_DELAY", "0.35").strip()
            try:
                self.step_delay = max(0.0, float(env_delay))
            except ValueError:
                self.step_delay = 0.35
        self._running = False
        self._stopped = False
        self._steered_notes: List[str] = []

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name="mock",
            display_name="Simulated Demo Agent",
            supports_streaming=True,
            supports_steering=True,
            supports_warm_session=False,
            supports_session_resume=True,
            supports_effort=True,
            supports_model_discovery=False,
            supports_json_schema=True,
        )

    def is_running(self) -> bool:
        return self._running

    async def check_health(self) -> HealthStatus:
        return HealthStatus(
            harness="mock",
            available=True,
            binary_path="builtin://mock",
            version="demo-1.0",
            message="Demo simulator ready",
        )

    async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        return list(DEFAULT_MOCK_MODELS)

    async def steer(self, message: str) -> bool:
        if not self._running:
            return False
        cleaned = (message or "").strip()
        if cleaned:
            self._steered_notes.append(cleaned)
        return True

    async def stop(self) -> None:
        self._stopped = True
        self._running = False

    def _advance_first_active_task(self, working_dir: str, summary_note: str) -> Optional[str]:
        """Find the first in_progress AI task in working_dir and hand it to Admin review."""
        tasks_dir = Path(working_dir) / "tasks"
        if not tasks_dir.is_dir():
            return None
        for entry in sorted(tasks_dir.iterdir(), key=lambda p: p.name):
            task_file = entry / "task.json"
            if not task_file.is_file():
                continue
            try:
                data = json.loads(task_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("status") == "in_progress" and data.get("assignee") == "AI":
                comments = list(data.get("comments") or [])
                comments.append(
                    {
                        "author": "AI",
                        "time": datetime.now().isoformat(),
                        "content": summary_note,
                    }
                )
                data["comments"] = comments
                data["status"] = "need_confirm"
                data["assignee"] = "Admin"
                task_file.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
                return str(data.get("id") or entry.name)
        return None

    async def _sleep_step(self) -> bool:
        if self.step_delay > 0:
            await asyncio.sleep(self.step_delay)
        return self._stopped

    async def run_stream(self, request: RunRequest) -> AsyncIterator[HarnessEvent]:
        self._running = True
        self._stopped = False
        self._steered_notes.clear()
        pid = Path(request.working_dir).name or "DEMO"

        try:
            yield HarnessEvent(type="init", harness="mock", session_id=f"mock-{pid.lower()}")

            # Read-only PLAN turns: briefings or intent classifier calls
            if request.permission_preset == PermissionPreset.PLAN:
                prompt_low = request.prompt.lower()
                if "strict json only" in prompt_low or '"intent"' in prompt_low:
                    reply_json = json.dumps(
                        {
                            "intent": "create_and_run",
                            "tid": "",
                            "title": "Implement requested enhancement",
                            "description": "Created from conversational command bar in TodoX Demo Mode.",
                            "comment": "",
                            "reply": "Created a new ticket and started the simulated AI worker step.",
                        }
                    )
                    if await self._sleep_step():
                        yield HarnessEvent(
                            type="result",
                            harness="mock",
                            status="CANCELLED",
                            error_category=ErrorCategory.CANCELLED,
                            response="Cancelled by Admin.",
                        )
                        return
                    yield HarnessEvent(type="text_delta", harness="mock", text_delta=reply_json)
                    yield HarnessEvent(
                        type="result",
                        harness="mock",
                        status="SUCCESS",
                        duration_seconds=round(self.step_delay, 2),
                        usage=UsageMetrics(input_tokens=320, output_tokens=95, total_tokens=415),
                        response=reply_json,
                    )
                    return

                summary_md = (
                    f"Executive summary for {pid}: active deliverables are progressing smoothly in Demo Mode. "
                    "Verified completed unit modules, flagged pending need_confirm tickets for Admin approval, "
                    "and queued remaining backlog items with zero errors."
                )
                yield HarnessEvent(
                    type="tool",
                    harness="mock",
                    canonical_tool=CanonicalTool.READ,
                    raw_tool_name="read_file",
                    tool_state="DONE",
                    duration_seconds=0.1,
                    tool_info={"summary": "project.json & tasks/*.json"},
                )
                if await self._sleep_step():
                    yield HarnessEvent(
                        type="result",
                        harness="mock",
                        status="CANCELLED",
                        error_category=ErrorCategory.CANCELLED,
                        response="Cancelled by Admin.",
                    )
                    return
                yield HarnessEvent(type="text_delta", harness="mock", text_delta=summary_md)
                yield HarnessEvent(
                    type="result",
                    harness="mock",
                    status="SUCCESS",
                    duration_seconds=round(self.step_delay, 2),
                    usage=UsageMetrics(input_tokens=510, output_tokens=130, total_tokens=640),
                    response=summary_md,
                )
                return

            # Full simulated coding step
            steps = [
                (CanonicalTool.READ, "read_file", "project.json & active task ticket", "Inspecting task requirements and existing tests...\n"),
                (CanonicalTool.EDIT, "edit_file", f"{pid.lower()}_module.py", "Applying implementation patch and updating state assertions...\n"),
                (CanonicalTool.SHELL, "run_command", "python -m unittest -v", "Running isolated unit tests: all checks passed cleanly.\n"),
            ]
            for tool_cat, raw_name, summary, msg in steps:
                yield HarnessEvent(
                    type="tool",
                    harness="mock",
                    canonical_tool=tool_cat,
                    raw_tool_name=raw_name,
                    tool_state="ACTIVE",
                    tool_info={"summary": summary},
                )
                if await self._sleep_step():
                    yield HarnessEvent(
                        type="result",
                        harness="mock",
                        status="CANCELLED",
                        error_category=ErrorCategory.CANCELLED,
                        response="Execution cancelled by Admin.",
                    )
                    return
                yield HarnessEvent(
                    type="tool",
                    harness="mock",
                    canonical_tool=tool_cat,
                    raw_tool_name=raw_name,
                    tool_state="DONE",
                    duration_seconds=max(0.1, round(self.step_delay, 2)),
                    tool_info={"summary": summary},
                )
                yield HarnessEvent(type="text_delta", harness="mock", text_delta=msg)

            steer_suffix = (
                f" Incorporated live steering: {'; '.join(self._steered_notes)}."
                if self._steered_notes
                else ""
            )
            final_note = (
                "Simulated AI step completed: updated implementation, verified unit tests, "
                f"and moved ticket to need_confirm for Admin review.{steer_suffix}"
            )
            moved_tid = self._advance_first_active_task(request.working_dir, final_note)
            done_msg = f"[{moved_tid}] {final_note}" if moved_tid else final_note

            yield HarnessEvent(
                type="result",
                harness="mock",
                session_id=f"mock-{pid.lower()}",
                status="SUCCESS",
                duration_seconds=round(max(0.1, self.step_delay * 3), 2),
                usage=UsageMetrics(input_tokens=1420, output_tokens=485, total_tokens=1905),
                response=done_msg,
            )
        finally:
            self._running = False


if __name__ == "__main__":
    import tempfile

    async def _main() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_dir = Path(tmpdir) / "tasks" / "DEMO-1"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            (tasks_dir / "task.json").write_text(
                json.dumps({"id": "DEMO-1", "status": "in_progress", "assignee": "AI", "comments": []}),
                encoding="utf-8",
            )
            mh = MockHarness(step_delay=0.01)
            assert (await mh.check_health()).available
            events = [e async for e in mh.run_stream(RunRequest(prompt="Execute step", working_dir=tmpdir))]
            assert events[-1].status == "SUCCESS"
            updated = json.loads((tasks_dir / "task.json").read_text(encoding="utf-8"))
            assert updated["status"] == "need_confirm" and updated["assignee"] == "Admin"
        print("harness.mock_harness self-test passed!")

    asyncio.run(_main())
