"""Antigravity CLI (agy) harness implementation supporting bidirectional stream-json."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from .base import BaseHarness, DEFAULT_STREAM_LIMIT_BYTES
from .types import (
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HealthStatus,
    ModelInfo,
    PermissionPreset,
    RunRequest,
    UsageMetrics,
    normalize_permission_preset,
    normalize_tool_name,
)

logger = logging.getLogger(__name__)

_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$", re.IGNORECASE)

DEFAULT_AGY_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gemini-3.8-flash",
        name="Gemini 3.8 Flash",
        harness="agy",
        icon="⚡",
        description="Fast multimodal model with dynamic reasoning",
    ),
    ModelInfo(
        id="gemini-3.1-pro",
        name="Gemini 3.1 Pro",
        harness="agy",
        icon="🧠",
        description="Deep reasoning and complex coding",
    ),
    ModelInfo(
        id="gemini-3.7-flash",
        name="Gemini 3.7 Flash",
        harness="agy",
        icon="⚡",
        description="Multimodal hybrid reasoning model",
    ),
    ModelInfo(
        id="claude-sonnet-4-6",
        name="Claude Sonnet 4.6",
        harness="agy",
        icon="🟣",
        description="Anthropic Claude 4.6 (Thinking)",
    ),
    ModelInfo(
        id="claude-opus-4-6-thinking",
        name="Claude Opus 4.6",
        harness="agy",
        icon="🎭",
        description="Anthropic Claude 4.6 Opus (Thinking)",
    ),
]


def resolve_default_agy_bin(explicit_bin: Optional[str] = None) -> str:
    """Resolve the agy executable path from explicit arg, env var, ~/.local/bin/agy, or PATH."""
    if explicit_bin:
        expanded = os.path.expanduser(explicit_bin)
        if os.path.isfile(expanded):
            return expanded
        found = shutil.which(explicit_bin)
        return found or expanded

    env_bin = os.environ.get("AGY_BIN_PATH", "").strip()
    if env_bin:
        expanded = os.path.expanduser(env_bin)
        if os.path.isfile(expanded):
            return expanded
        found = shutil.which(env_bin)
        return found or expanded

    local_bin = Path.home() / ".local" / "bin" / "agy"
    if local_bin.is_file():
        return str(local_bin)

    return shutil.which("agy") or str(local_bin)


def model_supports_effort(model: Optional[str]) -> bool:
    """Return False when the model manages reasoning effort internally or pins it in its ID."""
    if not model:
        return True
    normalized = model.strip().lower()
    if normalized == "default":
        return True
    if "orchestrator" in normalized:
        return False
    if _EFFORT_SUFFIX_RE.search(normalized):
        return False
    return True


def parse_agy_models(output: str) -> List[ModelInfo]:
    """Parse 'agy models' CLI stdout into normalized ModelInfo instances."""
    models: List[ModelInfo] = []
    seen_base_ids: Set[str] = set()

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("Fetching", "Usage:", "List")):
            continue

        parts = stripped.split("\t")
        if not parts or not parts[0].strip():
            continue

        raw_id = parts[0].strip()
        raw_name = parts[1].strip() if len(parts) > 1 else raw_id

        if raw_id.startswith("gemini-"):
            base_id = re.sub(r"-(high|medium|low)$", "", raw_id)
        else:
            base_id = raw_id

        if base_id in seen_base_ids:
            continue
        seen_base_ids.add(base_id)

        clean_name = re.sub(r"\s*\(Thinking\)", "", raw_name).strip()
        if base_id != raw_id:
            clean_name = re.sub(r"\s*\((High|Medium|Low)\)", "", clean_name).strip()
        if not clean_name or clean_name == base_id:
            clean_name = " ".join(word.capitalize() for word in base_id.split("-"))

        lower_id = base_id.lower()
        if "orchestrator" in lower_id:
            icon, desc = "🎼", "Multi-model Orchestrator (dynamic planning & execution)"
        elif "flash" in lower_id:
            icon, desc = "⚡", "Fast multimodal model with dynamic reasoning"
        elif "pro" in lower_id:
            icon, desc = "🧠", "Deep reasoning and complex coding"
        elif "claude" in lower_id:
            icon, desc = "🟣", "Anthropic reasoning model"
        elif "gpt" in lower_id or "oss" in lower_id:
            icon, desc = "🤖", "Open-weights model"
        else:
            icon, desc = "✨", "Antigravity CLI model"

        models.append(
            ModelInfo(
                id=base_id,
                raw_id=raw_id,
                name=clean_name,
                harness="agy",
                icon=icon,
                description=desc,
            )
        )

    return models


class AgyHarness(BaseHarness):
    """Harness adapter for Google Antigravity CLI (agy) using stream-json."""

    _CAPABILITIES = HarnessCapabilities(
        name="agy",
        display_name="Antigravity CLI (agy)",
        supports_streaming=True,
        supports_steering=True,
        supports_warm_session=True,
        supports_session_resume=True,
        supports_effort=True,
        supports_model_discovery=True,
        supports_json_schema=True,
    )

    def __init__(
        self,
        agy_bin: Optional[str] = None,
        keep_alive: bool = False,
        idle_timeout_seconds: float = 300.0,
        stream_limit: int = DEFAULT_STREAM_LIMIT_BYTES,
    ) -> None:
        self.agy_bin = resolve_default_agy_bin(agy_bin)
        self.keep_alive = keep_alive
        self.idle_timeout_seconds = idle_timeout_seconds
        self.stream_limit = stream_limit

        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue[Optional[HarnessEvent]] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._is_stopped = False
        self._pending_turns = 0

        self._current_session_id: Optional[str] = None
        self._current_working_dir: Optional[str] = None
        self._current_model: Optional[str] = None
        self._current_effort: Optional[str] = None
        self._current_permission_preset: Optional[PermissionPreset] = None

        self._cached_models: Optional[List[ModelInfo]] = None
        self._cached_models_expiry: float = 0.0
        self._raw_output_lines: List[str] = []

    @property
    def capabilities(self) -> HarnessCapabilities:
        """Return AgyHarness capability flags."""
        return self._CAPABILITIES

    def is_running(self) -> bool:
        """Return True if the active agy subprocess is still running."""
        proc = self._current_process
        return proc is not None and proc.returncode is None and not self._is_stopped

    def _binary_exists(self) -> bool:
        """Return True if the resolved agy binary exists on disk or PATH."""
        return os.path.isfile(self.agy_bin) or shutil.which(self.agy_bin) is not None

    async def check_health(self) -> HealthStatus:
        """Verify agy binary presence and CLI version without running an LLM turn."""
        if not self._binary_exists():
            return HealthStatus(
                harness="agy",
                available=False,
                binary_path=self.agy_bin,
                message=f"agy binary not found at {self.agy_bin}",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                self.agy_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            version_out = (stdout_bytes or stderr_bytes).decode("utf-8", errors="replace").strip()
            first_line = version_out.splitlines()[0].strip() if version_out else "installed"
            return HealthStatus(
                harness="agy",
                available=True,
                binary_path=self.agy_bin,
                version=first_line,
                message="OK",
            )
        except Exception as exc:
            return HealthStatus(
                harness="agy",
                available=True,
                binary_path=self.agy_bin,
                version=None,
                message=f"Binary present; --version probe returned: {exc}",
            )

    async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        """Discover models from 'agy models' with 10-minute cache and static fallback."""
        now = time.time()
        if not force_refresh and self._cached_models and now < self._cached_models_expiry:
            return list(self._cached_models)

        if not self._binary_exists():
            return list(DEFAULT_AGY_MODELS)

        try:
            proc = await asyncio.create_subprocess_exec(
                self.agy_bin,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
            parsed = parse_agy_models(stdout_bytes.decode("utf-8", errors="replace"))
            if parsed:
                self._cached_models = parsed
                self._cached_models_expiry = now + 600.0
                return list(parsed)
        except Exception as exc:
            logger.warning("Failed to discover models from %s: %s", self.agy_bin, exc)

        return list(self._cached_models or DEFAULT_AGY_MODELS)

    def build_command(self, request: RunRequest) -> List[str]:
        """Build CLI invocation arguments for agy stream-json mode."""
        preset = normalize_permission_preset(request.permission_preset)
        cmd = [
            self.agy_bin,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]

        if preset == PermissionPreset.TURBO:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--mode", preset.value])

        if request.session_id:
            cmd.extend(["--conversation", request.session_id])
        else:
            cmd.append("--new-project")

        if request.working_dir:
            cmd.extend(["--add-dir", request.working_dir])
        for extra_dir in request.extra_dirs:
            if extra_dir and extra_dir != request.working_dir:
                cmd.extend(["--add-dir", extra_dir])

        model_val = (request.model or "").strip()
        if model_val and model_val.lower() != "default":
            cmd.extend(["--model", model_val])

        effort_val = (request.effort or "").strip().lower()
        if effort_val and model_supports_effort(model_val or None):
            cmd.extend(["--effort", effort_val])

        if request.json_schema:
            cmd.extend(["--json-schema", request.json_schema])

        return cmd

    def _cancel_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _start_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_task = asyncio.create_task(self._idle_worker())

    async def _idle_worker(self) -> None:
        try:
            await asyncio.sleep(self.idle_timeout_seconds)
            logger.info("AgyHarness idle timeout (%ss) expired; stopping warm subprocess.", self.idle_timeout_seconds)
            await self.stop()
        except asyncio.CancelledError:
            pass

    async def _stdout_reader(self, proc: asyncio.subprocess.Process) -> None:
        """Single-coroutine stdout reader pushing parsed events into self._event_queue."""
        assert proc.stdout is not None
        try:
            while not self._is_stopped:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    if proc.returncode is not None or (proc.stdin and proc.stdin.is_closing()):
                        break
                    await asyncio.sleep(0.01)
                    if proc.returncode is not None:
                        break
                    continue

                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                self._raw_output_lines.append(line_str)
                event = self._parse_line(line_str)
                if event:
                    if event.type in ("init", "result") and event.session_id:
                        self._current_session_id = event.session_id
                    await self._event_queue.put(event)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("AgyHarness stdout reader error: %s", exc)
            await self._event_queue.put(
                HarnessEvent(
                    type="error",
                    harness="agy",
                    status="ERROR",
                    error_category=ErrorCategory.PROCESS_CRASH,
                    response=f"Stdout reader failure: {exc}",
                )
            )
        finally:
            await self._event_queue.put(None)

    def _compose_prompt_content(self, request: RunRequest, is_followup_turn: bool) -> str:
        """Attach full system prompt on fresh sessions or compact reminder on followups."""
        if not is_followup_turn:
            if request.system_prompt and request.system_prompt.strip():
                return f"{request.system_prompt.strip()}\n\n---\n\n{request.prompt}"
            return request.prompt

        if (
            request.system_prompt
            and request.system_prompt.strip()
            and request.followup_system_prompt
            and request.followup_system_prompt.strip()
        ):
            return f"{request.followup_system_prompt.strip()}\n\n---\n\n{request.prompt}"
        return request.prompt

    async def steer(self, message: str) -> bool:
        """Inject a mid-flight steering turn into the active agy process over stdin."""
        msg_str = message.strip()
        if not msg_str:
            return False

        async with self._lock:
            proc = self._current_process
            if not proc or proc.returncode is not None or not proc.stdin or proc.stdin.is_closing():
                return False

            try:
                payload = json.dumps({"event": "user", "message": {"content": msg_str}}) + "\n"
                proc.stdin.write(payload.encode("utf-8"))
                await proc.stdin.drain()
                self._pending_turns += 1
                return True
            except Exception as exc:
                logger.error("AgyHarness failed to send steering message: %s", exc)
                return False

    async def run_stream(self, request: RunRequest) -> AsyncIterator[HarnessEvent]:
        """Execute an agy turn and yield normalized HarnessEvent instances."""
        self._is_stopped = False
        self._cancel_idle_timer()
        self._raw_output_lines.clear()

        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        effective_model = (request.model or "").strip() or None
        if effective_model and effective_model.lower() == "default":
            effective_model = None
        effective_effort = (
            request.effort.strip().lower()
            if request.effort and model_supports_effort(effective_model)
            else None
        )
        effective_preset = normalize_permission_preset(request.permission_preset)

        can_reuse = (
            self.keep_alive
            and self.is_running()
            and self._current_process is not None
            and self._current_process.stdin is not None
            and not self._current_process.stdin.is_closing()
            and self._current_session_id == request.session_id
            and self._current_working_dir == request.working_dir
            and self._current_model == effective_model
            and self._current_effort == effective_effort
            and self._current_permission_preset == effective_preset
            and self._reader_task is not None
            and not self._reader_task.done()
        )

        is_followup_turn = can_reuse or bool(request.session_id)
        full_content = self._compose_prompt_content(request, is_followup_turn)

        if can_reuse:
            assert self._current_process is not None
            process = self._current_process
            async with self._lock:
                assert process.stdin is not None
                self._pending_turns = 1
                payload = json.dumps({"event": "user", "message": {"content": full_content}}) + "\n"
                process.stdin.write(payload.encode("utf-8"))
                await process.stdin.drain()
        else:
            if self.is_running():
                await self.stop()

            self._is_stopped = False
            self._event_queue = asyncio.Queue()
            self._pending_turns = 0

            cmd = self.build_command(request)
            try:
                process = await self._spawn_subprocess(
                    cmd=cmd,
                    cwd=request.working_dir,
                    env_overrides=request.env_overrides,
                    limit=self.stream_limit,
                )
            except FileNotFoundError:
                yield HarnessEvent(
                    type="error",
                    harness="agy",
                    status="ERROR",
                    error_category=ErrorCategory.BINARY_NOT_FOUND,
                    response=f"agy binary not found at: {self.agy_bin}",
                )
                return
            except Exception as exc:
                yield HarnessEvent(
                    type="error",
                    harness="agy",
                    status="ERROR",
                    error_category=ErrorCategory.PROCESS_CRASH,
                    response=f"Failed to start agy process: {exc}",
                )
                return

            self._current_process = process
            self._current_session_id = request.session_id
            self._current_working_dir = request.working_dir
            self._current_model = effective_model
            self._current_effort = effective_effort
            self._current_permission_preset = effective_preset
            self._reader_task = asyncio.create_task(self._stdout_reader(process))

            assert process.stdin is not None
            async with self._lock:
                self._pending_turns = 1
                init_payload = json.dumps({"event": "user", "message": {"content": full_content}}) + "\n"
                process.stdin.write(init_payload.encode("utf-8"))
                await process.stdin.drain()

        got_result = False
        try:
            while True:
                if request.timeout_seconds and request.timeout_seconds > 0:
                    event = await asyncio.wait_for(
                        self._event_queue.get(),
                        timeout=request.timeout_seconds,
                    )
                else:
                    event = await self._event_queue.get()

                if event is None:
                    break

                if event.type == "result":
                    got_result = True
                    async with self._lock:
                        self._pending_turns -= 1
                        has_more_turns = self._pending_turns > 0
                        if (
                            not self.keep_alive
                            and not has_more_turns
                            and process.stdin
                            and not process.stdin.is_closing()
                        ):
                            try:
                                process.stdin.close()
                            except Exception:
                                pass

                    if has_more_turns:
                        yield HarnessEvent(
                            type="turn_complete",
                            harness="agy",
                            session_id=event.session_id,
                            status=event.status,
                            response=event.response,
                            duration_seconds=event.duration_seconds,
                            usage=event.usage,
                            payload=event.payload,
                        )
                        continue

                    yield event
                    if self.keep_alive:
                        if event.status.upper() in ("ERROR", "FAILED", "FAILURE") and process.returncode is not None:
                            self._current_process = None
                        else:
                            self._start_idle_timer()
                    break

                yield event

            if not self.keep_alive or (not got_result and not self._is_stopped):
                await process.wait()

                if process.returncode != 0 and not self._is_stopped and not got_result:
                    stderr_bytes = await process.stderr.read() if process.stderr else b""
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                    raw_joined = "\n".join(self._raw_output_lines[-20:])
                    err_cat = self.classify_error(process.returncode, stderr_text, raw_joined)
                    self._current_process = None
                    detail = stderr_text or raw_joined or "process exited with non-zero status"
                    yield HarnessEvent(
                        type="error",
                        harness="agy",
                        status="ERROR",
                        error_category=err_cat,
                        response=f"agy error (exit code {process.returncode}): {detail}",
                    )
        except asyncio.TimeoutError:
            await self.stop()
            yield HarnessEvent(
                type="error",
                harness="agy",
                status="TIMEOUT",
                error_category=ErrorCategory.TIMEOUT,
                response=f"agy process timed out after {request.timeout_seconds} seconds.",
            )
        except asyncio.CancelledError:
            await self.stop()
            raise
        finally:
            if not self.keep_alive:
                if self._reader_task and not self._reader_task.done():
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except asyncio.CancelledError:
                        pass
                self._reader_task = None
                self._current_process = None

    def _parse_line(self, line: str) -> Optional[HarnessEvent]:
        """Parse one NDJSON line emitted by agy into a normalized HarnessEvent."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return HarnessEvent(type="raw", harness="agy", raw_text=line)

        if not isinstance(data, dict):
            return HarnessEvent(type="raw", harness="agy", raw_text=line)

        event_name = data.get("event")
        if event_name == "init":
            return HarnessEvent(
                type="init",
                harness="agy",
                session_id=data.get("conversation_id"),
                payload=data.get("init", {}) if isinstance(data.get("init"), dict) else {},
            )

        if event_name == "step_update":
            step = data.get("step_update", {})
            if not isinstance(step, dict):
                return HarnessEvent(type="raw", harness="agy", raw_text=line, payload=data)

            step_type = step.get("step_type")
            conv_id = step.get("conversation_id")
            step_idx = step.get("step_index")

            if step_type == "tool":
                raw_tool = step.get("tool_name")
                raw_info = step.get("tool_info", {})
                norm_info = _normalize_agy_tool_info(raw_tool, raw_info if isinstance(raw_info, dict) else {})
                return HarnessEvent(
                    type="tool",
                    harness="agy",
                    session_id=conv_id,
                    step_index=step_idx,
                    canonical_tool=normalize_tool_name(raw_tool),
                    raw_tool_name=raw_tool,
                    tool_state=step.get("state"),
                    tool_info=norm_info,
                    duration_seconds=step.get("duration_seconds"),
                    payload=step,
                )

            if step_type == "agent_response":
                return HarnessEvent(
                    type="text_delta",
                    harness="agy",
                    session_id=conv_id,
                    step_index=step_idx,
                    text_delta=step.get("text_delta", ""),
                    duration_seconds=step.get("duration_seconds"),
                    usage=UsageMetrics.from_dict(step.get("usage")),
                    payload=step,
                )

            if step_type == "checkpoint":
                return HarnessEvent(
                    type="checkpoint",
                    harness="agy",
                    session_id=conv_id,
                    step_index=step_idx,
                    duration_seconds=step.get("duration_seconds"),
                    usage=UsageMetrics.from_dict(step.get("usage")),
                    payload=step,
                )

            return HarnessEvent(
                type="raw",
                harness="agy",
                session_id=conv_id,
                step_index=step_idx,
                raw_text=line,
                payload=step,
            )

        if event_name == "result":
            res = data.get("result", {})
            if not isinstance(res, dict):
                res = {}
            status = str(res.get("status", "SUCCESS") or "SUCCESS")
            response_text = str(res.get("response", "") or "")
            error_text = str(res.get("error", "") or "")
            if not response_text and error_text:
                response_text = error_text

            err_cat = ErrorCategory.NONE
            if status.upper() in ("ERROR", "FAILED", "FAILURE"):
                classified = self.classify_error(1, error_text, response_text)
                err_cat = classified if classified != ErrorCategory.NONE else ErrorCategory.PROCESS_CRASH

            return HarnessEvent(
                type="result",
                harness="agy",
                session_id=res.get("conversation_id"),
                status=status,
                error_category=err_cat,
                response=response_text,
                duration_seconds=res.get("duration_seconds"),
                usage=UsageMetrics.from_dict(res.get("usage")),
                payload=res,
            )

        return HarnessEvent(type="raw", harness="agy", raw_text=line, payload=data)

    async def stop(self) -> None:
        """Interrupt and clean up any running agy subprocess."""
        self._cancel_idle_timer()
        self._is_stopped = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("Exception awaiting cancelled reader task: %s", exc)
        self._reader_task = None

        await self._terminate_process(self._current_process, grace_seconds=2.0)
        self._current_process = None
        self._current_session_id = None


