"""Harness-agnostic AI Worker executor with step lifecycle, validation, and alerts."""

from __future__ import annotations

import asyncio
import html
import inspect
import os
import tempfile
from datetime import datetime
from typing import Any, Callable, List, Optional

import httpx

from harness.registry import create_default_registry
from harness.router import HarnessRouter, StepResult, format_event_log_line
from harness.types import ErrorCategory, HarnessEvent, RunRequest

from .models import Comment, ExecutionMeta, Task
from .state import state
from .storage import (
    get_data_dir,
    get_default_worker_prompt,
    load_task,
    restore_project_json_files,
    save_task,
    snapshot_project_json_files,
    validate_project_json_files,
)


def find_active_task(pid: str) -> Optional[Task]:
    """Return the first active AI subtask (sorted by TID), or the root PID-0 project task."""
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.isdir(tasks_dir):
        for tid in sorted(os.listdir(tasks_dir)):
            if tid.startswith("."):
                continue
            task = load_task(pid, tid)
            if task and task.assignee == "AI" and task.status == "in_progress":
                return task
    return load_task(pid, f"{pid}-0")


def _build_worker_prompt(pid: str, active_task: Task, system_prompt: str) -> str:
    """Build the single-step prompt instructing the worker on the target task."""
    focus = (
        f"Current active subtask: ./tasks/{active_task.id}/task.json ({active_task.id})."
        if active_task.id != f"{pid}-0"
        else f"Current active target: ./project.json ({pid}-0)."
    )
    return (
        f"{system_prompt.strip()}\n\n"
        f"{focus}\n"
        "Inspect ./project.json and existing ./tasks/<TID>/task.json files, complete ONE logical step "
        "(plan/create tasks, implement code, run verification tests, add comments, or update status), "
        "and update the local JSON files directly. Validate modified JSON files with python -m json.tool."
    )


