"""Loop/budget guardrails, structured JSONL execution tracing, and project telemetry."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .models import Task
from .storage import get_data_dir

DEFAULT_MAX_STEPS_PER_TASK = 20
DEFAULT_MAX_TOKENS_PER_TASK = 250000
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
MAX_TRACES_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class GuardrailLimits:
    """Resolved execution safety limits (0 disables a check)."""

    max_steps: int = DEFAULT_MAX_STEPS_PER_TASK
    max_tokens: int = DEFAULT_MAX_TOKENS_PER_TASK
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES


def _env_nonneg_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def resolve_guardrail_limits(
    project_task: Optional[Task] = None,
    active_task: Optional[Task] = None,
) -> GuardrailLimits:
    """Resolve guardrail budgets in Task > Project > Env order."""
    max_steps = _env_nonneg_int("MAX_STEPS_PER_TASK", DEFAULT_MAX_STEPS_PER_TASK)
    max_tokens = _env_nonneg_int("MAX_TOKENS_PER_TASK", DEFAULT_MAX_TOKENS_PER_TASK)
    max_failures = _env_nonneg_int("MAX_CONSECUTIVE_FAILURES", DEFAULT_MAX_CONSECUTIVE_FAILURES)

    for task_obj in (project_task, active_task):
        if task_obj is None or task_obj.harness_config is None:
            continue
        hc = task_obj.harness_config
        if hc.max_steps is not None and hc.max_steps >= 0:
            max_steps = int(hc.max_steps)
        if hc.max_tokens is not None and hc.max_tokens >= 0:
            max_tokens = int(hc.max_tokens)

    return GuardrailLimits(
        max_steps=max_steps,
        max_tokens=max_tokens,
        max_consecutive_failures=max_failures,
    )


def check_task_guardrails(
    project_task: Optional[Task],
    active_task: Optional[Task],
) -> Optional[str]:
    """Return a descriptive block reason when a task breaches step, token, or retry budgets."""
    if active_task is None:
        return None
    limits = resolve_guardrail_limits(project_task=project_task, active_task=active_task)
    meta = active_task.execution_meta
    if meta is None:
        return None

    if limits.max_steps > 0 and meta.total_steps >= limits.max_steps:
        return (
            f"Guardrail triggered on {active_task.id}: step budget reached "
            f"({meta.total_steps}/{limits.max_steps} steps)."
        )
    if limits.max_tokens > 0 and meta.total_tokens >= limits.max_tokens:
        return (
            f"Guardrail triggered on {active_task.id}: token budget exceeded "
            f"({meta.total_tokens:,}/{limits.max_tokens:,} tokens)."
        )
    if limits.max_consecutive_failures > 0 and meta.consecutive_failures >= limits.max_consecutive_failures:
        return (
            f"Guardrail triggered on {active_task.id}: consecutive failure circuit-breaker "
            f"({meta.consecutive_failures}/{limits.max_consecutive_failures} failed turns)."
        )
    return None


def get_traces_path() -> str:
    """Return path to DATA_DIR/traces.jsonl."""
    return os.path.join(get_data_dir(), "traces.jsonl")


def _rotate_if_oversized(path: str, max_bytes: int = MAX_TRACES_FILE_BYTES) -> None:
    try:
        if os.path.isfile(path) and os.path.getsize(path) >= max_bytes:
            backup = f"{path}.1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except OSError:
        pass


def append_trace_span(
    pid: str,
    tid: str,
    harness: str,
    model: str,
    status: str,
    error_category: str = "none",
    duration_seconds: float = 0.0,
    total_tokens: int = 0,
    tool_count: int = 0,
    kind: str = "worker_step",
    note: str = "",
) -> Dict[str, Any]:
    """Append a structured execution trace span to DATA_DIR/traces.jsonl."""
    span = {
        "trace_id": uuid.uuid4().hex[:12],
        "span_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "ts_epoch": round(time.time(), 3),
        "kind": kind,
        "pid": (pid or "").strip().upper(),
        "tid": (tid or "").strip().upper(),
        "harness": harness or "unknown",
        "model": model or "default",
        "status": status or "UNKNOWN",
        "error_category": error_category or "none",
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "total_tokens": int(total_tokens or 0),
        "tool_count": int(tool_count or 0),
        "note": (note or "")[:300],
    }
    path = get_traces_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _rotate_if_oversized(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(span, ensure_ascii=False) + "\n")
    return span


def read_recent_traces(limit: int = 50, pid: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read the newest execution spans, optionally filtered by project ID."""
    path = get_traces_path()
    if not os.path.isfile(path):
        return []
    target_pid = pid.strip().upper() if pid else None
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if target_pid and str(item.get("pid") or "").upper() != target_pid:
                    continue
                rows.append(item)
    except OSError:
        return []
    return rows[-limit:]


