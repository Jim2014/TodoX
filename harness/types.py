"""Normalized types, enums, data contracts, and tool mapping for AI CLI harnesses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


class PermissionPreset(str, Enum):
    """Canonical execution permission presets shared across all agent harnesses."""

    PLAN = "plan"
    ACCEPT_EDITS = "accept-edits"
    AUTO = "auto"
    TURBO = "turbo"


_PERMISSION_ALIASES: Dict[str, PermissionPreset] = {
    "auto": PermissionPreset.AUTO,
    "sandbox": PermissionPreset.AUTO,
    "accept-edits": PermissionPreset.ACCEPT_EDITS,
    "accept_edits": PermissionPreset.ACCEPT_EDITS,
    "acceptedits": PermissionPreset.ACCEPT_EDITS,
    "edits": PermissionPreset.ACCEPT_EDITS,
    "plan": PermissionPreset.PLAN,
    "readonly": PermissionPreset.PLAN,
    "read-only": PermissionPreset.PLAN,
    "turbo": PermissionPreset.TURBO,
    "bypass": PermissionPreset.TURBO,
    "unrestricted": PermissionPreset.TURBO,
    "skip": PermissionPreset.TURBO,
    "dangerously-skip-permissions": PermissionPreset.TURBO,
}


def parse_permission_preset(raw: Optional[Union[str, PermissionPreset]]) -> Optional[PermissionPreset]:
    """Parse a preset enum or alias string into a canonical PermissionPreset, or None if invalid."""
    if isinstance(raw, PermissionPreset):
        return raw
    if not raw or not isinstance(raw, str):
        return None
    return _PERMISSION_ALIASES.get(raw.strip().lower())


def normalize_permission_preset(raw: Optional[Union[str, PermissionPreset]]) -> PermissionPreset:
    """Normalize a permission preset value, defaulting empty or unknown values to AUTO."""
    return parse_permission_preset(raw) or PermissionPreset.AUTO


class ErrorCategory(str, Enum):
    """Typed failure classification used by the router for fallback vs. alert decisions."""

    NONE = "none"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    TIMEOUT = "timeout"
    BINARY_NOT_FOUND = "binary_not_found"
    CANCELLED = "cancelled"
    PROCESS_CRASH = "process_crash"
    UNKNOWN = "unknown"


class CanonicalTool(str, Enum):
    """Normalized tool categories across agy, Claude Code, Codex CLI, and custom harnesses."""

    SHELL = "shell"
    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    SEARCH = "search"
    WEB = "web"
    AGENT = "agent"
    MCP = "mcp"
    QUESTION = "question"
    OTHER = "other"


_TOOL_NAME_MAP: Dict[str, CanonicalTool] = {
    # Shell / command execution
    "run_command": CanonicalTool.SHELL,
    "bash": CanonicalTool.SHELL,
    "command_execution": CanonicalTool.SHELL,
    "shell": CanonicalTool.SHELL,
    "exec_command": CanonicalTool.SHELL,
    # File read
    "read_file": CanonicalTool.READ,
    "view_file": CanonicalTool.READ,
    "read": CanonicalTool.READ,
    # File write / create
    "write_file": CanonicalTool.WRITE,
    "write_to_file": CanonicalTool.WRITE,
    "write": CanonicalTool.WRITE,
    # File edit / patch
    "edit_file": CanonicalTool.EDIT,
    "replace_file_content": CanonicalTool.EDIT,
    "multi_replace_file_content": CanonicalTool.EDIT,
    "edit": CanonicalTool.EDIT,
    "multiedit": CanonicalTool.EDIT,
    "file_change": CanonicalTool.EDIT,
    # Search / directory discovery
    "grep_search": CanonicalTool.SEARCH,
    "find_by_name": CanonicalTool.SEARCH,
    "list_dir": CanonicalTool.SEARCH,
    "grep": CanonicalTool.SEARCH,
    "glob": CanonicalTool.SEARCH,
    "file_search": CanonicalTool.SEARCH,
    # Web search / fetch
    "search_web": CanonicalTool.WEB,
    "read_url_content": CanonicalTool.WEB,
    "websearch": CanonicalTool.WEB,
    "webfetch": CanonicalTool.WEB,
    "web_search": CanonicalTool.WEB,
    # Subagents / swarm orchestration
    "invoke_subagent": CanonicalTool.AGENT,
    "manage_subagents": CanonicalTool.AGENT,
    "send_message": CanonicalTool.AGENT,
    "define_subagent": CanonicalTool.AGENT,
    "swarm": CanonicalTool.AGENT,
    "task": CanonicalTool.AGENT,
    "agent": CanonicalTool.AGENT,
    # MCP tools
    "mcp_tool_call": CanonicalTool.MCP,
    "mcp": CanonicalTool.MCP,
    # User clarification prompts
    "ask_question": CanonicalTool.QUESTION,
    "askuserquestion": CanonicalTool.QUESTION,
}


def normalize_tool_name(raw_name: Optional[str]) -> CanonicalTool:
    """Map an agent-specific tool name to a canonical tool category."""
    if not raw_name:
        return CanonicalTool.OTHER
    cleaned = raw_name.strip().lower()
    if cleaned.startswith("mcp__"):
        return CanonicalTool.MCP
    return _TOOL_NAME_MAP.get(cleaned, CanonicalTool.OTHER)


@dataclass(frozen=True)
class HarnessCapabilities:
    """Declares optional protocol capabilities supported by a CLI harness."""

    name: str
    display_name: str
    supports_streaming: bool = True
    supports_steering: bool = False
    supports_warm_session: bool = False
    supports_session_resume: bool = False
    supports_effort: bool = False
    supports_model_discovery: bool = False
    supports_json_schema: bool = False


@dataclass(frozen=True)
class ModelInfo:
    """Metadata describing a selectable model exposed by a harness."""

    id: str
    name: str
    harness: str
    raw_id: Optional[str] = None
    icon: str = "✨"
    description: str = ""


@dataclass(frozen=True)
class HealthStatus:
    """Non-billable binary & environment health check result for a harness."""

    harness: str
    available: bool
    binary_path: Optional[str] = None
    version: Optional[str] = None
    message: str = ""


@dataclass
class UsageMetrics:
    """Normalized token and cost metrics extracted from a harness turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "UsageMetrics":
        """Construct UsageMetrics from heterogeneous CLI usage dictionaries."""
        if not isinstance(data, dict):
            return cls()

        def _as_int(*keys: str) -> int:
            for k in keys:
                val = data.get(k)
                if val is not None:
                    try:
                        return max(0, int(val))
                    except (TypeError, ValueError):
                        pass
            return 0

        input_tok = _as_int("input_tokens", "prompt_tokens")
        output_tok = _as_int("output_tokens", "completion_tokens")
        thinking_tok = _as_int("thinking_tokens", "reasoning_tokens", "thoughts_tokens")
        cached_tok = _as_int("cached_tokens", "cache_read_input_tokens")
        total_tok = _as_int("total_tokens")
        if total_tok == 0 and (input_tok or output_tok or thinking_tok):
            total_tok = input_tok + output_tok + thinking_tok

        cost_val: Optional[float] = None
        for key in ("cost_usd", "total_cost_usd"):
            raw_cost = data.get(key)
            if raw_cost is not None:
                try:
                    cost_val = float(raw_cost)
                    break
                except (TypeError, ValueError):
                    pass

        return cls(
            input_tokens=input_tok,
            output_tokens=output_tok,
            thinking_tokens=thinking_tok,
            cached_tokens=cached_tok,
            total_tokens=total_tok,
            cost_usd=cost_val,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize usage metrics to a plain dictionary."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class HarnessProfile:
    """Resolved execution configuration for running a TodoX step."""

    harness: str = "agy"
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_preset: PermissionPreset = PermissionPreset.AUTO
    session_mode: str = "stateless"  # "stateless" | "sticky"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize profile to a JSON-friendly dictionary."""
        return {
            "harness": self.harness,
            "model": self.model,
            "effort": self.effort,
            "permission_preset": self.permission_preset.value,
            "session_mode": self.session_mode,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "HarnessProfile":
        """Construct a HarnessProfile from a dictionary."""
        if not isinstance(data, dict):
            return cls()
        raw_session_mode = str(data.get("session_mode") or "stateless").strip().lower()
        session_mode = "sticky" if raw_session_mode == "sticky" else "stateless"
        return cls(
            harness=str(data.get("harness") or "agy").strip().lower(),
            model=data.get("model"),
            effort=data.get("effort"),
            permission_preset=normalize_permission_preset(data.get("permission_preset")),
            session_mode=session_mode,
        )


@dataclass
class RunRequest:
    """Normalized execution request passed into BaseHarness.run_stream()."""

    prompt: str
    working_dir: str
    system_prompt: Optional[str] = None
    followup_system_prompt: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_preset: PermissionPreset = PermissionPreset.AUTO
    session_id: Optional[str] = None
    timeout_seconds: int = 300
    extra_dirs: List[str] = field(default_factory=list)
    env_overrides: Dict[str, str] = field(default_factory=dict)
    json_schema: Optional[str] = None


EventType = Literal[
    "init",
    "tool",
    "thinking",
    "text_delta",
    "checkpoint",
    "turn_complete",
    "result",
    "error",
    "raw",
]


@dataclass
class HarnessEvent:
    """Unified streaming event emitted by any BaseHarness implementation."""

    type: EventType
    harness: str = ""
    session_id: Optional[str] = None
    step_index: Optional[int] = None
    canonical_tool: Optional[CanonicalTool] = None
    raw_tool_name: Optional[str] = None
    tool_state: Optional[str] = None  # "ACTIVE" | "DONE" | "ERROR"
    tool_info: Dict[str, Any] = field(default_factory=dict)
    text_delta: str = ""
    response: str = ""
    status: str = ""  # "SUCCESS" | "ERROR" | "TIMEOUT" | "CANCELLED"
    error_category: ErrorCategory = ErrorCategory.NONE
    duration_seconds: Optional[float] = None
    usage: UsageMetrics = field(default_factory=UsageMetrics)
    raw_text: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


if __name__ == "__main__":
    assert normalize_permission_preset("sandbox") == PermissionPreset.AUTO
    assert normalize_permission_preset("dangerously-skip-permissions") == PermissionPreset.TURBO
    assert normalize_tool_name("run_command") == CanonicalTool.SHELL
    assert normalize_tool_name("Bash") == CanonicalTool.SHELL
    assert normalize_tool_name("command_execution") == CanonicalTool.SHELL
    assert normalize_tool_name("mcp__github__list_prs") == CanonicalTool.MCP
    usage = UsageMetrics.from_dict({"input_tokens": 100, "output_tokens": 50, "thinking_tokens": 25})
    assert usage.total_tokens == 175
    print("harness.types self-test passed!")
