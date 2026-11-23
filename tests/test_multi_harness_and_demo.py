"""Isolated tests for ClaudeCodeHarness, MockHarness, cross-CLI fallback routing, and --demo seeding."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.claude_harness import ClaudeCodeHarness
from harness.mock_harness import MockHarness
from harness.registry import HarnessRegistry, create_default_registry
from harness.router import HarnessRouter
from harness.types import (
    CanonicalTool,
    ErrorCategory,
    HarnessEvent,
    PermissionPreset,
    RunRequest,
)
from orchestrator.demo import seed_demo_workspace
from orchestrator.models import ExecutionMeta, Task
from orchestrator.state import SystemState
from orchestrator.storage import load_projects, load_task


def _write_mock_claude_script(directory: str) -> str:
    """Create a self-contained mock `claude` CLI executable in a temp directory."""
    script_path = os.path.join(directory, "mock_claude.py")
    content = """#!/usr/bin/env python3
import json
import os
import sys

if "--version" in sys.argv:
    print("2.1.0 (Mock Claude Code CLI)")
    sys.exit(0)

mode = os.environ.get("MOCK_CLAUDE_MODE", "success")
if mode == "rate_limit":
    sys.stderr.write("Error: HTTP 429 rate_limit_error too many requests\\n")
    sys.stderr.flush()
    sys.exit(1)

if mode == "auth_error":
    print(json.dumps({
        "type": "result",
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "401 Unauthorized: invalid api key",
    }), flush=True)
    sys.exit(1)