def compute_project_telemetry(
    pid: str,
    subtasks: Sequence[Task],
    root_task: Optional[Task] = None,
) -> Dict[str, Any]:
    """Aggregate token usage, runtime, success rate, guardrail limits, and recent trace spans."""
    total_steps = 0
    total_tokens = 0
    total_duration = 0.0
    max_failures = 0

    all_tasks: List[Task] = []
    if root_task is not None:
        all_tasks.append(root_task)
    all_tasks.extend(subtasks)

    for task in all_tasks:
        meta = task.execution_meta
        if meta is None:
            continue
        total_steps += int(meta.total_steps or 0)
        total_tokens += int(meta.total_tokens or 0)
        total_duration += float(meta.total_duration_seconds or 0.0)
        max_failures = max(max_failures, int(meta.consecutive_failures or 0))

    traces = read_recent_traces(limit=100, pid=pid)
    if traces:
        success_runs = sum(1 for item in traces if str(item.get("status") or "").upper() == "SUCCESS")
        guardrail_trips = sum(
            1 for item in traces if str(item.get("status") or "").upper() == "GUARDRAIL_BLOCKED"
        )
        failed_runs = sum(
            1
            for item in traces
            if str(item.get("status") or "").upper() not in ("SUCCESS", "CANCELLED")
        )
        completed_runs = success_runs + failed_runs
        success_rate_pct = round((success_runs / completed_runs) * 100) if completed_runs > 0 else 100
    else:
        success_runs = total_steps
        failed_runs = sum(1 for t in subtasks if t.status in ("failed", "blocked"))
        guardrail_trips = 0
        total_observed = success_runs + failed_runs
        success_rate_pct = round((success_runs / total_observed) * 100) if total_observed > 0 else 100

    limits = resolve_guardrail_limits(project_task=root_task)
    return {
        "pid": pid.strip().upper(),
        "total_steps": total_steps,
        "total_tokens": total_tokens,
        "total_duration_seconds": round(total_duration, 2),
        "success_runs": success_runs,
        "failed_runs": failed_runs,
        "guardrail_trips": guardrail_trips,
        "success_rate_pct": success_rate_pct,
        "max_consecutive_failures_seen": max_failures,
        "limits": {
            "max_steps": limits.max_steps,
            "max_tokens": limits.max_tokens,
            "max_consecutive_failures": limits.max_consecutive_failures,
        },
        "recent_traces": traces[-20:],
    }


if __name__ == "__main__":
    import tempfile
    from .models import ExecutionMeta, HarnessConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            proj = Task(id="G-0", harness_config=HarnessConfig(max_steps=5, max_tokens=1000))
            sub = Task(id="G-1", execution_meta=ExecutionMeta(total_steps=5, total_tokens=400))
            msg = check_task_guardrails(proj, sub)
            assert msg and "step budget" in msg
            sub_ok = Task(id="G-2", execution_meta=ExecutionMeta(total_steps=2, total_tokens=500))
            assert check_task_guardrails(proj, sub_ok) is None
            append_trace_span("G", "G-2", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=1.2, total_tokens=500)
            tel = compute_project_telemetry("G", [sub, sub_ok], proj)
            assert tel["total_steps"] == 7 and tel["total_tokens"] == 900
        finally:
            if prev is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev
    print("orchestrator.guardrails self-test passed!")
