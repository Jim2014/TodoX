"""30-case multilingual evaluation benchmark for orchestrator.chat_router intent classification."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import orchestrator.chat_router as chat_router_mod
from orchestrator.chat_router import handle_project_chat
from orchestrator.event_bus import clear_stale_worker_runtime
from orchestrator.state import SystemState
from orchestrator.storage import create_project, create_task, get_default_worker_prompt


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    message: str
    expected_action: str
    worker_running: bool = False
    expected_tid: Optional[str] = None


EVAL_CASES: List[EvalCase] = [
    # 1. Briefing / Status queries
    EvalCase("brf_01", "status", "briefing"),
    EvalCase("brf_02", "What is the current status?", "briefing"),
    EvalCase("brf_03", "What changed?", "briefing"),
    EvalCase("brf_04", "\u603b\u7ed3\u4e00\u4e0b\u5f53\u524d\u9879\u76ee\u8fdb\u5ea6", "briefing"),
    EvalCase("brf_05", "\u73b0\u5728\u4ec0\u4e48\u60c5\u51b5\uff1f", "briefing"),
    # 2. Explicit AI summary refresh
    EvalCase("ais_01", "ai summary", "ai_summary"),
    EvalCase("ais_02", "Refresh AI summary", "ai_summary"),
    EvalCase("ais_03", "\u751f\u6210ai\u603b\u7ed3", "ai_summary"),
    # 3. Task creation (idle and concurrent running worker)
    EvalCase("crt_01", "Create a task to add rate limiting middleware", "create_and_run"),
    EvalCase("crt_02", "Add a ticket for WebAuthn passkeys", "create_and_run"),
    EvalCase("crt_03", "Create task: implement CSV exporter", "create_and_run", worker_running=True),
    EvalCase("crt_04", "Implement dark theme toggle and keyboard navigation", "create_and_run"),
    EvalCase("crt_05", "\u65b0\u5efa\u4efb\u52a1\uff1a\u6dfb\u52a0\u5bfc\u51fa\u6309\u94ae", "create_and_run"),
    # 4. Comment & re-run existing tickets
    EvalCase("cmt_01", "On EVL-2 please also cover invalid token expiry", "comment_and_run", expected_tid="EVL-2"),
    EvalCase("cmt_02", "Update EVL-1 with edge case unit assertions", "comment_and_run", expected_tid="EVL-1"),
    EvalCase("cmt_03", "EVL-2 needs stricter input sanitization", "comment_and_run", expected_tid="EVL-2"),
    # 5. Approve need_confirm tickets
    EvalCase("app_01", "approve EVL-1", "approve", expected_tid="EVL-1"),
    EvalCase("app_02", "confirm EVL-2", "approve", expected_tid="EVL-2"),
    EvalCase("app_03", "approve pending", "approve", expected_tid="EVL-1"),
    EvalCase("app_04", "\u6279\u51c6 EVL-1", "approve", expected_tid="EVL-1"),
    # 6. Run / retry specific ticket
    EvalCase("run_01", "run EVL-2", "run_task", expected_tid="EVL-2"),
    EvalCase("run_02", "retry EVL-2", "run_task", expected_tid="EVL-2"),
    # 7. Worker steering (explicit prefix or running-worker context)
    EvalCase("str_01", "steer focus on regression tests first", "steer", worker_running=True),
    EvalCase("str_02", "Please run pytest with verbose output next", "steer", worker_running=True),
    EvalCase("str_03", "\u7ea0\u504f \u5148\u4fee\u590d\u8fb9\u754c\u6d4b\u8bd5", "steer", worker_running=True),
    # 8. Stop, pause, and resume controls
    EvalCase("stp_01", "stop worker", "stop", worker_running=True),
    EvalCase("stp_02", "cancel worker", "stop", worker_running=True),
    EvalCase("pau_01", "pause project", "pause"),
    EvalCase("pau_02", "\u6682\u505c\u9879\u76ee", "pause"),
    EvalCase("res_01", "resume work", "resume"),
]


async def run_chat_router_eval(use_live_llm: bool = False) -> Dict[str, Any]:
    """Execute the 30-case intent routing benchmark and return accuracy metrics."""
    passed = 0
    per_intent: Dict[str, Dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "total": 0})
    failures: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        prev_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            state = SystemState(state_file=os.path.join(tmpdir, "system_state.json"))
            for idx, case in enumerate(EVAL_CASES):
                pid = f"EVL"
                proj_dir = os.path.join(tmpdir, "projects", pid)
                if idx == 0:
                    create_project(pid, "Eval Showcase", "Benchmark intent router", get_default_worker_prompt())
                    create_task(pid, "EVL-0", "Pending Review", "Check middleware", "Admin", status="need_confirm")
                    create_task(pid, "EVL-0", "Existing Ticket", "Core endpoint", "Admin", status="not_started")
                elif case.case_id == "app_03":
                    from orchestrator.storage import update_task
                    update_task(pid, "EVL-1", status="need_confirm", assignee="Admin")
                elif not os.path.isdir(proj_dir):
                    create_project(pid, "Eval Showcase", "Benchmark intent router", get_default_worker_prompt())

                clear_stale_worker_runtime(set())
                watcher = AsyncMock()
                watcher.is_project_running = MagicMock(return_value=case.worker_running)
                watcher.trigger_project = AsyncMock(return_value=True)
                watcher.stop_project = AsyncMock(return_value=True)
                watcher.steer_project = AsyncMock(return_value=(True, "steered"))

                orig_llm = chat_router_mod._default_llm_classify
                orig_ai_brief = chat_router_mod.generate_ai_project_briefing
                if not use_live_llm:
                    chat_router_mod._default_llm_classify = AsyncMock(return_value=None)  # type: ignore[assignment]
                    chat_router_mod.generate_ai_project_briefing = AsyncMock(  # type: ignore[assignment]
                        return_value={
                            "pid": pid,
                            "summary_text": "Stub AI summary.",
                            "spoken_text": "Stub AI summary.",
                            "summary_source": "ai_refresh",
                        }
                    )
                try:
                    res = await handle_project_chat(
                        pid=pid,
                        message=case.message,
                        source="eval",
                        auto_run=True,
                        watcher=watcher,
                        state_manager=state,
                    )
                finally:
                    chat_router_mod._default_llm_classify = orig_llm  # type: ignore[assignment]
                    chat_router_mod.generate_ai_project_briefing = orig_ai_brief  # type: ignore[assignment]

                actual = str(res.get("action") or "")
                actual_tid = str(res.get("tid") or "")
                tid_ok = True if case.expected_tid is None else (actual_tid == case.expected_tid)
                ok = bool(res.get("ok")) and (actual == case.expected_action) and tid_ok

                per_intent[case.expected_action]["total"] += 1
                if ok:
                    passed += 1
                    per_intent[case.expected_action]["tp"] += 1
                else:
                    per_intent[case.expected_action]["fn"] += 1
                    per_intent[actual or "unknown"]["fp"] += 1
                    failures.append(
                        {
                            "case_id": case.case_id,
                            "message": case.message,
                            "expected_action": case.expected_action,
                            "actual_action": actual,
                            "expected_tid": case.expected_tid,
                            "actual_tid": actual_tid,
                        }
                    )
        finally:
            if prev_dir is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev_dir

    total = len(EVAL_CASES)
    accuracy = round((passed / total) * 100.0, 2) if total else 0.0
    intent_rows: Dict[str, Dict[str, Any]] = {}
    for intent, counts in sorted(per_intent.items()):
        if counts["total"] == 0 and counts["fp"] == 0:
            continue
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        prec = round((tp / (tp + fp)) * 100.0, 1) if (tp + fp) else 0.0
        rec = round((tp / (tp + fn)) * 100.0, 1) if (tp + fn) else 0.0
        intent_rows[intent] = {
            "total": counts["total"],
            "passed": tp,
            "precision_pct": prec,
            "recall_pct": rec,
        }

    return {
        "total_cases": total,
        "passed": passed,
        "accuracy_pct": accuracy,
        "per_intent": intent_rows,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TodoX conversational router intent benchmark")
    parser.add_argument("--live", action="store_true", help="Include live HarnessRouter LLM calls")
    parser.add_argument("--json", action="store_true", help="Print JSON summary report")
    parser.add_argument("--min-accuracy", type=float, default=95.0, help="Minimum passing accuracy percentage")
    args = parser.parse_args()

    report = asyncio.run(run_chat_router_eval(use_live_llm=args.live))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"TodoX Chat Router Intent Eval: {report['passed']}/{report['total_cases']} ({report['accuracy_pct']}%)")
        print(f"{'Intent':<18} {'Passed':<10} {'Precision':<12} {'Recall':<10}")
        print("-" * 52)
        for intent, stats in report["per_intent"].items():
            ratio = f"{stats['passed']}/{stats['total']}"
            print(f"{intent:<18} {ratio:<10} {stats['precision_pct']:>6.1f}%     {stats['recall_pct']:>6.1f}%")
        if report["failures"]:
            print("\nFailures:")
            for f in report["failures"]:
                print(f" - {f['case_id']}: expected={f['expected_action']}, got={f['actual_action']}")

    if report["accuracy_pct"] < args.min_accuracy:
        sys.exit(1)


if __name__ == "__main__":
    main()
