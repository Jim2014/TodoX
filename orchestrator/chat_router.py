"""Unified natural-language & voice command router shared across Telegram and Web UI."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from harness.registry import create_default_registry
from harness.router import HarnessRouter
from harness.types import HarnessProfile, PermissionPreset, RunRequest

from .briefing import build_project_briefing, generate_ai_project_briefing
from .event_bus import enqueue_control_command, get_worker_runtime, publish_event
from .state import SystemState, state as default_state
from .storage import add_comment, create_task, load_task, update_project, update_task
from .voice import detect_spoken_language, distill_spoken_briefing

_TID_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{0,20}-\d+)\b")


def _extract_json_dict(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from model output (tolerating Markdown fences)."""
    cleaned = (raw_text or "").strip()
    if not cleaned:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned, flags=re.IGNORECASE)
    candidate = fenced.group(1) if fenced else cleaned
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        brace_match = re.search(r"\{[\s\S]*\}", cleaned)
        if brace_match:
            try:
                parsed = json.loads(brace_match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _load_project_tasks_brief(pid: str) -> List[Dict[str, Any]]:
    """Return compact task summaries sorted cleanly for intent routing."""
    tasks_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # fallback replaced by storage call
    )
    from .storage import get_data_dir

    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if not os.path.isdir(tasks_dir):
        return []
    rows: List[Dict[str, Any]] = []
    for tid in sorted(os.listdir(tasks_dir)):
        if tid.startswith("."):
            continue
        t = load_task(pid, tid)
        if t:
            rows.append({"id": t.id, "title": t.title, "status": t.status, "assignee": t.assignee})
    return rows


async def _trigger_project(pid: str, watcher: Any, state_manager: SystemState) -> None:
    """Trigger immediate execution via in-process watcher or cross-process event bus."""
    if state_manager.is_project_paused(pid):
        state_manager.resume_project(pid)
    if watcher is not None and hasattr(watcher, "trigger_project"):
        await watcher.trigger_project(pid)
    else:
        enqueue_control_command("trigger", pid)


async def _stop_project(pid: str, watcher: Any) -> bool:
    """Stop active project worker via in-process watcher or cross-process control queue."""
    if watcher is not None and hasattr(watcher, "stop_project"):
        return bool(await watcher.stop_project(pid))
    enqueue_control_command("stop", pid)
    publish_event("control_action", pid=pid, message=f"Queued stop signal for {pid}")
    return True


async def _steer_project(pid: str, msg: str, watcher: Any) -> str:
    """Steer active worker or queue instruction via cross-process control queue."""
    if watcher is not None and hasattr(watcher, "steer_project"):
        ok, reason = await watcher.steer_project(pid, msg)
        if ok:
            return "steered"
    enqueue_control_command("steer", pid, msg)
    publish_event("control_action", pid=pid, message=f"Queued steer instruction: {msg[:100]}")
    return "queued_steer"


def _deterministic_fast_path(pid: str, message: str) -> Optional[Dict[str, Any]]:
    """Match unambiguous crisp English or CJK commands with zero LLM latency."""
    low = message.strip().lower()
    trimmed = low.rstrip("?.! \u3002\uff1f\uff01")
    if trimmed in (
        "status",
        "summary",
        "brief",
        "briefing",
        "progress",
        "what's the status",
        "whats the status",
        "what is the status",
        "what is the current status",
        "what's the current status",
        "what is the progress",
        "\u603b\u7ed3",
        "\u603b\u7ed3\u4e00\u4e0b",
        "\u603b\u7ed3\u4e00\u4e0b\u5f53\u524d\u9879\u76ee",
        "\u603b\u7ed3\u4e00\u4e0b\u5f53\u524d\u9879\u76ee\u8fdb\u5ea6",
        "\u5f53\u524d\u8fdb\u5ea6",
        "\u5f53\u524d\u72b6\u6001",
    ):
        return {"intent": "briefing"}
    if trimmed in ("ai summary", "refresh summary", "regenerate summary", "ai briefing", "full summary"):
        return {"intent": "ai_summary"}
    if trimmed in ("stop", "stop worker", "cancel", "abort"):
        return {"intent": "stop"}
    if trimmed in ("pause", "pause project"):
        return {"intent": "pause"}
    if trimmed in ("resume", "resume project"):
        return {"intent": "resume"}

    m_steer = re.match(r"^steer\s+(.+)$", message.strip(), flags=re.IGNORECASE | re.DOTALL)
    if m_steer:
        return {"intent": "steer", "comment": m_steer.group(1).strip()}

    m_app = re.match(r"^(?:approve|complete|confirm|done)\s+([A-Za-z][A-Za-z0-9_-]*-\d+)$", trimmed)
    if m_app:
        return {"intent": "approve", "tid": m_app.group(1).upper()}

    m_run = re.match(r"^(?:run|start|retry)\s+([A-Za-z][A-Za-z0-9_-]*-\d+)$", trimmed)
    if m_run:
        return {"intent": "run_task", "tid": m_run.group(1).upper()}

    return None


