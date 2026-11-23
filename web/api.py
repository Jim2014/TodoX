"""REST and Server-Sent Events (SSE) API endpoints for the TodoX Web Panel."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from harness.registry import create_default_registry
from harness.router import HarnessRouter
from orchestrator.briefing import build_project_briefing, generate_ai_project_briefing
from orchestrator.chat_router import handle_project_chat
from orchestrator.event_bus import (
    enqueue_control_command,
    get_worker_runtime,
    publish_event,
    read_recent_events,
    stream_events_async,
)
from orchestrator.guardrails import compute_project_telemetry, read_recent_traces
from orchestrator.models import HarnessConfig, Task
from orchestrator.state import state
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
    save_task,
    update_comment,
    update_project,
    update_task,
)
from orchestrator.voice import synthesize_voice_mp3, transcribe_voice
from web.models import (
    ApproveRequest,
    CommentCreate,
    CommentUpdate,
    ProjectChatRequest,
    ProjectCreate,
    ProjectUpdate,
    SteerRequest,
    SystemStateUpdate,
    TaskCreate,
    TaskUpdate,
)

router = APIRouter()


def _tid_sort_key(tid: str) -> tuple[int, str]:
    """Sort task IDs numerically (e.g. GO-2 before GO-10)."""
    parts = tid.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return (int(parts[1]), tid)
    return (999999, tid)


def _apply_harness_config(task: Task, raw_cfg: Optional[Dict[str, Any]]) -> None:
    """Apply or clear optional HarnessConfig overrides on a Task model."""
    if raw_cfg is None:
        return
    if raw_cfg.get("clear"):
        task.harness_config = None
        return

    cleaned: Dict[str, Any] = {}
    for key in ("harness", "model", "effort", "permission_preset", "session_mode"):
        val = raw_cfg.get(key)
        if val is not None and str(val).strip() != "":
            cleaned[key] = str(val).strip()

    for int_key in ("max_steps", "max_tokens"):
        val = raw_cfg.get(int_key)
        if val is not None and str(val).strip() != "":
            try:
                parsed = int(val)
                if parsed >= 0:
                    cleaned[int_key] = parsed
            except (TypeError, ValueError):
                pass

    task.harness_config = HarnessConfig(**cleaned) if cleaned else None


def _load_project_subtasks(pid: str) -> List[Task]:
    """Load and numerically sort all subtasks in a project."""
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if not os.path.isdir(tasks_dir):
        return []
    items: List[Task] = []
    for tid in sorted(os.listdir(tasks_dir), key=_tid_sort_key):
        if tid.startswith("."):
            continue
        t = load_task(pid, tid)
        if t:
            items.append(t)
    items.sort(key=lambda item: _tid_sort_key(item.id))
    return items


def _build_file_tree(root_dir: Path, rel_prefix: str = "") -> List[Dict[str, Any]]:
    """Build recursive directory/file metadata tree for project files tab."""
    cur_dir = root_dir / rel_prefix if rel_prefix else root_dir
    if not cur_dir.is_dir():
        return []

    nodes: List[Dict[str, Any]] = []
    try:
        entries = sorted(cur_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []

    for entry in entries:
        if entry.name.startswith(".") or entry.name in ("__pycache__",):
            continue
        rel_path = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
        if entry.is_dir():
            nodes.append(
                {
                    "name": entry.name,
                    "path": rel_path,
                    "type": "dir",
                    "children": _build_file_tree(root_dir, rel_path),
                }
            )
        elif entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            nodes.append(
                {
                    "name": entry.name,
                    "path": rel_path,
                    "type": "file",
                    "ext": entry.suffix.lower(),
                    "size": size,
                }
            )
    return nodes


@router.get("/api/projects")
def api_list_projects():
    """Return enriched project list with task status counts, runtime activity, and state."""
    st = state.get_state()
    paused_set = set(st.get("paused_projects", []))
    runtime = get_worker_runtime()
    harness_router = HarnessRouter(registry=create_default_registry(), state_manager=state)

    project_list: List[Dict[str, Any]] = []
    for pid, summary in sorted(load_projects().items()):
        root = load_task(pid, f"{pid}-0")
        subtasks = _load_project_subtasks(pid)
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
        eff_profile = harness_router.resolve_profile(project_task=root).to_dict()
        project_list.append(
            {
                "id": pid,
                "title": root.title if root else summary.title,
                "status": root.status if root else summary.status,
                "assignee": root.assignee if root else summary.assignee,
                "descriptions": root.descriptions if root else "",
                "task_counts": counts,
                "total_tasks": len(subtasks),
                "completed_tasks": counts["complete"],
                "running": pid in runtime,
                "runtime": runtime.get(pid),
                "paused": pid in paused_set,
                "effective_profile": eff_profile,
                "execution_meta": (
                    root.execution_meta.model_dump() if root and root.execution_meta else None
                ),
            }
        )

    return {
        "projects": project_list,
        "system_state": st,
        "runtime": runtime,
    }


@router.get("/api/projects/{pid}")
def api_get_project(pid: str):
    """Return complete project root task, sorted subtasks, file tree, and live runtime."""
    clean_pid = pid.strip().upper()
    root = load_task(clean_pid, f"{clean_pid}-0")
    if not root:
        raise HTTPException(status_code=404, detail="Project not found")

    st = state.get_state()
    runtime = get_worker_runtime()
    harness_router = HarnessRouter(registry=create_default_registry(), state_manager=state)
    subtasks = _load_project_subtasks(clean_pid)
    project_dir = Path(get_data_dir()) / "projects" / clean_pid
    files_tree = _build_file_tree(project_dir)

    task_profiles = {
        t.id: harness_router.resolve_profile(project_task=root, active_task=t).to_dict()
        for t in subtasks
    }

    return {
        "pid": clean_pid,
        "project": root.model_dump(),
        "tasks": [t.model_dump() for t in subtasks],
        "task_profiles": task_profiles,
        "files": files_tree,
        "running": clean_pid in runtime,
        "runtime": runtime.get(clean_pid),
        "paused": clean_pid in set(st.get("paused_projects", [])),
        "effective_profile": harness_router.resolve_profile(project_task=root).to_dict(),
        "briefing": build_project_briefing(clean_pid),
        "telemetry": compute_project_telemetry(clean_pid, subtasks, root),
        "system_state": st,
    }


@router.get("/api/projects/{pid}/traces")
def api_project_traces(pid: str, limit: int = Query(default=40, ge=1, le=200)):
    """Return structured execution traces and guardrail telemetry for one project."""
    clean_pid = pid.strip().upper()
    root = load_task(clean_pid, f"{clean_pid}-0")
    if not root:
        raise HTTPException(status_code=404, detail="Project not found")
    subtasks = _load_project_subtasks(clean_pid)
    return {
        "pid": clean_pid,
        "telemetry": compute_project_telemetry(clean_pid, subtasks, root),
        "traces": read_recent_traces(limit=limit, pid=clean_pid),
    }


@router.get("/api/tasks/{pid}/{tid}")
def api_get_task(pid: str, tid: str):
    """Return one task payload with its resolved effective harness profile."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    root = load_task(clean_pid, f"{clean_pid}-0")
    task = load_task(clean_pid, clean_tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    harness_router = HarnessRouter(registry=create_default_registry(), state_manager=state)
    eff_profile = harness_router.resolve_profile(project_task=root, active_task=task).to_dict()
    runtime = get_worker_runtime()
    return {
        "pid": clean_pid,
        "task": task.model_dump(),
        "effective_profile": eff_profile,
        "runtime": runtime.get(clean_pid),
    }


@router.post("/api/projects")
def api_create_project(data: ProjectCreate):
    """Create a new project workspace and root ticket."""
    pid = data.pid.strip().upper()
    seed_prompt = get_default_worker_prompt()
    create_project(pid, data.title, data.desc, seed_prompt)
    publish_event("board_change", pid=pid, tid=f"{pid}-0", message=f"Created project {pid}")
    return {"status": "success", "pid": pid}


@router.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    """Delete a project workspace and all child tasks."""
    clean_pid = pid.strip().upper()
    if delete_project(clean_pid):
        publish_event("board_change", pid=clean_pid, message=f"Deleted project {clean_pid}")
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")


@router.put("/api/projects/{pid}")
def api_update_project(pid: str, data: ProjectUpdate):
    """Update root project fields and optional harness overrides."""
    clean_pid = pid.strip().upper()
    raw = data.model_dump()
    harness_cfg = raw.pop("harness_config", None)
    updates = {k: v for k, v in raw.items() if v is not None}

    updated = update_project(clean_pid, **updates) if updates else load_task(clean_pid, f"{clean_pid}-0")
    if not updated:
        raise HTTPException(status_code=404, detail="Not found")

    if harness_cfg is not None:
        _apply_harness_config(updated, harness_cfg)
        save_task(clean_pid, updated)

    publish_event("board_change", pid=clean_pid, tid=f"{clean_pid}-0", message=f"Updated project {clean_pid}")
    return {"status": "success"}


@router.post("/api/tasks/{pid}/{parent_tid}")
def api_create_task(pid: str, parent_tid: str, data: TaskCreate):
    """Create a new subtask ticket under parent_tid."""
    clean_pid = pid.strip().upper()
    clean_ptid = parent_tid.strip().upper()
    allowed_statuses = {"not_started", "in_progress", "blocked", "need_confirm", "complete", "failed"}
    init_status = data.status if data.status in allowed_statuses else "not_started"
    t = create_task(clean_pid, clean_ptid, data.title, data.desc, data.assignee, status=init_status)
    if t:
        if data.harness_config is not None:
            _apply_harness_config(t, data.harness_config)
            save_task(clean_pid, t)
        publish_event("board_change", pid=clean_pid, tid=t.id, message=f"Created task {t.id}")
        return {"status": "success", "tid": t.id}
    raise HTTPException(status_code=404, detail="Parent not found")


@router.delete("/api/tasks/{pid}/{tid}")
def api_delete_task(pid: str, tid: str):
    """Delete a subtask (or entire project if tid is PID-0)."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    if delete_task(clean_pid, clean_tid):
        publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Deleted task {clean_tid}")
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")


@router.put("/api/tasks/{pid}/{tid}")
def api_update_task(pid: str, tid: str, data: TaskUpdate):
    """Update subtask fields, column status, assignee, or harness overrides."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    raw = data.model_dump()
    harness_cfg = raw.pop("harness_config", None)
    updates = {k: v for k, v in raw.items() if v is not None}

    updated = update_task(clean_pid, clean_tid, **updates) if updates else load_task(clean_pid, clean_tid)
    if not updated:
        raise HTTPException(status_code=404, detail="Not found")

    if harness_cfg is not None:
        _apply_harness_config(updated, harness_cfg)
        save_task(clean_pid, updated)

    publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Updated task {clean_tid}")
    return {"status": "success"}


@router.post("/api/comments/{pid}/{tid}")
def api_create_comment(pid: str, tid: str, data: CommentCreate):
    """Append an Admin comment to a task or project."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    if add_comment(clean_pid, clean_tid, "Admin", data.msg):
        publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Commented on {clean_tid}")
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")


@router.delete("/api/comments/{pid}/{tid}/{index}")
def api_delete_comment(pid: str, tid: str, index: int):
    """Delete a comment by index."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    if delete_comment(clean_pid, clean_tid, index):
        publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Removed comment on {clean_tid}")
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")


@router.put("/api/comments/{pid}/{tid}/{index}")
def api_update_comment(pid: str, tid: str, index: int, data: CommentUpdate):
    """Update comment text at index."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    if update_comment(clean_pid, clean_tid, index, data.msg):
        publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Updated comment on {clean_tid}")
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")


@router.post("/api/projects/{pid}/run")
def api_run_project(pid: str):
    """Assign project root to AI (in_progress) and trigger immediate execution."""
    clean_pid = pid.strip().upper()
    updated = update_project(clean_pid, status="in_progress", assignee="AI")
    if not updated:
        raise HTTPException(status_code=404, detail="Project not found")
    state.resume_project(clean_pid)
    enqueue_control_command("trigger", clean_pid)
    publish_event("control_action", pid=clean_pid, tid=f"{clean_pid}-0", message="Queued immediate AI run")
    publish_event("board_change", pid=clean_pid, tid=f"{clean_pid}-0", message=f"Assigned {clean_pid} to AI")
    return {"status": "queued", "pid": clean_pid, "project": updated.model_dump()}


@router.post("/api/tasks/{pid}/{tid}/run")
def api_run_task(pid: str, tid: str):
    """Assign task to AI (in_progress) and trigger project worker immediately."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    updated = update_task(clean_pid, clean_tid, status="in_progress", assignee="AI")
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    state.resume_project(clean_pid)
    enqueue_control_command("trigger", clean_pid)
    publish_event("control_action", pid=clean_pid, tid=clean_tid, message=f"Queued AI run for {clean_tid}")
    publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Assigned {clean_tid} to AI")
    return {"status": "queued", "pid": clean_pid, "tid": clean_tid, "task": updated.model_dump()}


@router.post("/api/tasks/{pid}/{tid}/approve")
def api_approve_task(pid: str, tid: str, data: Optional[ApproveRequest] = Body(default=None)):
    """Mark a task complete and hand back to Admin with an optional note."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    updated = update_task(clean_pid, clean_tid, status="complete", assignee="Admin")
    if not updated:
        raise HTTPException(status_code=404, detail="Task not found")
    if data and data.comment and data.comment.strip():
        updated = add_comment(clean_pid, clean_tid, "Admin", data.comment.strip()) or updated
    publish_event("board_change", pid=clean_pid, tid=clean_tid, message=f"Approved {clean_tid}")
    return {"status": "success", "pid": clean_pid, "tid": clean_tid, "task": updated.model_dump()}


@router.post("/api/workers/{pid}/stop")
def api_stop_worker(pid: str):
    """Queue a stop signal for an active project worker."""
    clean_pid = pid.strip().upper()
    enqueue_control_command("stop", clean_pid)
    publish_event("control_action", pid=clean_pid, message=f"Stop signal queued for {clean_pid}")
    return {"status": "queued", "action": "stop", "pid": clean_pid}


@router.post("/api/workers/{pid}/steer")
def api_steer_worker(pid: str, data: SteerRequest):
    """Queue a mid-flight steering instruction or Admin note for a project worker."""
    clean_pid = pid.strip().upper()
    msg = (data.msg or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Steering message cannot be empty")
    enqueue_control_command("steer", clean_pid, msg)
    publish_event("control_action", pid=clean_pid, message=f"Steering queued: {msg[:100]}")
    return {"status": "queued", "action": "steer", "pid": clean_pid}


@router.get("/api/projects/{pid}/briefing")
def api_get_project_briefing(pid: str):
    """Return zero-latency project briefing combining board stats, latest ticket note, and cache."""
    clean_pid = pid.strip().upper()
    briefing = build_project_briefing(clean_pid)
    if not briefing:
        raise HTTPException(status_code=404, detail="Project not found")
    return briefing


@router.post("/api/projects/{pid}/briefing/refresh")
async def api_refresh_project_briefing(pid: str):
    """Run read-only AI executive summarization pass for a project and refresh cache."""
    clean_pid = pid.strip().upper()
    if not load_task(clean_pid, f"{clean_pid}-0"):
        raise HTTPException(status_code=404, detail="Project not found")
    briefing = await generate_ai_project_briefing(clean_pid)
    return briefing


@router.get("/api/projects/{pid}/briefing/audio")
async def api_project_briefing_audio(pid: str):
    """Synthesize spoken MP3 briefing for in-browser playback."""
    clean_pid = pid.strip().upper()
    briefing = build_project_briefing(clean_pid)
    if not briefing:
        raise HTTPException(status_code=404, detail="Project not found")
    spoken = str(briefing.get("spoken_text") or briefing.get("summary_text") or "").strip()
    audio_path = await synthesize_voice_mp3(spoken)
    if not audio_path or not audio_path.is_file():
        raise HTTPException(status_code=503, detail="Voice synthesis unavailable")
    return FileResponse(audio_path, media_type="audio/mpeg", filename=f"{clean_pid}_briefing.mp3")


@router.post("/api/projects/{pid}/chat")
async def api_project_chat(pid: str, data: ProjectChatRequest):
    """Route a project-scoped natural-language command into briefing, ticket creation, or worker control."""
    clean_pid = pid.strip().upper()
    if not load_task(clean_pid, f"{clean_pid}-0"):
        raise HTTPException(status_code=404, detail="Project not found")
    if not (data.msg or "").strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return await handle_project_chat(clean_pid, data.msg, source="web", auto_run=data.auto_run)


@router.post("/api/projects/{pid}/voice")
async def api_project_voice(pid: str, file: UploadFile = File(...)):
    """Transcribe an uploaded browser voice recording and execute it as a project command."""
    clean_pid = pid.strip().upper()
    if not load_task(clean_pid, f"{clean_pid}-0"):
        raise HTTPException(status_code=404, detail="Project not found")

    voice_dir = Path(get_data_dir()) / "runtime" / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "recording.webm").suffix or ".webm"
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    fd, temp_name = tempfile.mkstemp(dir=str(voice_dir), prefix=f"web_{clean_pid}_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
        transcript, detected_lang = await transcribe_voice(temp_name)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    if not transcript.strip():
        return {
            "ok": False,
            "pid": clean_pid,
            "transcript": "",
            "language": detected_lang,
            "reply": "Could not detect speech in the recording. Please try speaking closer to the microphone.",
        }

    result = await handle_project_chat(clean_pid, transcript, source="web_voice", auto_run=True)
    return {
        "transcript": transcript,
        "language": detected_lang,
        **result,
    }


@router.get("/api/system_state")
def api_system_state():
    """Return global orchestrator configuration state."""
    return state.get_state()


@router.post("/api/system_state")
def api_update_system_state(data: SystemStateUpdate):
    """Update global watcher, harness, model, effort, or permission settings."""
    if data.watcher_paused is not None:
        state.set_watcher_paused(data.watcher_paused)
    if data.default_harness is not None and data.default_harness.strip():
        h_name = data.default_harness.strip().lower()
        reg = create_default_registry()
        if not reg.has(h_name):
            raise HTTPException(status_code=400, detail=f"Unknown harness '{h_name}'")
        state.set_harness(h_name, data.default_model)
    elif data.default_model is not None and data.default_model.strip():
        cur_h = state.get_current_harness()
        state.set_harness(cur_h, data.default_model.strip())
    if data.default_effort is not None and data.default_effort.strip():
        if not state.set_effort(data.default_effort.strip()):
            raise HTTPException(status_code=400, detail="Effort must be one of low, medium, high, xhigh")
    if data.default_permission_preset is not None and data.default_permission_preset.strip():
        state.set_permission_preset(data.default_permission_preset.strip())
    if data.pause_project:
        state.pause_project(data.pause_project)
    if data.resume_project:
        state.resume_project(data.resume_project)

    publish_event("board_change", message="Updated global orchestrator settings")
    return state.get_state()


@router.get("/api/harnesses")
async def api_list_harnesses():
    """Return registered harnesses, discovered models per harness, and active defaults."""
    reg = create_default_registry()
    st = state.get_state()
    harnesses: List[Dict[str, Any]] = []
    models_by_harness: Dict[str, List[Dict[str, Any]]] = {}

    for name in reg.list_names():
        models_list: List[Dict[str, Any]] = []
        available = True
        try:
            instance = reg.create(name)
            discovered = await instance.list_models(force_refresh=False)
            for m in discovered:
                models_list.append(
                    {
                        "id": m.id,
                        "raw_id": m.raw_id or m.id,
                        "name": m.name,
                        "icon": m.icon,
                        "description": m.description,
                    }
                )
        except Exception:
            available = False
        harnesses.append({"name": name, "available": available})
        models_by_harness[name] = models_list

    return {
        "harnesses": harnesses,
        "models": models_by_harness,
        "fallback_chain": st.get("fallback_chain", []),
        "active": {
            "harness": st.get("default_harness", "agy"),
            "model": st.get("default_model", "default"),
            "effort": st.get("default_effort", "high"),
            "permission_preset": st.get("default_permission_preset", "auto"),
        },
    }


@router.get("/api/events/recent")
def api_recent_events(
    pid: Optional[str] = Query(default=None),
    limit: int = Query(default=80, ge=1, le=500),
):
    """Return recent live worker and board events plus current active runtime."""
    return {
        "events": read_recent_events(limit=limit, pid=pid),
        "runtime": get_worker_runtime(),
        "system_state": state.get_state(),
    }


@router.get("/api/events/stream")
async def api_events_stream(
    pid: Optional[str] = Query(default=None),
    once: bool = Query(default=False),
):
    """Stream real-time worker tool, token, and board-change events over SSE."""
    return StreamingResponse(
        stream_events_async(
            pid=pid,
            once=once,
            state_snapshot_fn=state.get_state,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    assert _tid_sort_key("GO-2") < _tid_sort_key("GO-10")
    t = Task(id="DEMO-0")
    _apply_harness_config(t, {"harness": "agy", "model": "gemini-3.8-flash", "effort": "high"})
    assert t.harness_config is not None and t.harness_config.model == "gemini-3.8-flash"
    _apply_harness_config(t, {"clear": True})
    assert t.harness_config is None
    print("web.api self-test passed!")
