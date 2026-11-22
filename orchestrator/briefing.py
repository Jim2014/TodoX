"""Zero-latency project status briefings and read-only AI executive summaries."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from harness.registry import create_default_registry
from harness.router import HarnessRouter
from harness.types import HarnessProfile, PermissionPreset, RunRequest

from .event_bus import get_worker_runtime, publish_event
from .models import Task
from .state import state
from .storage import get_data_dir, load_task
from .voice import distill_spoken_briefing


def _tid_sort_key(tid: str) -> tuple[int, str]:
    parts = tid.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return (int(parts[1]), tid)
    return (999999, tid)


def _briefing_cache_path(pid: str) -> str:
    return os.path.join(get_data_dir(), "projects", pid.strip().upper(), "briefing.json")


def _atomic_write_json(path: str, data: Any) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_cached_briefing(pid: str) -> Optional[Dict[str, Any]]:
    """Load cached project briefing JSON if present and valid."""
    path = _briefing_cache_path(pid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_cached_briefing(
    pid: str,
    summary_text: str,
    source: str = "ai_refresh",
    latest_activity_at: str = "",
    harness: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """Persist structured project summary cache alongside project.json."""
    clean_pid = pid.strip().upper()
    payload = {
        "pid": clean_pid,
        "summary_text": (summary_text or "").strip(),
        "source": source,
        "latest_activity_at": latest_activity_at or datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "harness": harness,
        "model": model or "default",
    }
    _atomic_write_json(_briefing_cache_path(clean_pid), payload)
    return payload


def _load_all_subtasks(pid: str) -> List[Task]:
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if not os.path.isdir(tasks_dir):
        return []
    tasks: List[Task] = []
    for tid in os.listdir(tasks_dir):
        if tid.startswith("."):
            continue
        task = load_task(pid, tid)
        if task:
            tasks.append(task)
    tasks.sort(key=lambda t: _tid_sort_key(t.id))
    return tasks


def build_project_briefing(pid: str) -> Optional[Dict[str, Any]]:
    """Build an instant, zero-token briefing combining board metrics, ticket summaries, and cache."""
    clean_pid = pid.strip().upper()
    root = load_task(clean_pid, f"{clean_pid}-0")
    if not root:
        return None

    subtasks = _load_all_subtasks(clean_pid)
    counts: Dict[str, int] = {
        "not_started": 0,
        "in_progress": 0,
        "need_confirm": 0,
        "blocked": 0,
        "complete": 0,
        "failed": 0,
    }
    for t in subtasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    total = len(subtasks)
    completed = counts["complete"]
    progress_pct = round((completed / total) * 100) if total > 0 else (100 if root.status == "complete" else 0)

    attention_tasks: List[Dict[str, Any]] = []
    in_progress_tasks: List[Dict[str, Any]] = []
    latest_ticket_update: Optional[Dict[str, Any]] = None
    latest_activity_at = ""

    for t in [root, *subtasks]:
        last_comment = t.comments[-1] if t.comments else None
        activity_ts = ""
        if last_comment and last_comment.time:
            activity_ts = last_comment.time
        if t.execution_meta and t.execution_meta.last_run_at:
            activity_ts = max(activity_ts, t.execution_meta.last_run_at)

        if t.id != root.id:
            if t.status in ("need_confirm", "blocked", "failed"):
                attention_tasks.append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "assignee": t.assignee,
                        "latest_note": (last_comment.content[:220] if last_comment else t.descriptions[:220]),
                    }
                )
            elif t.status == "in_progress":
                in_progress_tasks.append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "assignee": t.assignee,
                    }
                )

        if last_comment and last_comment.content.strip():
            cand_ts = last_comment.time or ""
            if latest_ticket_update is None or cand_ts >= latest_activity_at:
                latest_ticket_update = {
                    "tid": t.id,
                    "title": t.title,
                    "status": t.status,
                    "assignee": t.assignee,
                    "author": last_comment.author,
                    "time": last_comment.time,
                    "summary": last_comment.content.strip(),
                }
        if activity_ts > latest_activity_at:
            latest_activity_at = activity_ts

    runtime = get_worker_runtime().get(clean_pid)
    paused = state.is_project_paused(clean_pid)
    cached = load_cached_briefing(clean_pid)

    summary_text = ""
    summary_source = "project_goal"
    summary_updated_at = ""
    is_ai_summary_stale = False

    if cached and cached.get("summary_text"):
        cached_src = str(cached.get("source") or "ai_refresh")
        cached_ts = str(cached.get("latest_activity_at") or "")
        if cached_src == "ai_refresh":
            summary_text = cached["summary_text"]
            summary_source = "ai_refresh"
            summary_updated_at = str(cached.get("updated_at") or "")
            is_ai_summary_stale = bool(latest_activity_at and cached_ts and latest_activity_at > cached_ts)
        elif not latest_activity_at or latest_activity_at <= cached_ts:
            summary_text = cached["summary_text"]
            summary_source = cached_src
            summary_updated_at = str(cached.get("updated_at") or "")

    if not summary_text:
        if latest_ticket_update:
            summary_text = latest_ticket_update["summary"]
            summary_source = "latest_ticket"
            summary_updated_at = str(latest_ticket_update.get("time") or "")
        else:
            summary_text = (root.descriptions or "").strip() or "No summary recorded yet."
            summary_source = "project_goal"

    spoken_lead = f"Project {clean_pid}, {root.title}. "
    if total > 0:
        spoken_lead += f"{completed} of {total} tickets complete. "
    else:
        spoken_lead += f"Status is {root.status}. "
    if runtime:
        spoken_lead += f"Worker is currently running {runtime.get('tid', clean_pid)}. "
    elif attention_tasks:
        spoken_lead += f"{len(attention_tasks)} ticket requires your attention. "
    spoken_text = distill_spoken_briefing(spoken_lead + summary_text, max_chars=900)

    return {
        "pid": clean_pid,
        "title": root.title,
        "status": root.status,
        "assignee": root.assignee,
        "running": runtime is not None,
        "runtime": runtime,
        "paused": paused,
        "counts": counts,
        "total_tasks": total,
        "completed_tasks": completed,
        "progress_pct": progress_pct,
        "attention_tasks": attention_tasks,
        "in_progress_tasks": in_progress_tasks,
        "latest_ticket_update": latest_ticket_update,
        "latest_activity_at": latest_activity_at,
        "summary_text": summary_text,
        "summary_source": summary_source,
        "summary_updated_at": summary_updated_at,
        "is_ai_summary_stale": is_ai_summary_stale,
        "spoken_text": spoken_text,
    }


def _build_ai_briefing_prompt(pid: str, root: Task, subtasks: List[Task]) -> str:
    """Build compact read-only prompt to synthesize current project progress."""
    lines = [
        f"You are summarizing TodoX project '{pid}' ({root.title}) for the Admin.",
        f"Root Status: {root.status} | Assignee: {root.assignee}",
        f"Goal: {(root.descriptions or '').strip()[:500]}",
        "",
        "Subtasks:",
    ]
    if not subtasks:
        lines.append("- (No subtasks yet)")
    for t in subtasks[:25]:
        last_c = t.comments[-1].content.strip()[:280] if t.comments else ""
        lines.append(
            f"- {t.id} [{t.status} | {t.assignee}] {t.title}"
            + (f" — Latest note: {last_c}" if last_c else "")
        )

    files_dir = os.path.join(get_data_dir(), "projects", pid, "files")
    if os.path.isdir(files_dir):
        sample_files = sorted(os.listdir(files_dir))[:12]
        if sample_files:
            lines.append("")
            lines.append("Workspace files/: " + ", ".join(sample_files))

    lines.extend(
        [
            "",
            "Write a crisp, high-signal executive briefing in 3-5 short Markdown bullets covering:",
            "1. **Current Progress**: what is finished or actively moving.",
            "2. **Needs Attention**: any blocked, failed, or need_confirm items awaiting Admin action.",
            "3. **Recommended Next Step**: one concrete action to take next.",
            "Do not modify any files or JSON state; reply with only the briefing.",
        ]
    )
    return "\n".join(lines)


async def generate_ai_project_briefing(
    pid: str,
    router: Optional[HarnessRouter] = None,
) -> Optional[Dict[str, Any]]:
    """Run a read-only AI summarization pass and refresh briefing.json."""
    clean_pid = pid.strip().upper()
    root = load_task(clean_pid, f"{clean_pid}-0")
    if not root:
        return None

    subtasks = _load_all_subtasks(clean_pid)
    active_router = router or HarnessRouter(registry=create_default_registry(), state_manager=state)
    base_profile = active_router.resolve_profile(project_task=root)
    read_only_profile = HarnessProfile(
        harness=base_profile.harness,
        model=base_profile.model,
        effort="low",
        permission_preset=PermissionPreset.PLAN,
        session_mode="stateless",
    )

    project_dir = os.path.join(get_data_dir(), "projects", clean_pid)
    prompt = _build_ai_briefing_prompt(clean_pid, root, subtasks)
    result = await active_router.run_step(
        RunRequest(
            prompt=prompt,
            working_dir=project_dir,
            model=read_only_profile.model,
            effort="low",
            permission_preset=PermissionPreset.PLAN,
            timeout_seconds=90,
        ),
        profile=read_only_profile,
    )

    current = build_project_briefing(clean_pid)
    latest_ts = current["latest_activity_at"] if current else ""
    if result.success and (result.response or "").strip():
        save_cached_briefing(
            clean_pid,
            summary_text=result.response.strip(),
            source="ai_refresh",
            latest_activity_at=latest_ts,
            harness=result.harness,
            model=result.model or "default",
        )
        publish_event(
            "board_change",
            pid=clean_pid,
            tid=f"{clean_pid}-0",
            message=f"Generated AI summary for {clean_pid}",
        )
    return build_project_briefing(clean_pid)


if __name__ == "__main__":
    from .storage import create_project, create_task, add_comment, get_default_worker_prompt

    with tempfile.TemporaryDirectory() as tmpdir:
        prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            create_project("BRF", "Briefing Demo", "Build responsive UI", get_default_worker_prompt())
            t1 = create_task("BRF", "BRF-0", "Implement board", "Create columns", assignee="Admin", status="need_confirm")
            assert t1 is not None
            add_comment("BRF", "BRF-1", "AI", "Implemented responsive Kanban board and passed unit tests.")
            brf = build_project_briefing("BRF")
            assert brf is not None
            assert brf["total_tasks"] == 1
            assert len(brf["attention_tasks"]) == 1
            assert brf["latest_ticket_update"]["tid"] == "BRF-1"
            assert "Implement" in brf["summary_text"] or "Implemented" in brf["summary_text"]
        finally:
            if prev is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev
    print("orchestrator.briefing self-test passed!")
