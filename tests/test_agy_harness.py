import asyncio
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.agy_harness import (
    AgyHarness,
    model_supports_effort,
    parse_agy_models,
)
from harness.types import CanonicalTool, ErrorCategory, PermissionPreset, RunRequest


def _write_mock_agy_script(directory: str) -> str:
    """Create a standalone executable Python script simulating agy stream-json protocol."""
    script_path = os.path.join(directory, "mock_agy.py")
    content = textwrap.dedent(
        f"""#!{sys.executable}
import json
import os
import sys
import time

mode = os.environ.get("MOCK_AGY_MODE", "success")

if "--version" in sys.argv:
    print("agy 1.1.15-mock")
    sys.exit(0)

if "models" in sys.argv:
    print("gemini-3.8-flash-high\\tGemini 3.8 Flash (High)")
    print("gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)")
    print("gemini-3.1-pro\\tGemini 3.1 Pro")
    sys.exit(0)

if mode == "rate_limit_stderr":
    print("429 Too Many Requests: quota exceeded", file=sys.stderr, flush=True)
    sys.exit(1)

if mode == "auth_error":
    print("401 Unauthorized: invalid API key", file=sys.stderr, flush=True)
    sys.exit(1)

if mode == "sleep":
    time.sleep(5)
    sys.exit(0)

turn = 0
for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    msg = json.loads(line)
    user_text = msg.get("message", {{}}).get("content", "")
    turn += 1

    if mode == "large_line":
        big_text = "A" * 120000
        print(json.dumps({{"event": "init", "conversation_id": "conv-big"}}), flush=True)
        print(json.dumps({{
            "event": "result",
            "result": {{
                "conversation_id": "conv-big",
                "status": "SUCCESS",
                "response": big_text,
                "duration_seconds": 0.2,
                "usage": {{"total_tokens": 500}}
            }}
        }}), flush=True)
        continue

    if mode == "steer_two_turns":
        print(json.dumps({{"event": "init", "conversation_id": "conv-steer"}}), flush=True)
        print(json.dumps({{
            "event": "step_update",
            "step_update": {{
                "step_type": "tool",
                "conversation_id": "conv-steer",
                "tool_name": "run_command",
                "state": "ACTIVE",
                "tool_info": {{"parameters": {{"CommandLine": "pytest"}}}}
            }}
        }}), flush=True)
        time.sleep(0.15)
        print(json.dumps({{
            "event": "result",
            "result": {{
                "conversation_id": "conv-steer",
                "status": "SUCCESS",
                "response": f"turn-{{turn}}: {{user_text}}",
                "duration_seconds": 0.1,
                "usage": {{"total_tokens": 100}}
            }}
        }}), flush=True)
        if turn >= 2:
            break
        continue

    if mode == "result_rate_limit":
        print(json.dumps({{"event": "init", "conversation_id": "conv-429"}}), flush=True)
        print(json.dumps({{
            "event": "result",
            "result": {{
                "conversation_id": "conv-429",
                "status": "ERROR",
                "error": "HTTP 429 quota exceeded",
                "duration_seconds": 0.1
            }}
        }}), flush=True)
        break

    # Default success / warm reuse
    print(json.dumps({{"event": "init", "conversation_id": "conv-ok"}}), flush=True)
    print(json.dumps({{
        "event": "step_update",
        "step_update": {{
            "step_type": "tool",
            "conversation_id": "conv-ok",
            "tool_name": "read_file",
            "state": "DONE",
            "duration_seconds": 0.05,
            "tool_info": {{"parameters": {{"Path": "/tmp/project.json"}}}}
        }}
    }}), flush=True)
    print(json.dumps({{
        "event": "step_update",
        "step_update": {{
            "step_type": "agent_response",
            "conversation_id": "conv-ok",
            "text_delta": "Hello "
        }}
    }}), flush=True)
    print(json.dumps({{
        "event": "result",
        "result": {{
            "conversation_id": "conv-ok",
            "status": "SUCCESS",
            "response": f"pid={{os.getpid()}} turn={{turn}} prompt={{user_text}}",
            "duration_seconds": 0.25,
            "usage": {{"input_tokens": 80, "output_tokens": 20, "thinking_tokens": 10, "total_tokens": 110}}
        }}
    }}), flush=True)
"""
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
    return script_path


class TestAgyHarness(unittest.IsolatedAsyncioTestCase):
    def test_effort_and_model_parsing(self):
        self.assertTrue(model_supports_effort("gemini-3.8-flash"))
        self.assertTrue(model_supports_effort("default"))
        self.assertFalse(model_supports_effort("gemini-3.8-flash-high"))
        self.assertFalse(model_supports_effort("gemini-pro-orchestrator-neon"))

        raw = (
            "Fetching models...\n"
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
            "gemini-3.1-pro\tGemini 3.1 Pro\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        )
        models = parse_agy_models(raw)
        self.assertEqual([m.id for m in models], ["gemini-3.8-flash", "gemini-3.1-pro", "claude-sonnet-4-6"])

    def test_build_command_flags(self):
        harness = AgyHarness(agy_bin="/usr/local/bin/agy")
        cmd_auto = harness.build_command(
            RunRequest(
                prompt="hi",
                working_dir="/tmp/proj",
                model="gemini-3.8-flash",
                effort="high",
                permission_preset=PermissionPreset.AUTO,
                extra_dirs=["/tmp/shared"],
            )
        )
        self.assertIn("--mode", cmd_auto)
        self.assertIn("auto", cmd_auto)
        self.assertIn("--new-project", cmd_auto)
        self.assertIn("--model", cmd_auto)
        self.assertIn("gemini-3.8-flash", cmd_auto)
        self.assertIn("--effort", cmd_auto)
        self.assertIn("high", cmd_auto)
        self.assertIn("/tmp/shared", cmd_auto)

        cmd_turbo = harness.build_command(
            RunRequest(
                prompt="hi",
                working_dir="/tmp/proj",
                model="default",
                effort="high",
                permission_preset=PermissionPreset.TURBO,
                session_id="conv-123",
            )
        )
        self.assertIn("--dangerously-skip-permissions", cmd_turbo)
        self.assertNotIn("--mode", cmd_turbo)
        self.assertIn("--conversation", cmd_turbo)
        self.assertIn("conv-123", cmd_turbo)
        self.assertNotIn("--model", cmd_turbo)

    async def test_stream_and_large_ndjson_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_bin = _write_mock_agy_script(tmpdir)
            harness = AgyHarness(agy_bin=mock_bin, keep_alive=False)

            health = await harness.check_health()
            self.assertTrue(health.available)
            self.assertIn("1.1.15-mock", health.version or "")

            models = await harness.list_models(force_refresh=True)
            self.assertEqual([m.id for m in models], ["gemini-3.8-flash", "gemini-3.1-pro"])

            # Verify >100 KiB single NDJSON line succeeds with 16 MiB StreamReader limit
            events = []
            req = RunRequest(
                prompt="huge",
                working_dir=tmpdir,
                env_overrides={"MOCK_AGY_MODE": "large_line"},
            )
            async for ev in harness.run_stream(req):
                events.append(ev)

            results = [e for e in events if e.type == "result"]
            self.assertEqual(len(results), 1)
            self.assertEqual(len(results[0].response), 120000)

    async def test_streaming_tool_normalization_and_warm_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_bin = _write_mock_agy_script(tmpdir)
            harness = AgyHarness(agy_bin=mock_bin, keep_alive=True)

            try:
                req1 = RunRequest(prompt="step 1", working_dir=tmpdir, system_prompt="SYS")
                ev1 = [e async for e in harness.run_stream(req1)]
                tools = [e for e in ev1 if e.type == "tool"]
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0].canonical_tool, CanonicalTool.READ)
                res1 = [e for e in ev1 if e.type == "result"][0]
                self.assertEqual(res1.usage.total_tokens, 110)

                # Reuse warm process for turn 2 with matching conversation_id
                req2 = RunRequest(
                    prompt="step 2",
                    working_dir=tmpdir,
                    system_prompt="SYS",
                    followup_system_prompt="SHORT_REMINDER",
                    session_id="conv-ok",
                )
                ev2 = [e async for e in harness.run_stream(req2)]
                res2 = [e for e in ev2 if e.type == "result"][0]
                self.assertIn("turn=2", res2.response)
                self.assertIn("SHORT_REMINDER", res2.response)
            finally:
                await harness.stop()

    async def test_mid_flight_steer_and_turn_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_bin = _write_mock_agy_script(tmpdir)
            harness = AgyHarness(agy_bin=mock_bin, keep_alive=False)

            req = RunRequest(
                prompt="initial",
                working_dir=tmpdir,
                env_overrides={"MOCK_AGY_MODE": "steer_two_turns"},
            )
            seen = []
            steered = False
            async for ev in harness.run_stream(req):
                seen.append(ev)
                if ev.type == "tool" and not steered:
                    steered = await harness.steer("focus on tests")
                    self.assertTrue(steered)

            self.assertTrue(any(e.type == "turn_complete" for e in seen))
            final_results = [e for e in seen if e.type == "result"]
            self.assertEqual(len(final_results), 1)
            self.assertIn("turn-2: focus on tests", final_results[0].response)

    async def test_error_classifications_timeout_and_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_bin = _write_mock_agy_script(tmpdir)
            harness = AgyHarness(agy_bin=mock_bin, keep_alive=False)

            # 429 via stderr
            ev_429 = [
                e
                async for e in harness.run_stream(
                    RunRequest(prompt="x", working_dir=tmpdir, env_overrides={"MOCK_AGY_MODE": "rate_limit_stderr"})
                )
            ]
            self.assertEqual(ev_429[-1].error_category, ErrorCategory.RATE_LIMIT)

            # 429 via result payload
            ev_res_429 = [
                e
                async for e in harness.run_stream(
                    RunRequest(prompt="x", working_dir=tmpdir, env_overrides={"MOCK_AGY_MODE": "result_rate_limit"})
                )
            ]
            self.assertEqual(ev_res_429[-1].error_category, ErrorCategory.RATE_LIMIT)

            # 401 via stderr
            ev_401 = [
                e
                async for e in harness.run_stream(
                    RunRequest(prompt="x", working_dir=tmpdir, env_overrides={"MOCK_AGY_MODE": "auth_error"})
                )
            ]
            self.assertEqual(ev_401[-1].error_category, ErrorCategory.AUTH_ERROR)

            # Timeout
            ev_to = [
                e
                async for e in harness.run_stream(
                    RunRequest(
                        prompt="x",
                        working_dir=tmpdir,
                        timeout_seconds=0.2,
                        env_overrides={"MOCK_AGY_MODE": "sleep"},
                    )
                )
            ]
            self.assertEqual(ev_to[-1].error_category, ErrorCategory.TIMEOUT)

            # Missing binary
            missing_harness = AgyHarness(agy_bin=os.path.join(tmpdir, "does_not_exist_agy"))
            health = await missing_harness.check_health()
            self.assertFalse(health.available)
            ev_missing = [e async for e in missing_harness.run_stream(RunRequest(prompt="x", working_dir=tmpdir))]
            self.assertEqual(ev_missing[-1].error_category, ErrorCategory.BINARY_NOT_FOUND)

    async def test_stream_reader_value_error_recovery_and_bookkeeping_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            harness = AgyHarness(agy_bin="/usr/bin/agy", keep_alive=False)

            # 1. Verify internal user_input and system_message step updates are ignored
            self.assertIsNone(
                harness._parse_line(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {"step_type": "user_input", "state": "DONE"},
                        }
                    )
                )
            )
            self.assertIsNone(
                harness._parse_line(
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {"step_type": "system_message", "state": "DONE"},
                        }
                    )
                )
            )

            # 2. If stdout reader encounters ValueError, run_stream emits error & terminates proc cleanly
            class BrokenStdout:
                async def readline(self):
                    raise ValueError("Separator is found, but chunk is longer than limit")

            class DummyStdin:
                def write(self, data):
                    pass

                async def drain(self):
                    pass

                def is_closing(self):
                    return False

                def close(self):
                    pass

            class DummyStderr:
                async def read(self):
                    return b""

            class BrokenProc:
                def __init__(self):
                    self.pid = 999
                    self.returncode = None
                    self.stdin = DummyStdin()
                    self.stdout = BrokenStdout()
                    self.stderr = DummyStderr()
                    self.terminated = False

                def terminate(self):
                    self.terminated = True
                    self.returncode = -15

                def kill(self):
                    self.returncode = -9

                async def wait(self):
                    return self.returncode

            broken_proc = BrokenProc()

            async def _mock_spawn(**kwargs):
                return broken_proc

            harness._spawn_subprocess = _mock_spawn  # type: ignore[method-assign]
            events = [e async for e in harness.run_stream(RunRequest(prompt="broken", working_dir=tmpdir))]
            self.assertTrue(len(events) >= 1)
            self.assertEqual(events[-1].type, "error")
            self.assertIn("ValueError", events[-1].response)
            self.assertTrue(broken_proc.terminated)

    async def test_real_agy_check_health_non_billable(self):
        real_harness = AgyHarness()
        if not os.path.isfile(real_harness.agy_bin):
            self.skipTest("agy binary not installed in this environment")
        health = await real_harness.check_health()
        self.assertTrue(health.available)


if __name__ == "__main__":
    unittest.main()
