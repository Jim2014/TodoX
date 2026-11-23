"""Anthropic Claude Code CLI adapter implementing the TodoX BaseHarness contract."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

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

DEFAULT_CLAUDE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="claude-sonnet-4-6",
        name="Claude Sonnet 4.6",
        harness="claude",
        icon="✳️",
        description="Fast balanced coding model",
    ),
    ModelInfo(
        id="claude-opus-4-6",
        name="Claude Opus 4.6",
        harness="claude",
        icon="🧠",
        description="Deep architectural reasoning model",
    ),
    ModelInfo(
        id="claude-haiku-4-5",
        name="Claude Haiku 4.5",
        harness="claude",
        icon="⚡",
        description="Lightweight high-speed classification model",
    ),
]


def resolve_default_claude_bin() -> str:
    """Resolve the Claude Code CLI binary dynamically from env, PATH, or standard locations."""
    env_bin = os.environ.get("CLAUDE_BIN_PATH", "").strip()
    if env_bin:
        return env_bin

    found = shutil.which("claude")
    if found:
        return found

    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "claude",
        home / ".npm-global" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.is_file():
            return str(candidate)

    return "claude"


class ClaudeCodeHarness(BaseHarness):
    """Subprocess adapter for Claude Code CLI (`claude -p --output-format stream-json`)."""

    def __init__(self, claude_bin: Optional[str] = None, **kwargs: Any) -> None:
        self.claude_bin = claude_bin or resolve_default_claude_bin()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stopped = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            name="claude",
            display_name="Claude Code CLI",
            supports_streaming=True,
            supports_steering=False,
            supports_warm_session=False,
            supports_session_resume=True,
            supports_effort=False,
            supports_model_discovery=False,
            supports_json_schema=True,
            supported_presets=("auto", "accept-edits", "plan", "turbo"),
        )

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def check_health(self) -> HealthStatus:
        """Run non-billable `claude --version` binary readiness check."""
        resolved = shutil.which(self.claude_bin) if not os.path.isabs(self.claude_bin) else self.claude_bin
        if not resolved or not os.path.isfile(resolved):
            return HealthStatus(
                harness="claude",
                available=False,
                binary_path=self.claude_bin,
                message=f"Claude CLI binary not found at '{self.claude_bin}'",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            ver = stdout.decode("utf-8", errors="replace").strip() or stderr.decode("utf-8", errors="replace").strip()
            return HealthStatus(
                harness="claude",
                available=(proc.returncode == 0),
                binary_path=resolved,
                version=ver[:64] if ver else None,
                message="OK" if proc.returncode == 0 else f"Exit {proc.returncode}",
            )
        except Exception as exc:
            return HealthStatus(
                harness="claude",
                available=False,
                binary_path=resolved,
                message=str(exc),
            )

    async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        """Return curated Claude Code model options."""
        return list(DEFAULT_CLAUDE_MODELS)

    def build_command(self, request: RunRequest) -> List[str]:
        """Construct non-interactive `claude -p --output-format stream-json` CLI args."""
        preset = normalize_permission_preset(request.permission_preset)
        cmd: List[str] = [
            self.claude_bin,
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if preset == PermissionPreset.TURBO:
            cmd.append("--dangerously-skip-permissions")
        elif preset == PermissionPreset.ACCEPT_EDITS:
            cmd.extend(["--permission-mode", "acceptEdits"])
        elif preset == PermissionPreset.PLAN:
            cmd.extend(["--permission-mode", "plan"])
        else:
            cmd.extend(["--permission-mode", "default"])

        if request.model and request.model != "default":
            cmd.extend(["--model", str(request.model)])
        if request.session_id:
            cmd.extend(["--resume", str(request.session_id)])
        return cmd

    def _parse_ndjson_line(self, line: str) -> List[HarnessEvent]:
        """Parse one line from `claude --output-format stream-json` into normalized events."""
        raw = line.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return [HarnessEvent(type="text_delta", harness="claude", text_delta=f"{raw}\n", raw_text=raw)]
        if not isinstance(data, dict):
            return []

        ev_type = str(data.get("type") or "").strip().lower()
        events: List[HarnessEvent] = []

        if ev_type == "system":
            sid = data.get("session_id")
            if sid:
                events.append(HarnessEvent(type="init", harness="claude", session_id=str(sid), payload=data))
            return events

        if ev_type == "assistant":
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            content = msg.get("content") if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    b_type = str(block.get("type") or "").lower()
                    if b_type == "text" and block.get("text"):
                        events.append(
                            HarnessEvent(
                                type="text_delta",
                                harness="claude",
                                text_delta=str(block["text"]),
                                payload=block,
                            )
                        )
                    elif b_type == "tool_use":
                        tool_name = str(block.get("name") or "tool")
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        summary = str(
                            inp.get("command")
                            or inp.get("file_path")
                            or inp.get("path")
                            or inp.get("pattern")
                            or tool_name
                        )
                        events.append(
                            HarnessEvent(
                                type="tool",
                                harness="claude",
                                canonical_tool=normalize_tool_name(tool_name),
                                raw_tool_name=tool_name,
                                tool_state="ACTIVE",
                                tool_info={"summary": summary[:120], **inp},
                                payload=block,
                            )
                        )
            return events

        if ev_type == "user":
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and str(block.get("type") or "").lower() == "tool_result":
                        is_err = bool(block.get("is_error"))
                        events.append(
                            HarnessEvent(
                                type="tool",
                                harness="claude",
                                canonical_tool=normalize_tool_name(str(block.get("tool_name") or "tool")),
                                raw_tool_name=str(block.get("tool_name") or "tool"),
                                tool_state="ERROR" if is_err else "DONE",
                                tool_info={"summary": "Tool failed" if is_err else "Tool completed"},
                                payload=block,
                            )
                        )
            return events

        if ev_type == "result":
            usage_dict = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            if "total_cost_usd" in data and "total_cost_usd" not in usage_dict:
                usage_dict = {**usage_dict, "total_cost_usd": data.get("total_cost_usd")}
            usage = UsageMetrics.from_dict(usage_dict)
            is_error = bool(data.get("is_error")) or str(data.get("subtype") or "").startswith("error")
            resp_text = str(data.get("result") or "")
            dur_ms = data.get("duration_ms")
            dur_s = round(float(dur_ms) / 1000.0, 3) if isinstance(dur_ms, (int, float)) else None
            err_cat = ErrorCategory.NONE
            if is_error:
                err_cat = self.classify_error(1, stderr=resp_text, output=resp_text)
                if err_cat == ErrorCategory.NONE:
                    err_cat = ErrorCategory.PROCESS_CRASH
            events.append(
                HarnessEvent(
                    type="result",
                    harness="claude",
                    session_id=str(data.get("session_id") or "") or None,
                    status="ERROR" if is_error else "SUCCESS",
                    error_category=err_cat,
                    response=resp_text,
                    duration_seconds=dur_s,
                    usage=usage,
                    payload=data,
                )
            )
            return events

        return []

    async def stop(self) -> None:
        """Terminate active Claude subprocess cleanly."""
        self._stopped = True
        await self._terminate_process(self._proc)

    async def run_stream(self, request: RunRequest) -> AsyncIterator[HarnessEvent]:
        """Run a headless Claude Code turn and yield normalized HarnessEvent items."""
        self._stopped = False
        resolved = shutil.which(self.claude_bin) if not os.path.isabs(self.claude_bin) else self.claude_bin
        if not resolved or not os.path.isfile(resolved):
            yield HarnessEvent(
                type="result",
                harness="claude",
                status="ERROR",
                error_category=ErrorCategory.BINARY_NOT_FOUND,
                response=f"Claude CLI binary not found: {self.claude_bin}",
            )
            return

        cmd = self.build_command(request)
        cmd[0] = resolved
        started_at = time.time()
        try:
            self._proc = await self._spawn_subprocess(
                cmd=cmd,
                cwd=request.working_dir,
                env_overrides=request.env_overrides,
                limit=DEFAULT_STREAM_LIMIT_BYTES,
            )
        except FileNotFoundError:
            yield HarnessEvent(
                type="result",
                harness="claude",
                status="ERROR",
                error_category=ErrorCategory.BINARY_NOT_FOUND,
                response=f"Claude CLI binary not found: {resolved}",
            )
            return

        if self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.close()

        saw_terminal_result = False
        collected_text: List[str] = []
        stderr_lines: List[str] = []

        async def _drain_stderr() -> None:
            if not self._proc or not self._proc.stderr:
                return
            while True:
                raw_err = await self._proc.stderr.readline()
                if not raw_err:
                    break
                stderr_lines.append(raw_err.decode("utf-8", errors="replace").rstrip("\n"))

        stderr_task = asyncio.create_task(_drain_stderr())

        async def _read_stdout_events() -> AsyncIterator[HarnessEvent]:
            nonlocal saw_terminal_result
            if not self._proc or not self._proc.stdout:
                return
            while True:
                raw_line = await self._proc.stdout.readline()
                if not raw_line:
                    break
                decoded = raw_line.decode("utf-8", errors="replace")
                for ev in self._parse_ndjson_line(decoded):
                    if ev.type == "text_delta" and ev.text_delta:
                        collected_text.append(ev.text_delta)
                    elif ev.type == "result":
                        saw_terminal_result = True
                        if not ev.response and collected_text:
                            ev.response = "".join(collected_text).strip()
                    yield ev

        try:
            deadline = started_at + max(1, int(request.timeout_seconds))
            gen = _read_stdout_events()
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                try:
                    ev = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
                    yield ev
                except StopAsyncIteration:
                    break

            rc = await self._proc.wait()
            await stderr_task
            if not saw_terminal_result:
                stderr_txt = "\n".join(stderr_lines).strip()
                combined_txt = "".join(collected_text).strip()
                if self._stopped:
                    yield HarnessEvent(
                        type="result",
                        harness="claude",
                        status="CANCELLED",
                        error_category=ErrorCategory.CANCELLED,
                        duration_seconds=round(time.time() - started_at, 3),
                        response="Execution cancelled by Admin.",
                    )
                else:
                    cat = self.classify_error(rc, stderr=stderr_txt, output=combined_txt)
                    ok = (rc == 0 and cat == ErrorCategory.NONE)
                    yield HarnessEvent(
                        type="result",
                        harness="claude",
                        status="SUCCESS" if ok else "ERROR",
                        error_category=ErrorCategory.NONE if ok else cat,
                        duration_seconds=round(time.time() - started_at, 3),
                        response=combined_txt if ok else (stderr_txt or combined_txt or f"Claude exited with code {rc}"),
                    )
        except asyncio.TimeoutError:
            await self._terminate_process(self._proc)
            await stderr_task
            yield HarnessEvent(
                type="result",
                harness="claude",
                status="TIMEOUT",
                error_category=ErrorCategory.TIMEOUT,
                duration_seconds=round(time.time() - started_at, 3),
                response=f"Claude CLI timed out after {request.timeout_seconds}s.",
            )
        finally:
            self._proc = None


if __name__ == "__main__":
    async def _main() -> None:
        h = ClaudeCodeHarness(claude_bin="/usr/local/bin/claude")
        cmd = h.build_command(
            RunRequest(
                prompt="Run tests",
                working_dir=os.getcwd(),
                model="claude-sonnet-4-6",
                permission_preset=PermissionPreset.ACCEPT_EDITS,
            )
        )
        assert "--output-format" in cmd and "stream-json" in cmd and "--permission-mode" in cmd
        assert "acceptEdits" in cmd
        parsed_init = h._parse_ndjson_line('{"type":"system","session_id":"sess-1"}')
        assert parsed_init[0].type == "init" and parsed_init[0].session_id == "sess-1"
        parsed_tool = h._parse_ndjson_line(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{"file_path":"app.py"}}]}}'
        )
        assert parsed_tool[0].canonical_tool.value == "edit"
        health = await h.check_health()
        assert isinstance(health.available, bool)
        print("harness.claude_harness self-test passed!")

    asyncio.run(_main())
