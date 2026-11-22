"""3-level harness profile routing, step execution, and event normalization."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .base import BaseHarness
from .registry import HarnessRegistry, create_default_registry
from .types import (
    ErrorCategory,
    HarnessEvent,
    HarnessProfile,
    PermissionPreset,
    RunRequest,
    UsageMetrics,
    normalize_permission_preset,
)


@dataclass
class StepResult:
    """Aggregated outcome from executing one TodoX step via HarnessRouter."""

    success: bool
    harness: str
    model: Optional[str] = None
    session_id: Optional[str] = None
    response: str = ""
    status: str = "SUCCESS"
    error_category: ErrorCategory = ErrorCategory.NONE
    error_message: str = ""
    duration_seconds: float = 0.0
    usage: UsageMetrics = field(default_factory=UsageMetrics)
    tool_events: List[str] = field(default_factory=list)
    output_lines: List[str] = field(default_factory=list)
    fallback_switched: bool = False
    backoff_triggered: bool = False


def format_event_log_line(event: HarnessEvent) -> Optional[str]:
    """Format a normalized HarnessEvent into a concise single-line log string."""
    if event.type == "init":
        return f"[init] session={event.session_id or 'new'}"

    if event.type == "tool":
        tool_label = event.canonical_tool.value if event.canonical_tool else (event.raw_tool_name or "tool")
        state = event.tool_state or "ACTIVE"
        summary = event.tool_info.get("summary", "") if isinstance(event.tool_info, dict) else ""
        dur = f" ({event.duration_seconds:.1f}s)" if event.duration_seconds else ""
        detail = f": {summary}" if summary else ""
        return f"[tool:{tool_label}] {state}{detail}{dur}"

    if event.type == "text_delta" and event.text_delta:
        return event.text_delta.rstrip("\n")

    if event.type == "turn_complete":
        return f"[turn_complete] status={event.status}"

    if event.type == "result":
        tok = event.usage.total_tokens if event.usage else 0
        dur = f" {event.duration_seconds:.1f}s" if event.duration_seconds else ""
        return f"[result] status={event.status} tokens={tok}{dur}"

    if event.type == "error":
        return f"[error:{event.error_category.value}] {event.response}"

    if event.type == "raw" and event.raw_text:
        return event.raw_text

    return None


def _extract_config_dict(task_obj: Any) -> Dict[str, Any]:
    """Extract harness_config as a dictionary from a Task model or dict."""
    if task_obj is None:
        return {}
    cfg = task_obj.get("harness_config") if isinstance(task_obj, dict) else getattr(task_obj, "harness_config", None)
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return cfg
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(exclude_none=True)
    return {}


class HarnessRouter:
    """Resolves task/project/global harness profiles and runs single steps safely."""

    def __init__(
        self,
        registry: Optional[HarnessRegistry] = None,
        state_manager: Any = None,
    ) -> None:
        self.registry = registry or create_default_registry()
        self.state_manager = state_manager

    def _global_profile(self) -> HarnessProfile:
        """Read global default HarnessProfile from state_manager or fall back to defaults."""
        if self.state_manager is not None:
            if hasattr(self.state_manager, "get_current_profile"):
                return self.state_manager.get_current_profile()
            if hasattr(self.state_manager, "get_state"):
                st = self.state_manager.get_state()
                return HarnessProfile.from_dict(
                    {
                        "harness": st.get("default_harness", "agy"),
                        "model": st.get("current_model") or st.get("default_model"),
                        "effort": st.get("default_effort", "high"),
                        "permission_preset": st.get("default_permission_preset", "auto"),
                    }
                )
        return HarnessProfile(
            harness="agy",
            model="default",
            effort="high",
            permission_preset=PermissionPreset.AUTO,
            session_mode="stateless",
        )

    def resolve_profile(
        self,
        project_task: Any = None,
        active_task: Any = None,
    ) -> HarnessProfile:
        """
        Resolve effective HarnessProfile using 3-level precedence:
        Task override > Project override > Global default.
        Partial overrides merge field by field so unspecified fields inherit cleanly.
        """
        base = self._global_profile()
        merged: Dict[str, Any] = base.to_dict()

        for layer in (_extract_config_dict(project_task), _extract_config_dict(active_task)):
            if not layer:
                continue
            for key in ("harness", "model", "effort", "permission_preset", "session_mode"):
                val = layer.get(key)
                if val is not None and str(val).strip() != "":
                    merged[key] = val

        return HarnessProfile.from_dict(merged)

    async def run_step(
        self,
        request: RunRequest,
        profile: Optional[HarnessProfile] = None,
        on_event: Optional[Callable[[HarnessEvent], Any]] = None,
        harness_instance: Optional[BaseHarness] = None,
        on_harness_started: Optional[Callable[[BaseHarness], Any]] = None,
    ) -> StepResult:
        """Execute a single TodoX step through the resolved harness and aggregate results."""
        eff_profile = profile or self._global_profile()
        harness_name = eff_profile.harness

        if harness_instance is None:
            if not self.registry.has(harness_name):
                msg = f"Harness '{harness_name}' is not registered."
                return StepResult(
                    success=False,
                    harness=harness_name,
                    model=eff_profile.model,
                    status="ERROR",
                    error_category=ErrorCategory.BINARY_NOT_FOUND,
                    error_message=msg,
                )
            harness = self.registry.create(harness_name, keep_alive=False)
        else:
            harness = harness_instance

        effective_request = RunRequest(
            prompt=request.prompt,
            working_dir=request.working_dir,
            system_prompt=request.system_prompt,
            followup_system_prompt=request.followup_system_prompt,
            model=request.model if request.model is not None else eff_profile.model,
            effort=request.effort if request.effort is not None else eff_profile.effort,
            permission_preset=normalize_permission_preset(
                request.permission_preset or eff_profile.permission_preset
            ),
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
            extra_dirs=list(request.extra_dirs),
            env_overrides=dict(request.env_overrides),
            json_schema=request.json_schema,
        )

        tool_events: List[str] = []
        output_lines: List[str] = []
        accumulated_text: List[str] = []
        final_response = ""
        final_status = "SUCCESS"
        final_error_cat = ErrorCategory.NONE
        final_error_msg = ""
        session_id = effective_request.session_id
        usage = UsageMetrics()
        duration_seconds = 0.0
        saw_error = False

        try:
            if on_harness_started is not None:
                maybe_started = on_harness_started(harness)
                if inspect.isawaitable(maybe_started):
                    await maybe_started

            async for event in harness.run_stream(effective_request):
                if on_event is not None:
                    maybe_coro = on_event(event)
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro

                log_line = format_event_log_line(event)
                if log_line:
                    output_lines.append(log_line)

                if event.session_id:
                    session_id = event.session_id

                if event.type == "tool":
                    if log_line:
                        if tool_events and "ACTIVE" in tool_events[-1] and event.tool_state != "ACTIVE":
                            tool_events[-1] = log_line
                        else:
                            tool_events.append(log_line)

                elif event.type == "text_delta" and event.text_delta:
                    accumulated_text.append(event.text_delta)

                elif event.type in ("result", "turn_complete"):
                    if event.response:
                        final_response = event.response
                    final_status = event.status or "SUCCESS"
                    if event.usage and (event.usage.total_tokens or event.usage.input_tokens):
                        usage = event.usage
                    if event.duration_seconds is not None:
                        duration_seconds = float(event.duration_seconds)
                    if final_status.upper() in ("ERROR", "FAILED", "FAILURE"):
                        saw_error = True
                        final_error_cat = (
                            event.error_category
                            if event.error_category != ErrorCategory.NONE
                            else ErrorCategory.PROCESS_CRASH
                        )
                        final_error_msg = event.response or f"Harness step failed with status {final_status}"

                elif event.type == "error":
                    saw_error = True
                    final_status = event.status or "ERROR"
                    final_error_cat = (
                        event.error_category
                        if event.error_category != ErrorCategory.NONE
                        else ErrorCategory.PROCESS_CRASH
                    )
                    final_error_msg = event.response or "Harness execution failed"
        finally:
            await harness.stop()

        if not final_response and accumulated_text:
            final_response = "".join(accumulated_text).strip()

        success = (not saw_error) and final_status.upper() not in ("ERROR", "FAILED", "FAILURE", "TIMEOUT", "CANCELLED")
        return StepResult(
            success=success,
            harness=harness_name,
            model=effective_request.model,
            session_id=session_id,
            response=final_response,
            status=final_status,
            error_category=final_error_cat,
            error_message=final_error_msg,
            duration_seconds=duration_seconds,
            usage=usage,
            tool_events=tool_events,
            output_lines=output_lines,
        )


if __name__ == "__main__":
    router = HarnessRouter()
    proj = {"harness_config": {"harness": "agy", "model": "gemini-3.1-pro", "permission_preset": "accept-edits"}}
    sub = {"harness_config": {"model": "gemini-3.8-flash"}}
    resolved = router.resolve_profile(project_task=proj, active_task=sub)
    assert resolved.harness == "agy"
    assert resolved.model == "gemini-3.8-flash"
    assert resolved.permission_preset == PermissionPreset.ACCEPT_EDITS
    print("harness.router self-test passed!")
