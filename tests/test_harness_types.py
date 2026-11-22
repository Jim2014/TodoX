import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.base import BaseHarness
from harness.types import (
    CanonicalTool,
    ErrorCategory,
    HarnessCapabilities,
    HarnessEvent,
    HarnessProfile,
    HealthStatus,
    ModelInfo,
    PermissionPreset,
    RunRequest,
    UsageMetrics,
    normalize_permission_preset,
    normalize_tool_name,
    parse_permission_preset,
)


class _SampleHarness(BaseHarness):
    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name="sample", display_name="Sample")

    async def check_health(self) -> HealthStatus:
        return HealthStatus(harness="sample", available=True)

    async def list_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        return []

    async def run_stream(self, request: RunRequest):
        yield HarnessEvent(type="result", harness="sample", status="SUCCESS")

    async def stop(self) -> None:
        return None


class TestHarnessTypes(unittest.TestCase):
    def test_permission_preset_parsing_and_normalization(self):
        self.assertEqual(parse_permission_preset("AUTO"), PermissionPreset.AUTO)
        self.assertEqual(parse_permission_preset("sandbox"), PermissionPreset.AUTO)
        self.assertEqual(parse_permission_preset("accept_edits"), PermissionPreset.ACCEPT_EDITS)
        self.assertEqual(parse_permission_preset("acceptedits"), PermissionPreset.ACCEPT_EDITS)
        self.assertEqual(parse_permission_preset("readonly"), PermissionPreset.PLAN)
        self.assertEqual(parse_permission_preset("dangerously-skip-permissions"), PermissionPreset.TURBO)
        self.assertIsNone(parse_permission_preset("invalid-mode"))
        self.assertEqual(normalize_permission_preset("invalid-mode"), PermissionPreset.AUTO)
        self.assertEqual(normalize_permission_preset(PermissionPreset.PLAN), PermissionPreset.PLAN)

    def test_tool_name_normalization_across_clis(self):
        self.assertEqual(normalize_tool_name("run_command"), CanonicalTool.SHELL)
        self.assertEqual(normalize_tool_name("Bash"), CanonicalTool.SHELL)
        self.assertEqual(normalize_tool_name("command_execution"), CanonicalTool.SHELL)
        self.assertEqual(normalize_tool_name("view_file"), CanonicalTool.READ)
        self.assertEqual(normalize_tool_name("Read"), CanonicalTool.READ)
        self.assertEqual(normalize_tool_name("write_to_file"), CanonicalTool.WRITE)
        self.assertEqual(normalize_tool_name("Write"), CanonicalTool.WRITE)
        self.assertEqual(normalize_tool_name("replace_file_content"), CanonicalTool.EDIT)
        self.assertEqual(normalize_tool_name("MultiEdit"), CanonicalTool.EDIT)
        self.assertEqual(normalize_tool_name("file_change"), CanonicalTool.EDIT)
        self.assertEqual(normalize_tool_name("grep_search"), CanonicalTool.SEARCH)
        self.assertEqual(normalize_tool_name("Glob"), CanonicalTool.SEARCH)
        self.assertEqual(normalize_tool_name("file_search"), CanonicalTool.SEARCH)
        self.assertEqual(normalize_tool_name("search_web"), CanonicalTool.WEB)
        self.assertEqual(normalize_tool_name("WebFetch"), CanonicalTool.WEB)
        self.assertEqual(normalize_tool_name("web_search"), CanonicalTool.WEB)
        self.assertEqual(normalize_tool_name("invoke_subagent"), CanonicalTool.AGENT)
        self.assertEqual(normalize_tool_name("Task"), CanonicalTool.AGENT)
        self.assertEqual(normalize_tool_name("mcp__github__list_issues"), CanonicalTool.MCP)
        self.assertEqual(normalize_tool_name("ask_question"), CanonicalTool.QUESTION)
        self.assertEqual(normalize_tool_name("unknown_tool"), CanonicalTool.OTHER)

    def test_usage_metrics_from_dict(self):
        u = UsageMetrics.from_dict(
            {
                "input_tokens": 1200,
                "output_tokens": 300,
                "reasoning_tokens": 150,
                "cache_read_input_tokens": 400,
                "total_cost_usd": "0.0125",
            }
        )
        self.assertEqual(u.input_tokens, 1200)
        self.assertEqual(u.output_tokens, 300)
        self.assertEqual(u.thinking_tokens, 150)
        self.assertEqual(u.cached_tokens, 400)
        self.assertEqual(u.total_tokens, 1650)
        self.assertAlmostEqual(u.cost_usd, 0.0125)
        self.assertEqual(u.to_dict()["total_tokens"], 1650)

    def test_harness_profile_roundtrip(self):
        profile = HarnessProfile.from_dict(
            {
                "harness": "AGY",
                "model": "gemini-3.8-flash",
                "effort": "high",
                "permission_preset": "readonly",
                "session_mode": "sticky",
            }
        )
        self.assertEqual(profile.harness, "agy")
        self.assertEqual(profile.permission_preset, PermissionPreset.PLAN)
        self.assertEqual(profile.session_mode, "sticky")
        self.assertEqual(profile.to_dict()["permission_preset"], "plan")

    def test_base_harness_error_classification(self):
        h = _SampleHarness()
        self.assertEqual(h.classify_error(1, "HTTP 429 RESOURCE_EXHAUSTED"), ErrorCategory.RATE_LIMIT)
        self.assertEqual(h.classify_error(1, "Error: Not logged in (401 Unauthorized)"), ErrorCategory.AUTH_ERROR)
        self.assertEqual(h.classify_error(1, "Request timed out after 30s"), ErrorCategory.TIMEOUT)
        self.assertEqual(h.classify_error(127, ""), ErrorCategory.BINARY_NOT_FOUND)
        self.assertEqual(h.classify_error(1, "Segmentation fault"), ErrorCategory.PROCESS_CRASH)
        self.assertEqual(h.classify_error(0, "", ""), ErrorCategory.NONE)


if __name__ == "__main__":
    unittest.main()