def _normalize_agy_tool_info(tool_name: Optional[str], raw_info: Dict[str, Any]) -> Dict[str, Any]:
    """Attach canonical summary/path/command fields while preserving raw parameters."""
    params = raw_info.get("parameters", {}) if isinstance(raw_info.get("parameters"), dict) else {}
    summary = ""
    path = params.get("Path") or params.get("TargetFile") or params.get("target_file") or params.get("DirectoryPath") or ""
    command = params.get("CommandLine") or ""

    if command:
        summary = command[:100]
    elif path:
        summary = Path(str(path)).name or str(path)
    elif params.get("Query"):
        summary = str(params["Query"])[:60]
    elif params.get("query"):
        summary = str(params["query"])[:60]
    elif params.get("Pattern"):
        summary = str(params["Pattern"])[:60]

    normalized = dict(raw_info)
    normalized.setdefault("summary", summary)
    if path:
        normalized.setdefault("path", str(path))
    if command:
        normalized.setdefault("command", str(command))
    return normalized


if __name__ == "__main__":
    async def _main() -> None:
        harness = AgyHarness()
        health = await harness.check_health()
        print("Health:", health)
        sample_req = RunRequest(
            prompt="Check project status",
            working_dir=os.getcwd(),
            model="gemini-3.8-flash",
            effort="high",
            permission_preset=PermissionPreset.AUTO,
        )
        print("Sample command:", " ".join(harness.build_command(sample_req)))
        parsed = parse_agy_models(
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        )
        assert len(parsed) == 2
        assert model_supports_effort("gemini-3.8-flash")
        assert not model_supports_effort("gemini-3.8-flash-high")
        print("harness.agy_harness self-test passed!")

    asyncio.run(_main())