async def _default_llm_classify(
    pid: str,
    message: str,
    tasks_summary: List[Dict[str, Any]],
    is_running: bool,
    router: HarnessRouter,
) -> Optional[Dict[str, Any]]:
    """Use read-only HarnessRouter turn to classify multilingual natural-language intent."""
    root = load_task(pid, f"{pid}-0")
    base_profile = router.resolve_profile(project_task=root)
    profile = HarnessProfile(
        harness=base_profile.harness,
        model=base_profile.model,
        effort="low",
        permission_preset=PermissionPreset.PLAN,
        session_mode="stateless",
    )
    tickets_txt = "\n".join(
        f"- {row['id']} [{row['status']} | {row['assignee']}]: {row['title']}"
        for row in tasks_summary[:25]
    ) or "- (none yet)"

    prompt = (
        f"You are the command router for TodoX project '{pid}' ({root.title if root else pid}).\n"
        f"Worker currently running: {is_running}\n"
        f"Existing tickets:\n{tickets_txt}\n\n"
        f"User message (may be in English, Chinese, or another language):\n{message}\n\n"
        "Classify the user's intent into strict JSON ONLY (no markdown prose outside JSON):\n"
        "{\n"
        '  "intent": "briefing | ai_summary | create_and_run | comment_and_run | approve | stop | steer",\n'
        '  "tid": "Matching existing ticket ID like PID-3 or empty string",\n'
        '  "title": "Concise English ticket title (when create_and_run)",\n'
        '  "description": "Clear English ticket specification preserving all details from the user message",\n'
        '  "comment": "Concise instruction/feedback to attach (when comment_and_run or approve)",\n'
        '  "reply": "Short friendly confirmation written in the EXACT same language as the user message"\n'
        "}\n"
        "Rules:\n"
        "- Use 'briefing' if asking about current progress or what changed; use 'ai_summary' if explicitly requesting a fresh AI synthesis.\n"
        "- Use 'approve' if confirming/approving an existing need_confirm ticket.\n"
        "- Use 'comment_and_run' if giving feedback or follow-up work on a clearly referenced existing ticket.\n"
        "- Use 'steer' only if asking to adjust an actively running worker in real time without specifying a new ticket.\n"
        "- Use 'create_and_run' for any new feature, bugfix, or work item.\n"
    )
    from .storage import get_data_dir

    project_dir = os.path.join(get_data_dir(), "projects", pid)
    res = await router.run_step(
        RunRequest(
            prompt=prompt,
            working_dir=project_dir,
            model=profile.model,
            effort="low",
            permission_preset=PermissionPreset.PLAN,
            timeout_seconds=35,
        ),
        profile=profile,
    )
    if not res.success or not res.response:
        return None
    return _extract_json_dict(res.response)