print(json.dumps({"type": "system", "subtype": "init", "session_id": "claude-sess-42"}), flush=True)
print(json.dumps({
    "type": "assistant",
    "message": {
        "content": [
            {"type": "tool_use", "id": "tu1", "name": "Edit", "input": {"file_path": "auth.py"}},
            {"type": "text", "text": "Updated auth.py and ran tests."},
        ]
    },
}), flush=True)
print(json.dumps({
    "type": "user",
    "message": {
        "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "tool_name": "Edit", "is_error": False}
        ]
    },
}), flush=True)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "claude-sess-42",
    "duration_ms": 1450,
    "result": "Implemented requested fix with tests passing.",
    "usage": {"input_tokens": 900, "output_tokens": 250, "cache_read_input_tokens": 120},
}), flush=True)
"""
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
    st = os.stat(script_path)
    os.chmod(script_path, st.st_mode | stat.S_IEXEC)
    return script_path


class TestMultiHarnessAndDemo(unittest.IsolatedAsyncioTestCase):
    def test_claude_command_flags_and_registry(self):
        harness = ClaudeCodeHarness(claude_bin="/usr/local/bin/claude")
        cmd_auto = harness.build_command(
            RunRequest(prompt="fix bug", working_dir=".", model="claude-sonnet-4-6", permission_preset=PermissionPreset.AUTO)
        )
        self.assertIn("--output-format", cmd_auto)
        self.assertIn("stream-json", cmd_auto)
        self.assertIn("default", cmd_auto)
        self.assertIn("claude-sonnet-4-6", cmd_auto)

        cmd_turbo = harness.build_command(
            RunRequest(prompt="ship", working_dir=".", permission_preset=PermissionPreset.TURBO, session_id="s-9")
        )
        self.assertIn("--dangerously-skip-permissions", cmd_turbo)
        self.assertIn("--resume", cmd_turbo)
        self.assertIn("s-9", cmd_turbo)

        reg = create_default_registry(include_mock=True)
        self.assertEqual(reg.list_names(), ["agy", "claude", "mock"])

    async def test_claude_stream_events_and_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_bin = _write_mock_claude_script(tmpdir)
            harness = ClaudeCodeHarness(claude_bin=mock_bin)

            health = await harness.check_health()
            self.assertTrue(health.available)
            self.assertIn("2.1.0", health.version or "")

            events = [e async for e in harness.run_stream(RunRequest(prompt="check auth", working_dir=tmpdir))]
            self.assertEqual(events[0].type, "init")
            self.assertEqual(events[0].session_id, "claude-sess-42")
            tools = [e for e in events if e.type == "tool"]
            self.assertGreaterEqual(len(tools), 2)
            self.assertEqual(tools[0].canonical_tool, CanonicalTool.EDIT)
            self.assertEqual(events[-1].type, "result")
            self.assertEqual(events[-1].status, "SUCCESS")
            self.assertEqual(events[-1].usage.input_tokens, 900)
            self.assertEqual(events[-1].usage.output_tokens, 250)
            self.assertEqual(events[-1].usage.cached_tokens, 120)

            # Error classifications
            err_429 = [
                e
                async for e in harness.run_stream(
                    RunRequest(prompt="x", working_dir=tmpdir, env_overrides={"MOCK_CLAUDE_MODE": "rate_limit"})
                )
            ]
            self.assertEqual(err_429[-1].error_category, ErrorCategory.RATE_LIMIT)

            err_401 = [
                e
                async for e in harness.run_stream(
                    RunRequest(prompt="x", working_dir=tmpdir, env_overrides={"MOCK_CLAUDE_MODE": "auth_error"})
                )
            ]
            self.assertEqual(err_401[-1].error_category, ErrorCategory.AUTH_ERROR)

            missing = ClaudeCodeHarness(claude_bin=os.path.join(tmpdir, "no_such_claude"))
            missing_evs = [e async for e in missing.run_stream(RunRequest(prompt="x", working_dir=tmpdir))]
            self.assertEqual(missing_evs[-1].error_category, ErrorCategory.BINARY_NOT_FOUND)

    async def test_cross_harness_fallback_routing_and_mock_harness(self):
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_claude = _write_mock_claude_script(tmpdir)
            sf = os.path.join(tmpdir, "system_state.json")
            with open(sf, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "default_harness": "agy",
                        "default_model": "gemini-3.8-flash",
                        "model_index": 0,
                        "fallback_chain": [
                            {"harness": "agy", "model": "gemini-3.8-flash"},
                            {"harness": "claude", "model": "claude-sonnet-4-6"},
                        ],
                    },
                    f,
                )
            st = SystemState(state_file=sf)

            class _RateLimitedAgy:
                async def run_stream(self, request):
                    yield HarnessEvent(
                        type="result",
                        harness="agy",
                        status="ERROR",
                        error_category=ErrorCategory.RATE_LIMIT,
                        response="HTTP 429 rate_limit",
                    )

                async def stop(self):
                    return None

            reg = HarnessRegistry()
            reg.register("agy", lambda **kw: _RateLimitedAgy())  # type: ignore[arg-type]
            reg.register("claude", lambda **kw: ClaudeCodeHarness(claude_bin=mock_claude))
            router = HarnessRouter(registry=reg, state_manager=st)

            # Primary fails with 429; state steps along fallback chain onto ClaudeCodeHarness cleanly
            r1 = await router.run_step(RunRequest(prompt="ship", working_dir=tmpdir), profile=router.resolve_profile())
            self.assertEqual(r1.error_category, ErrorCategory.RATE_LIMIT)
            st.handle_429()
            r2 = await router.run_step(RunRequest(prompt="ship", working_dir=tmpdir), profile=router.resolve_profile())
            self.assertTrue(r2.success)
            self.assertEqual(r2.harness, "claude")

            # MockHarness fast turn mutates active task into need_confirm
            mh = MockHarness(step_delay=0.01)
            prev_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = tmpdir
            try:
                pids = seed_demo_workspace(tmpdir)
                self.assertEqual(pids, ["AUTH", "SEARCH"])
                projects = load_projects()
                self.assertEqual(set(projects.keys()), {"AUTH", "SEARCH"})
                auth_proj_dir = os.path.join(tmpdir, "projects", "AUTH")
                evs = [e async for e in mh.run_stream(RunRequest(prompt="run auth step", working_dir=auth_proj_dir))]
                self.assertEqual(evs[-1].status, "SUCCESS")
                self.assertEqual(load_task("AUTH", "AUTH-4").status, "need_confirm")
            finally:
                if prev_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prev_dir


if __name__ == "__main__":
    unittest.main()
