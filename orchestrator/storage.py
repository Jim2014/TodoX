"""Atomic filesystem storage, prompt loading, and JSON validation helpers for TodoX."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from .models import Comment, ProjectSummary, Task

load_dotenv()


def _atomic_write_text(path: str, content: str) -> None:
    """Write UTF-8 text atomically via a temporary file in the same directory."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _atomic_write_json(path: str, data: Any) -> None:
    """Serialize data as indented JSON and atomically replace target file."""
    _atomic_write_text(path, json.dumps(data, indent=4, ensure_ascii=False))


def get_data_dir() -> str:
    """Resolve DATA_DIR from environment, anchored to repository root when relative."""
    raw_dir = os.environ.get("DATA_DIR", "./data")
    if not os.path.isabs(raw_dir):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(project_root, raw_dir))
    return raw_dir


def get_default_worker_prompt() -> str:
    """Load the vendor-neutral worker prompt template (with legacy fallback)."""
    prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
    for filename in ("default_worker.md", "default_gemini.md"):
        candidate = os.path.join(prompts_dir, filename)
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def load_projects() -> Dict[str, ProjectSummary]:
    """Load all project summaries from DATA_DIR/projects."""
    data_dir = get_data_dir()
    projects_dir = os.path.join(data_dir, "projects")
    result: Dict[str, ProjectSummary] = {}

    if not os.path.exists(projects_dir):
        return result

    for pid in sorted(os.listdir(projects_dir)):
        project_path = os.path.join(projects_dir, pid)
        if os.path.isdir(project_path):
            task = load_task(pid, f"{pid}-0")
            if task:
                result[pid] = ProjectSummary(
                    id=pid,
                    title=task.title,
                    status=task.status,
                    assignee=task.assignee,
                )
    return result


def _get_task_path(pid: str, tid: str) -> str:
    data_dir = get_data_dir()
    if tid == f"{pid}-0":
        return os.path.join(data_dir, "projects", pid, "project.json")
    return os.path.join(data_dir, "projects", pid, "tasks", tid, "task.json")


def load_task(pid: str, tid: str) -> Optional[Task]:
    """Load a project root (PID-0) or subtask JSON file as a Task model."""
    path = _get_task_path(pid, tid)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return Task(**json.load(f))


def save_task(pid: str, task: Task) -> None:
    """Save a Task model atomically to disk, omitting unset optional fields."""
    path = _get_task_path(pid, task.id)
    _atomic_write_json(path, task.model_dump(exclude_none=True))


def _collect_project_json_paths(pid: str) -> List[str]:
    """Return paths to project.json and all tasks/<TID>/task.json files for a project."""
    project_dir = os.path.join(get_data_dir(), "projects", pid)
    paths: List[str] = []

    project_json = os.path.join(project_dir, "project.json")
    if os.path.isfile(project_json):
        paths.append(project_json)

    tasks_dir = os.path.join(project_dir, "tasks")
    if os.path.isdir(tasks_dir):
        for tid in sorted(os.listdir(tasks_dir)):
            task_json = os.path.join(tasks_dir, tid, "task.json")
            if os.path.isfile(task_json):
                paths.append(task_json)

    return paths


def validate_project_json_files(pid: str) -> List[Tuple[str, str]]:
    """
    Validate JSON syntax and Pydantic Task schema for all state files in a project.
    Returns a list of (file_path, error_message) tuples; empty list means all valid.
    """
    errors: List[Tuple[str, str]] = []
    paths = _collect_project_json_paths(pid)
    if not paths:
        project_json = os.path.join(get_data_dir(), "projects", pid, "project.json")
        return [(project_json, "Missing project.json")]

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            Task(**raw)
        except Exception as exc:
            errors.append((path, str(exc)))
    return errors


def snapshot_project_json_files(pid: str) -> Dict[str, str]:
    """Capture raw text contents of all existing project/task JSON files in memory."""
    snapshot: Dict[str, str] = {}
    for path in _collect_project_json_paths(pid):
        try:
            with open(path, "r", encoding="utf-8") as f:
                snapshot[path] = f.read()
        except OSError:
            pass
    return snapshot


def restore_project_json_files(snapshot: Dict[str, str]) -> None:
    """Restore previously snapshotted JSON files atomically."""
    for path, content in snapshot.items():
        _atomic_write_text(path, content)


def create_project(pid: str, title: str, descriptions: str, system_prompt: str) -> Task:
    """Initialize project folder structure, system_prompt.md, and root project.json."""
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)

    os.makedirs(os.path.join(project_dir, "files"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "tasks"), exist_ok=True)

    _atomic_write_text(os.path.join(project_dir, "system_prompt.md"), system_prompt)

    tid = f"{pid}-0"
    project_task = Task(
        id=tid,
        title=title,
        descriptions=descriptions,
        status="in_progress",
        assignee="AI",
    )
    save_task(pid, project_task)
    return project_task


