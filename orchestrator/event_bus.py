"""Cross-process event bus, runtime status snapshot, and control queue for TodoX."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

from .storage import get_data_dir

_MAX_EVENTS_FILE_BYTES = 512 * 1024
_TRIM_KEEP_LINES = 600
_VALID_CONTROL_ACTIONS = frozenset({"trigger", "stop", "steer"})
_BUS_LOCK = threading.Lock()


def get_events_log_path() -> str:
    """Return path to DATA_DIR/live_events.jsonl."""
    return os.path.join(get_data_dir(), "live_events.jsonl")


def get_runtime_state_path() -> str:
    """Return path to DATA_DIR/worker_runtime.json."""
    return os.path.join(get_data_dir(), "worker_runtime.json")


def get_control_queue_path() -> str:
    """Return path to DATA_DIR/web_control.jsonl."""
    return os.path.join(get_data_dir(), "web_control.jsonl")


def _atomic_write_json(path: str, data: Any) -> None:
    """Atomically write JSON data via temporary file replacement."""
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


def _trim_jsonl_locked(path: str) -> None:
    """Rotate oversized JSONL log to its trailing lines."""
    try:
        if os.path.getsize(path) <= _MAX_EVENTS_FILE_BYTES:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-_TRIM_KEEP_LINES:]
        parent = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".trim")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.writelines(tail)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except OSError:
        pass


def publish_event(
    event_type: str,
    pid: str = "",
    tid: str = "",
    harness: str = "",
    model: str = "",
    message: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append a structured live event record to DATA_DIR/live_events.jsonl."""
    payload: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "epoch": round(time.time(), 3),
        "type": str(event_type or "info"),
        "pid": str(pid or "").strip().upper(),
        "tid": str(tid or "").strip().upper(),
        "harness": str(harness or "").strip(),
        "model": str(model or "").strip(),
        "message": str(message or ""),
        "meta": dict(meta) if isinstance(meta, dict) else {},
    }
    path = get_events_log_path()
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _BUS_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        _trim_jsonl_locked(path)
    return payload


def read_recent_events(
    limit: int = 80,
    pid: Optional[str] = None,
    include_global: bool = True,
) -> List[Dict[str, Any]]:
    """Read trailing live events from disk, optionally scoped to a project ID."""
    path = get_events_log_path()
    if not os.path.isfile(path):
        return []

    target_pid = pid.strip().upper() if pid else None
    safe_limit = max(1, min(500, int(limit or 80)))
    events: List[Dict[str, Any]] = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    scan_window = max(safe_limit * 4, 400)
    for raw in lines[-scan_window:]:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        item_pid = str(item.get("pid") or "").upper()
        if target_pid:
            if item_pid == target_pid:
                events.append(item)
            elif include_global and item_pid == "":
                events.append(item)
        else:
            events.append(item)

    return events[-safe_limit:]


def get_worker_runtime() -> Dict[str, Dict[str, Any]]:
    """Load currently active project workers from DATA_DIR/worker_runtime.json."""
    path = get_runtime_state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                clean[str(k).upper()] = v
        return clean
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def mark_worker_started(
    pid: str,
    tid: str,
    harness: str,
    model: Optional[str],
    preset: str,
) -> Dict[str, Any]:
    """Record active worker execution metadata in worker_runtime.json."""
    clean_pid = pid.strip().upper()
    entry: Dict[str, Any] = {
        "pid": clean_pid,
        "tid": tid.strip().upper(),
        "harness": harness.strip(),
        "model": (model or "default").strip() or "default",
        "preset": preset.strip() or "auto",
        "started_at": time.time(),
        "started_iso": datetime.now().isoformat(timespec="seconds"),
        "last_tool": "",
        "tool_count": 0,
    }
    with _BUS_LOCK:
        runtime = get_worker_runtime()
        runtime[clean_pid] = entry
        _atomic_write_json(get_runtime_state_path(), runtime)
    return entry


def update_worker_activity(
    pid: str,
    last_tool: str = "",
    tool_count: Optional[int] = None,
) -> None:
    """Update latest tool activity and tool counter for a running worker."""
    clean_pid = pid.strip().upper()
    with _BUS_LOCK:
        runtime = get_worker_runtime()
        entry = runtime.get(clean_pid)
        if entry is None:
            return
        if last_tool:
            entry["last_tool"] = last_tool[:120]
        if tool_count is not None:
            entry["tool_count"] = int(tool_count)
        entry["updated_at"] = time.time()
        runtime[clean_pid] = entry
        _atomic_write_json(get_runtime_state_path(), runtime)


def mark_worker_finished(pid: str) -> None:
    """Remove project worker entry from worker_runtime.json when step completes."""
    clean_pid = pid.strip().upper()
    with _BUS_LOCK:
        runtime = get_worker_runtime()
        if clean_pid in runtime:
            runtime.pop(clean_pid, None)
            _atomic_write_json(get_runtime_state_path(), runtime)