async def handle_project_chat(
    pid: str,
    message: str,
    source: str = "web",
    auto_run: bool = True,
    router: Optional[HarnessRouter] = None,
    watcher: Any = None,
    state_manager: Optional[SystemState] = None,
    intent_classifier: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Route a project-scoped text or voice message into briefing, ticket creation, or worker control."""
    clean_pid = pid.strip().upper()
    clean_msg = (message or "").strip()
    if not clean_msg:
        return {"ok": False, "pid": clean_pid, "reply": "Message cannot be empty.", "spoken_reply": ""}

    root = load_task(clean_pid, f"{clean_pid}-0")
    if not root:
        return {"ok": False, "pid": clean_pid, "reply": f"Project {clean_pid} not found.", "spoken_reply": ""}

    active_state = state_manager or default_state
    active_router = router or HarnessRouter(registry=create_default_registry(), state_manager=active_state)
    runtime_entry = get_worker_runtime().get(clean_pid)
    is_running = bool(
        runtime_entry
        or (watcher is not None and hasattr(watcher, "is_project_running") and watcher.is_project_running(clean_pid))
    )
    tasks_summary = _load_project_tasks_brief(clean_pid)

    decision = _deterministic_fast_path(clean_pid, clean_msg)
    if decision is None:
        if intent_classifier is not None:
            maybe = intent_classifier(clean_pid, clean_msg, tasks_summary, is_running)
            decision = await maybe if inspect.isawaitable(maybe) else maybe
        else:
            decision = await _default_llm_classify(clean_pid, clean_msg, tasks_summary, is_running, active_router)

    if not isinstance(decision, dict) or not decision.get("intent"):
        # Resilient fallback when offline or classifier fails
        m_tid = _TID_RE.search(clean_msg)
        if m_tid and load_task(clean_pid, m_tid.group(1).upper()):
            decision = {
                "intent": "comment_and_run",
                "tid": m_tid.group(1).upper(),
                "comment": clean_msg,
            }
        elif is_running:
            decision = {"intent": "steer", "comment": clean_msg}
        else:
            first_line = clean_msg.splitlines()[0].strip()
            title = first_line[:69] + "..." if len(first_line) > 72 else first_line
            decision = {
                "intent": "create_and_run",
                "title": title or "New request",
                "description": clean_msg,
            }

    intent = str(decision.get("intent") or "create_and_run").strip().lower()
    target_tid = str(decision.get("tid") or "").strip().upper()
    reply_hint = str(decision.get("reply") or "").strip()

    if intent == "briefing":
        briefing = build_project_briefing(clean_pid)
        reply = briefing["summary_text"] if briefing else ""
        spoken = briefing["spoken_text"] if briefing else ""
        return {
            "ok": True,
            "action": "briefing",
            "pid": clean_pid,
            "tid": "",
            "reply": reply,
            "spoken_reply": spoken,
            "briefing": briefing,
        }

    if intent == "ai_summary":
        briefing = await generate_ai_project_briefing(clean_pid, router=active_router)
        reply = briefing["summary_text"] if briefing else "Unable to generate AI summary right now."
        spoken = briefing["spoken_text"] if briefing else distill_spoken_briefing(reply)
        return {
            "ok": True,
            "action": "ai_summary",
            "pid": clean_pid,
            "tid": "",
            "reply": reply,
            "spoken_reply": spoken,
            "briefing": briefing,
        }

    if intent == "stop":
        await _stop_project(clean_pid, watcher)
        reply = reply_hint or f"Sent stop signal to {clean_pid}."
        return {
            "ok": True,
            "action": "stop",
            "pid": clean_pid,
            "tid": "",
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "pause":
        active_state.pause_project(clean_pid)
        publish_event("board_change", pid=clean_pid, message=f"Paused {clean_pid}")
        reply = reply_hint or f"Paused scheduling for {clean_pid}."
        return {
            "ok": True,
            "action": "pause",
            "pid": clean_pid,
            "tid": "",
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "resume":
        active_state.resume_project(clean_pid)
        await _trigger_project(clean_pid, watcher, active_state)
        publish_event("board_change", pid=clean_pid, message=f"Resumed {clean_pid}")
        reply = reply_hint or f"Resumed scheduling for {clean_pid}."
        return {
            "ok": True,
            "action": "resume",
            "pid": clean_pid,
            "tid": "",
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "steer":
        steer_text = str(decision.get("comment") or clean_msg).strip()
        mode = await _steer_project(clean_pid, steer_text, watcher)
        reply = reply_hint or (
            f"Steered running worker on {clean_pid}." if mode == "steered" else f"Queued guidance for {clean_pid}."
        )
        return {
            "ok": True,
            "action": "steer",
            "pid": clean_pid,
            "tid": runtime_entry.get("tid", "") if runtime_entry else "",
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "approve":
        if not target_tid:
            for row in tasks_summary:
                if row["status"] == "need_confirm":
                    target_tid = row["id"]
                    break
        if not target_tid:
            target_tid = f"{clean_pid}-0"
        updated = update_task(clean_pid, target_tid, status="complete", assignee="Admin")
        if not updated:
            return {"ok": False, "pid": clean_pid, "reply": f"Ticket {target_tid} not found.", "spoken_reply": ""}
        note = str(decision.get("comment") or "").strip()
        if note:
            add_comment(clean_pid, target_tid, "Admin", note)
        publish_event("board_change", pid=clean_pid, tid=target_tid, message=f"Approved {target_tid}")
        reply = reply_hint or f"Approved and marked {target_tid} complete."
        return {
            "ok": True,
            "action": "approve",
            "pid": clean_pid,
            "tid": target_tid,
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "task": updated.model_dump(),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "run_task" and target_tid:
        updated = update_task(clean_pid, target_tid, status="in_progress", assignee="AI")
        if not updated:
            return {"ok": False, "pid": clean_pid, "reply": f"Ticket {target_tid} not found.", "spoken_reply": ""}
        await _trigger_project(clean_pid, watcher, active_state)
        publish_event("board_change", pid=clean_pid, tid=target_tid, message=f"Assigned {target_tid} to AI")
        reply = reply_hint or f"Assigned {target_tid} to AI and triggered execution."
        return {
            "ok": True,
            "action": "run_task",
            "pid": clean_pid,
            "tid": target_tid,
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "task": updated.model_dump(),
            "briefing": build_project_briefing(clean_pid),
        }

    if intent == "comment_and_run" and target_tid and load_task(clean_pid, target_tid):
        comment_body = str(decision.get("comment") or clean_msg).strip()
        add_comment(clean_pid, target_tid, f"Admin ({source})", comment_body)
        if auto_run:
            updated = update_task(clean_pid, target_tid, status="in_progress", assignee="AI")
            await _trigger_project(clean_pid, watcher, active_state)
        else:
            updated = load_task(clean_pid, target_tid)
        publish_event("board_change", pid=clean_pid, tid=target_tid, message=f"Updated {target_tid} from chat")
        reply = reply_hint or (
            f"Added instruction to {target_tid} and queued AI execution."
            if auto_run
            else f"Added comment to {target_tid}."
        )
        return {
            "ok": True,
            "action": "comment_and_run",
            "pid": clean_pid,
            "tid": target_tid,
            "reply": reply,
            "spoken_reply": distill_spoken_briefing(reply),
            "task": updated.model_dump() if updated else None,
            "briefing": build_project_briefing(clean_pid),
        }

    # Default / create_and_run: create structured ticket under PID-0
    title = str(decision.get("title") or "").strip()
    if not title:
        first_line = clean_msg.splitlines()[0].strip()
        title = first_line[:69] + "..." if len(first_line) > 72 else first_line
    description = str(decision.get("description") or clean_msg).strip()
    if clean_msg not in description and detect_spoken_language(clean_msg) != "en":
        description = f"{description}\n\nOriginal request: {clean_msg}"

    new_task = create_task(
        clean_pid,
        f"{clean_pid}-0",
        title=title,
        descriptions=description,
        assignee="AI" if auto_run else "Admin",
        status="in_progress" if auto_run else "not_started",
    )
    if new_task is None:
        update_project(clean_pid, status="in_progress", assignee="AI")
        return {"ok": False, "pid": clean_pid, "reply": f"Failed to create ticket in {clean_pid}.", "spoken_reply": ""}

    if auto_run:
        await _trigger_project(clean_pid, watcher, active_state)
    publish_event("board_change", pid=clean_pid, tid=new_task.id, message=f"Created ticket {new_task.id}")

    default_reply = (
        f"Created ticket {new_task.id} ({new_task.title}) and started AI execution."
        if auto_run
        else f"Created ticket {new_task.id} ({new_task.title}) in backlog."
    )
    reply = reply_hint or default_reply
    return {
        "ok": True,
        "action": "create_and_run",
        "pid": clean_pid,
        "tid": new_task.id,
        "reply": reply,
        "spoken_reply": distill_spoken_briefing(reply),
        "task": new_task.model_dump(),
        "briefing": build_project_briefing(clean_pid),
    }


if __name__ == "__main__":
    import tempfile
    from .storage import create_project, get_default_worker_prompt

    async def _main() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prev = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = tmpdir
            try:
                create_project("CHAT", "Chat Test", "Verify chat router", get_default_worker_prompt())
                r_brief = await handle_project_chat("CHAT", "status")
                assert r_brief["ok"] and r_brief["action"] == "briefing"

                async def _stub_cls(*args: Any) -> Dict[str, Any]:
                    return {"intent": "create_and_run", "title": "Add undo button", "description": "Support move undo", "reply": "Created and started Undo ticket."}

                r_new = await handle_project_chat("CHAT", "add an undo button", auto_run=False, intent_classifier=_stub_cls)
                assert r_new["ok"] and r_new["tid"] == "CHAT-1"
            finally:
                if prev is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prev
        print("orchestrator.chat_router self-test passed!")

    asyncio.run(_main())