def delete_project(pid: str) -> bool:
    """Delete an entire project directory tree."""
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)

    if not os.path.exists(project_dir):
        return False

    shutil.rmtree(project_dir)
    return True


def update_project(pid: str, **kwargs: Any) -> Optional[Task]:
    """Update fields on the root PID-0 task."""
    task = load_task(pid, f"{pid}-0")
    if not task:
        return None

    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)

    save_task(pid, task)
    return task


def create_task(
    pid: str,
    parent_tid: str,
    title: str,
    descriptions: str,
    assignee: str = "AI",
    status: str = "not_started",
) -> Optional[Task]:
    """Create a subtask under parent_tid and link it in parent child_ids."""
    parent_task = load_task(pid, parent_tid)
    if not parent_task:
        return None

    data_dir = get_data_dir()
    tasks_dir = os.path.join(data_dir, "projects", pid, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    existing_tids = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d))]
    max_num = 0
    for t in existing_tids:
        try:
            num = int(t.split("-")[1])
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            pass

    new_tid = f"{pid}-{max_num + 1}"
    new_task = Task(
        id=new_tid,
        title=title,
        descriptions=descriptions,
        assignee=assignee,
        parent_id=parent_tid,
        status=status,
    )
    save_task(pid, new_task)

    parent_task.child_ids.append(new_tid)
    save_task(pid, parent_task)

    project_task = load_task(pid, f"{pid}-0")
    if project_task:
        changed = False
        if project_task.status != "in_progress":
            project_task.status = "in_progress"
            changed = True
        if str(project_task.assignee).upper() != "AI":
            project_task.assignee = "AI"
            changed = True
        if changed:
            save_task(pid, project_task)

    return new_task


def delete_task(pid: str, tid: str) -> bool:
    """Delete a task (or entire project if tid == PID-0) and unlink from parent."""
    if tid == f"{pid}-0":
        return delete_project(pid)

    task = load_task(pid, tid)
    if not task:
        return False

    data_dir = get_data_dir()
    task_dir = os.path.join(data_dir, "projects", pid, "tasks", tid)
    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)

    if task.parent_id:
        parent_task = load_task(pid, task.parent_id)
        if parent_task and tid in parent_task.child_ids:
            parent_task.child_ids.remove(tid)
            save_task(pid, parent_task)

    return True


def update_task(pid: str, tid: str, **kwargs: Any) -> Optional[Task]:
    """Update fields on a subtask (or root project if tid == PID-0)."""
    if tid == f"{pid}-0":
        return update_project(pid, **kwargs)

    task = load_task(pid, tid)
    if not task:
        return None

    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)

    save_task(pid, task)

    if str(kwargs.get("assignee", "")).upper() == "AI":
        project_task = load_task(pid, f"{pid}-0")
        if project_task:
            changed = False
            if project_task.status != "in_progress":
                project_task.status = "in_progress"
                changed = True
            if str(project_task.assignee).upper() != "AI":
                project_task.assignee = "AI"
                changed = True
            if changed:
                save_task(pid, project_task)

    return task


def add_comment(pid: str, tid: str, author: str, content: str) -> Optional[Task]:
    """Append a narrative comment to a task."""
    task = load_task(pid, tid)
    if not task:
        return None

    task.comments.append(
        Comment(
            author=author,
            time=datetime.now().isoformat(),
            content=content,
        )
    )
    save_task(pid, task)
    return task


def delete_comment(pid: str, tid: str, comment_index: int) -> Optional[Task]:
    """Delete a comment at comment_index from a task."""
    task = load_task(pid, tid)
    if not task:
        return None

    if 0 <= comment_index < len(task.comments):
        task.comments.pop(comment_index)
        save_task(pid, task)
        return task
    return None


def update_comment(pid: str, tid: str, comment_index: int, content: str) -> Optional[Task]:
    """Update comment text at comment_index on a task."""
    task = load_task(pid, tid)
    if not task:
        return None

    if 0 <= comment_index < len(task.comments):
        task.comments[comment_index].content = content
        save_task(pid, task)
        return task
    return None


if __name__ == "__main__":
    prompt = get_default_worker_prompt()
    assert "9-Field JSON Schema" in prompt
    with tempfile.TemporaryDirectory() as tmpdir:
        prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            create_project("DEMO", "Demo Project", "Verify storage self-test", prompt)
            assert validate_project_json_files("DEMO") == []
            snap = snapshot_project_json_files("DEMO")
            assert len(snap) == 1
        finally:
            if prev is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev
    print("orchestrator.storage self-test passed!")