def clear_stale_worker_runtime(active_pids: Sequence[str] = ()) -> None:
    """Prune stale entries from worker_runtime.json not present in active_pids."""
    keep = {str(p).strip().upper() for p in active_pids if str(p).strip()}
    with _BUS_LOCK:
        runtime = get_worker_runtime()
        filtered = {pid: entry for pid, entry in runtime.items() if pid in keep}
        if filtered != runtime:
            _atomic_write_json(get_runtime_state_path(), filtered)


def enqueue_control_command(action: str, pid: str, message: str = "") -> Dict[str, Any]:
    """Queue a cross-process worker control action (trigger/stop/steer) for AsyncWatcher."""
    clean_action = action.strip().lower()
    if clean_action not in _VALID_CONTROL_ACTIONS:
        raise ValueError(f"Unsupported control action '{action}'. Must be one of {sorted(_VALID_CONTROL_ACTIONS)}.")
    clean_pid = pid.strip().upper()
    if not clean_pid:
        raise ValueError("Project ID cannot be empty.")

    cmd = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "action": clean_action,
        "pid": clean_pid,
        "message": str(message or ""),
    }
    path = get_control_queue_path()
    line = json.dumps(cmd, ensure_ascii=False) + "\n"
    with _BUS_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    return cmd


def drain_control_commands() -> List[Dict[str, Any]]:
    """Atomically drain and clear all queued Web UI control commands."""
    path = get_control_queue_path()
    if not os.path.isfile(path):
        return []

    cmds: List[Dict[str, Any]] = []
    with _BUS_LOCK:
        if not os.path.isfile(path):
            return []
        parent = os.path.dirname(os.path.abspath(path))
        fd, drain_path = tempfile.mkstemp(dir=parent, suffix=".drain")
        os.close(fd)
        try:
            os.replace(path, drain_path)
            with open(drain_path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            cmds.append(parsed)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        finally:
            if os.path.exists(drain_path):
                try:
                    os.unlink(drain_path)
                except OSError:
                    pass
    return cmds


def _format_sse(event_name: str, payload: Any) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_events_async(
    pid: Optional[str] = None,
    once: bool = False,
    poll_interval: float = 0.35,
    heartbeat_seconds: float = 15.0,
    state_snapshot_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """Yield SSE initial snapshot followed by live tailed events and heartbeats."""
    target_pid = pid.strip().upper() if pid else None
    snapshot_payload: Dict[str, Any] = {
        "events": read_recent_events(limit=60, pid=target_pid),
        "runtime": get_worker_runtime(),
    }
    if state_snapshot_fn is not None:
        try:
            snapshot_payload["system_state"] = state_snapshot_fn()
        except Exception:
            pass
    yield _format_sse("snapshot", snapshot_payload)

    if once:
        return

    path = get_events_log_path()
    offset = os.path.getsize(path) if os.path.isfile(path) else 0
    partial_line = ""
    last_heartbeat = time.time()
    step_sleep = max(0.1, float(poll_interval))

    while True:
        await asyncio.sleep(step_sleep)
        emitted = False

        if os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                if size < offset:
                    offset = 0
                    partial_line = ""
                if size > offset:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    if chunk:
                        buf = partial_line + chunk
                        if buf.endswith("\n"):
                            lines = buf.splitlines()
                            partial_line = ""
                        else:
                            split_lines = buf.splitlines()
                            lines = split_lines[:-1]
                            partial_line = split_lines[-1] if split_lines else ""

                        for raw in lines:
                            stripped = raw.strip()
                            if not stripped:
                                continue
                            try:
                                item = json.loads(stripped)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(item, dict):
                                continue
                            item_pid = str(item.get("pid") or "").upper()
                            if target_pid and item_pid and item_pid != target_pid:
                                continue
                            ev_type = str(item.get("type") or "message")
                            yield _format_sse(ev_type, item)
                            emitted = True
            except OSError:
                pass

        now = time.time()
        if emitted:
            last_heartbeat = now
        elif (now - last_heartbeat) >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            last_heartbeat = now


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmpdir:
        prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            mark_worker_started("DEMO", "DEMO-1", "agy", "gemini-3.8-flash", "auto")
            update_worker_activity("DEMO", "read_file task.json", 1)
            assert "DEMO" in get_worker_runtime()
            publish_event("tool", pid="DEMO", tid="DEMO-1", harness="agy", message="read_file DONE")
            recent = read_recent_events(limit=10, pid="DEMO")
            assert len(recent) == 1 and recent[0]["type"] == "tool"
            mark_worker_finished("DEMO")
            assert get_worker_runtime() == {}
            enqueue_control_command("trigger", "DEMO")
            drained = drain_control_commands()
            assert len(drained) == 1 and drained[0]["action"] == "trigger"
            assert drain_control_commands() == []
        finally:
            if prev is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev
    print("orchestrator.event_bus self-test passed!")
