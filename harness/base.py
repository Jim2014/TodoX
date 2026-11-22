"""Abstract base class and shared subprocess utilities for AI CLI harnesses."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from .types import (
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HealthStatus,
    ModelInfo,
    RunRequest,
)

# 16 MiB pipe buffer prevents StreamReader 'chunk is longer than limit' errors on large NDJSON lines.
DEFAULT_STREAM_LIMIT_BYTES = 16 * 1024 * 1024

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "resource_exhausted",
    "exhausted your capacity",
)

_AUTH_ERROR_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "invalid api key",
    "api key not valid",
    "not logged in",
    "authentication failed",
    "authentication required",
    "token expired",
    "permission denied",
)

_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "deadline exceeded",
)

_MISSING_BIN_MARKERS = (
    "no such file or directory",
    "not found",
    "cannot find the file",
)


class BaseHarness(ABC):
    """Abstract adapter interface implemented by every supported AI agent CLI."""

    @property
    @abstractmethod
    def capabilities(self) -> HarnessCapabilities:
        """Return static capability flags for this harness."""

    @abstractmethod
    async def check_health(self) -> HealthStatus:
        """Verify binary availability and basic CLI readiness without executing an LLM turn."""

    @abstractmethod
    async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
        """Return available models discovered from the CLI or curated static defaults."""

    @abstractmethod
    def run_stream(self, request: RunRequest) -> AsyncIterator[HarnessEvent]:
        """Execute a single prompt turn and yield normalized HarnessEvent objects."""

    async def steer(self, message: str) -> bool:
        """Inject mid-flight guidance into an active session when supported."""
        return False

    @abstractmethod
    async def stop(self) -> None:
        """Interrupt and clean up any active subprocess owned by this harness instance."""

    def is_running(self) -> bool:
        """Return True if a subprocess owned by this harness is currently active."""
        return False

    def classify_error(
        self,
        exit_code: Optional[int],
        stderr: str = "",
        output: str = "",
    ) -> ErrorCategory:
        """Classify CLI failure text and exit codes into canonical ErrorCategory values."""
        combined = f"{stderr}\n{output}".lower()

        if any(marker in combined for marker in _RATE_LIMIT_MARKERS):
            return ErrorCategory.RATE_LIMIT
        if any(marker in combined for marker in _AUTH_ERROR_MARKERS):
            return ErrorCategory.AUTH_ERROR
        if any(marker in combined for marker in _TIMEOUT_MARKERS):
            return ErrorCategory.TIMEOUT
        if exit_code == 127 or any(marker in combined for marker in _MISSING_BIN_MARKERS):
            return ErrorCategory.BINARY_NOT_FOUND
        if exit_code is not None and exit_code != 0:
            return ErrorCategory.PROCESS_CRASH
        if combined.strip():
            return ErrorCategory.UNKNOWN
        return ErrorCategory.NONE

    async def _spawn_subprocess(
        self,
        cmd: List[str],
        cwd: str,
        env_overrides: Optional[Dict[str, str]] = None,
        limit: int = DEFAULT_STREAM_LIMIT_BYTES,
    ) -> asyncio.subprocess.Process:
        """Spawn a headless subprocess with a generous NDJSON StreamReader buffer limit."""
        Path(cwd).mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)

        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=limit,
        )

    async def _terminate_process(
        self,
        proc: Optional[asyncio.subprocess.Process],
        grace_seconds: float = 2.0,
    ) -> None:
        """Terminate a subprocess gracefully (SIGTERM -> grace period -> SIGKILL)."""
        if not proc or proc.returncode is not None:
            return

        try:
            if proc.stdin and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    class _DummyHarness(BaseHarness):
        @property
        def capabilities(self) -> HarnessCapabilities:
            return HarnessCapabilities(name="dummy", display_name="Dummy")

        async def check_health(self) -> HealthStatus:
            return HealthStatus(harness="dummy", available=True)

        async def list_models(self, force_refresh: bool = False) -> List[ModelInfo]:
            return []

        async def run_stream(self, request: RunRequest) -> AsyncIterator[HarnessEvent]:
            yield HarnessEvent(type="result", harness="dummy", status="SUCCESS", response=request.prompt)

        async def stop(self) -> None:
            return None

    dummy = _DummyHarness()
    assert dummy.classify_error(1, "HTTP 429 Too Many Requests") == ErrorCategory.RATE_LIMIT
    assert dummy.classify_error(1, "401 Unauthorized") == ErrorCategory.AUTH_ERROR
    assert dummy.classify_error(127, "") == ErrorCategory.BINARY_NOT_FOUND
    assert dummy.classify_error(1, "segfault") == ErrorCategory.PROCESS_CRASH
    print("harness.base self-test passed!")