def _append_output_log(output_log_path: str, line: str) -> None:
    """Append one output line to latest_worker_output.log."""
    os.makedirs(os.path.dirname(os.path.abspath(output_log_path)), exist_ok=True)
    with open(output_log_path, "a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else f"{line}\n")


async def execute_ai_task_async(
    pid: str,
    router: Optional[HarnessRouter] = None,
    timeout_seconds: Optional[int] = None,
    on_event: Optional[Callable[[HarnessEvent], Any]] = None,
) -> Optional[StepResult]:
    """Execute one autonomous step for project pid through the configured HarnessRouter."""
    project_task = load_task(pid, f"{pid}-0")
    if not project_task:
        return None

    active_task = find_active_task(pid) or project_task
    active_router = router or HarnessRouter(registry=create_default_registry(), state_manager=state)
    profile = active_router.resolve_profile(project_task=project_task, active_task=active_task)
    eff_timeout = timeout_seconds or int(os.environ.get("WORKER_TIMEOUT_SECONDS", "300"))

    project_dir = os.path.join(get_data_dir(), "projects", pid)
    system_prompt_path = os.path.join(project_dir, "system_prompt.md")
    if os.path.isfile(system_prompt_path):
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            system_prompt = f.read()
    else:
        system_prompt = get_default_worker_prompt()

    output_log_path = os.path.join(get_data_dir(), "latest_worker_output.log")
    os.makedirs(os.path.dirname(os.path.abspath(output_log_path)), exist_ok=True)
    with open(output_log_path, "w", encoding="utf-8") as f:
        f.write(
            f"[{pid}] Starting AI Worker execution via harness='{profile.harness}' "
            f"(model='{profile.model}', effort='{profile.effort}', preset='{profile.permission_preset.value}').\n"
        )

    session_id: Optional[str] = None
    if (
        profile.session_mode == "sticky"
        and active_task.execution_meta
        and active_task.execution_meta.last_harness == profile.harness
    ):
        session_id = active_task.execution_meta.session_id

    snapshot = snapshot_project_json_files(pid)

    async def _handle_event(event: HarnessEvent) -> None:
        line = format_event_log_line(event)
        if line:
            print(f"[{pid} | {profile.harness}] {line}")
            _append_output_log(output_log_path, line)
        if on_event is not None:
            maybe = on_event(event)
            if inspect.isawaitable(maybe):
                await maybe

    try:
        request = RunRequest(
            prompt=_build_worker_prompt(pid, active_task, system_prompt),
            working_dir=project_dir,
            model=profile.model,
            effort=profile.effort,
            permission_preset=profile.permission_preset,
            session_id=session_id,
            timeout_seconds=eff_timeout,
        )
        print(
            f"[{pid}] Executing step via harness '{profile.harness}' "
            f"(model={profile.model}, preset={profile.permission_preset.value}, cwd={project_dir})"
        )
        result = await active_router.run_step(request, profile=profile, on_event=_handle_event)

        if result.success:
            json_errors = validate_project_json_files(pid)
            if json_errors:
                error_details = "\n".join(f"- {path}: {err}" for path, err in json_errors)
                repair_req = RunRequest(
                    prompt=(
                        "Some TodoX JSON state files became invalid after the previous step:\n"
                        f"{error_details}\n\n"
                        "Fix these files so they are valid JSON matching the TodoX 9-field Task schema."
                    ),
                    working_dir=project_dir,
                    model=profile.model,
                    effort=profile.effort,
                    permission_preset=profile.permission_preset,
                    timeout_seconds=min(eff_timeout, 120),
                )
                await active_router.run_step(repair_req, profile=profile, on_event=_handle_event)
                json_errors = validate_project_json_files(pid)

                if json_errors:
                    restore_project_json_files(snapshot)
                    err_msg = "Post-step JSON validation failed after repair attempt: " + "; ".join(
                        f"{os.path.basename(p)}: {e}" for p, e in json_errors
                    )
                    fail_active_tasks(pid, err_msg, result.output_lines)
                    return StepResult(
                        success=False,
                        harness=result.harness,
                        model=result.model,
                        session_id=result.session_id,
                        response=result.response,
                        status="ERROR",
                        error_category=ErrorCategory.PROCESS_CRASH,
                        error_message=err_msg,
                        duration_seconds=result.duration_seconds,
                        usage=result.usage,
                        tool_events=result.tool_events,
                        output_lines=result.output_lines,
                    )

            # Reload target task from disk so we never clobber updates written by the AI worker.
            reloaded = load_task(pid, active_task.id) or load_task(pid, f"{pid}-0")
            if reloaded:
                meta = reloaded.execution_meta or ExecutionMeta()
                meta.last_harness = result.harness
                meta.last_model = result.model
                meta.session_id = result.session_id if profile.session_mode == "sticky" else None
                meta.total_steps += 1
                meta.total_tokens += result.usage.total_tokens if result.usage else 0
                meta.total_duration_seconds += float(result.duration_seconds or 0.0)
                meta.last_run_at = datetime.now().isoformat()
                reloaded.execution_meta = meta
                save_task(pid, reloaded)

            print(f"[{pid}] AI Worker step finished successfully via {result.harness}.")
            state.reset_success()
            return result

        if result.error_category == ErrorCategory.RATE_LIMIT:
            is_backoff = state.handle_429()
            new_state = state.get_state()
            if is_backoff:
                result.backoff_triggered = True
                wait_s = new_state["interval_seconds"]
                print(f"[{pid}] Rate limit 429 hit on last fallback entry. Backing off for {wait_s}s.")
                send_telegram_alert(
                    pid,
                    f"⏳ [TodoX] Rate Limit 429 hit on all fallback entries. Pausing AI Worker for {wait_s}s.",
                    result.output_lines,
                )
            else:
                result.fallback_switched = True
                new_harness = new_state["default_harness"]
                new_model = new_state["default_model"]
                print(f"[{pid}] Rate limit 429 hit. Switched fallback to {new_harness} / {new_model}.")
                send_telegram_alert(
                    pid,
                    f"⚠️ [TodoX] Rate Limit 429 hit on {profile.harness}/{profile.model}. "
                    f"Switched fallback to {new_harness}/{new_model}.",
                    result.output_lines,
                )
            return result

        err_msg = result.error_message or f"Execution Failed ({result.error_category.value}): {result.status}"
        print(f"[{pid}] AI Worker step failed ({result.error_category.value}): {err_msg}")
        fail_active_tasks(pid, err_msg, result.output_lines)
        return result

    except Exception as exc:
        print(f"[{pid}] AI Worker failed (system error): {exc}")
        err_msg = f"Execution System Error: {exc}"
        fail_active_tasks(pid, err_msg, [])
        return StepResult(
            success=False,
            harness=profile.harness,
            model=profile.model,
            status="ERROR",
            error_category=ErrorCategory.UNKNOWN,
            error_message=err_msg,
        )


def execute_ai_task(
    pid: str,
    router: Optional[HarnessRouter] = None,
    timeout_seconds: Optional[int] = None,
) -> Optional[StepResult]:
    """Synchronous entrypoint invoked by watcher.py background thread."""
    return asyncio.run(execute_ai_task_async(pid, router=router, timeout_seconds=timeout_seconds))


def send_telegram_alert(pid: str, text: str, summary_lines: List[str]) -> None:
    """Send failure or rate-limit alert and HTML log tail to ADMIN_CHAT_ID via Telegram Bot API."""
    tg_token = os.environ.get("TG_TOKEN")
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if not (tg_token and admin_chat_id):
        return

    try:
        msg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        httpx.post(msg_url, json={"chat_id": admin_chat_id, "text": text}, timeout=10.0)

        if summary_lines:
            last_100 = [line if line.endswith("\n") else f"{line}\n" for line in summary_lines[-100:]]
            html_log = (
                "<html><body style='background:#1e1e1e;color:#d4d4d4;padding:10px;'>"
                f"<pre>{html.escape(''.join(last_100))}</pre></body></html>"
            )

            doc_url = f"https://api.telegram.org/bot{tg_token}/sendDocument"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
                tmp.write(html_log)
                tmp_path = tmp.name

            try:
                with open(tmp_path, "rb") as f:
                    files = {"document": (f"{pid}_error_log.html", f, "text/html")}
                    data = {"chat_id": admin_chat_id, "caption": f"Last {len(last_100)} lines of output log"}
                    httpx.post(doc_url, data=data, files=files, timeout=10.0)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
    except Exception as exc:
        print(f"[{pid}] Failed to send Telegram alert: {exc}")


def fail_active_tasks(pid: str, error_msg: str, summary_lines: List[str]) -> None:
    """Mark active AI tasks failed, hand control back to Admin, and alert Telegram."""
    tasks_to_fail: List[Task] = []

    root_task = load_task(pid, f"{pid}-0")
    if root_task and root_task.assignee == "AI" and root_task.status == "in_progress":
        tasks_to_fail.append(root_task)

    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        for tid in sorted(os.listdir(tasks_dir)):
            if tid == ".DS_Store":
                continue
            task = load_task(pid, tid)
            if task and task.assignee == "AI" and task.status == "in_progress":
                tasks_to_fail.append(task)

    if not tasks_to_fail and root_task:
        tasks_to_fail.append(root_task)

    comment_content = error_msg
    if summary_lines:
        last_100 = "\n".join(line.rstrip("\n") for line in summary_lines[-100:])
        comment_content += f"\n\nOutput Log:\n{last_100}"

    for t in tasks_to_fail:
        t.status = "failed"
        t.assignee = "Admin"
        t.comments.append(
            Comment(
                author="System",
                time=datetime.now().isoformat(),
                content=comment_content,
            )
        )
        save_task(pid, t)

    failed_tids = ", ".join(t.id for t in tasks_to_fail)
    alert_text = f"🚨 [TodoX] Project {pid} Execution Failed!\nTasks affected: {failed_tids}\nError: {error_msg}"
    send_telegram_alert(pid, alert_text, summary_lines)


if __name__ == "__main__":
    sample_task = Task(id="DEMO-0", title="Dry run", descriptions="Check prompt", status="in_progress", assignee="AI")
    prompt = _build_worker_prompt("DEMO", sample_task, get_default_worker_prompt())
    assert "DEMO-0" in prompt
    print("orchestrator.worker self-test passed!")
