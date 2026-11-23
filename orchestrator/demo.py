"""Zero-config simulated demo workspace seeder for `python todox_main.py --demo`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .briefing import save_cached_briefing
from .guardrails import append_trace_span
from .models import ExecutionMeta, HarnessConfig
from .storage import (
    add_comment,
    create_project,
    create_task,
    get_default_worker_prompt,
    load_task,
    save_task,
    update_project,
    update_task,
)


_AUTH_ARCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AUTH Architecture Diagram</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }
    .card { border: 1px solid #334155; border-radius: 12px; padding: 16px; background: #1e293b; max-width: 760px; }
    .flow { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .step { padding: 10px 14px; border-radius: 8px; background: #0f172a; border: 1px solid #38bdf8; font-size: 13px; }
  </style>
</head>
<body>
  <div class="card">
    <h2 style="margin-top:0;">OAuth2 PKCE + Rotating Refresh Token Flow</h2>
    <p>Implemented in TodoX Demo Mode. Zero external CDNs; self-contained HTML preview.</p>
    <div class="flow">
      <div class="step">1. /auth/login (PKCE S256)</div>
      <div class="step">2. /auth/callback (JWT + Rotation)</div>
      <div class="step">3. /auth/refresh (Reuse Detection)</div>
      <div class="step">4. /auth/logout (Family Revoke)</div>
    </div>
  </div>
</body>
</html>
"""

_AUTH_DESIGN_MD = """# Session Security Architecture

- **PKCE Validation**: Enforce SHA-256 code challenge verification on every authorization callback.
- **Token Family Rotation**: Single-use refresh tokens revoke the entire token family upon replay detection.
- **Guardrails Active**: Demo project budget configured cleanly at `max_steps=15`.
"""

_SEARCH_BENCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SEARCH Retrieval Benchmark</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }
    table { border-collapse: collapse; width: 100%; max-width: 680px; background: #1e293b; border-radius: 8px; overflow: hidden; }
    th, td { padding: 10px 14px; border-bottom: 1px solid #334155; text-align: left; font-size: 13px; }
    th { background: #020617; color: #38bdf8; }
  </style>
</head>
<body>
  <h2>Hybrid BM25 + Vector Retrieval Benchmark</h2>
  <table>
    <thead><tr><th>Pipeline</th><th>Recall@10</th><th>p95 Latency</th></tr></thead>
    <tbody>
      <tr><td>BM25 Baseline</td><td>0.74</td><td>11 ms</td></tr>
      <tr><td>Dense Embeddings Only</td><td>0.81</td><td>28 ms</td></tr>
      <tr><td>Hybrid RRF Fusion (Current)</td><td><strong>0.92</strong></td><td><strong>31 ms</strong></td></tr>
    </tbody>
  </table>
</body>
</html>
"""


def seed_demo_workspace(data_dir: str) -> List[str]:
    """Populate realistic showcase projects, Kanban tickets, artifacts, briefings, and telemetry traces."""
    prev_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = data_dir
    try:
        prompt = get_default_worker_prompt()

        # Project 1: AUTH — flagship active Kanban + Briefing + Preview showcase
        auth_root = create_project(
            "AUTH",
            "OAuth2 & Session Security",
            "Build hardened OAuth2 PKCE authentication, rotating refresh tokens, and rate-limited login endpoints.",
            prompt,
        )
        auth_root.harness_config = HarnessConfig(harness="mock", model="mock-agent-v1", max_steps=15, max_tokens=150000)
        save_task("AUTH", auth_root)
        update_project("AUTH", status="in_progress", assignee="AI")

        t1 = create_task("AUTH", "AUTH-0", "Implement PKCE verifier", "Generate and verify S256 code challenges.", "Admin", status="complete")
        if t1:
            t1.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=2, total_tokens=3410, total_duration_seconds=3.8)
            save_task("AUTH", t1)
            add_comment("AUTH", "AUTH-1", "AI", "PKCE S256 verifier implemented and unit-tested cleanly.")

        t2 = create_task("AUTH", "AUTH-0", "Add token rotation middleware", "Detect refresh token replay and revoke compromised families.", "Admin", status="need_confirm")
        if t2:
            t2.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=3, total_tokens=5120, total_duration_seconds=5.4)
            save_task("AUTH", t2)
            add_comment("AUTH", "AUTH-2", "AI", "Completed sliding-window token rotation middleware; ready for Admin review.")

        t3 = create_task("AUTH", "AUTH-0", "Add brute-force login limiter", "Block repeated failed logins by IP + account bucket.", "Admin", status="complete")
        if t3:
            t3.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=1, total_tokens=1890, total_duration_seconds=2.1)
            save_task("AUTH", t3)
            add_comment("AUTH", "AUTH-3", "AI", "Token bucket rate limiter integrated and verified under burst load.")

        t4 = create_task("AUTH", "AUTH-0", "Add audit log redaction hook", "Strip Authorization headers and refresh secrets from HTTP access logs.", "AI", status="in_progress")
        if t4:
            t4.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=1, total_tokens=1620, total_duration_seconds=1.9)
            save_task("AUTH", t4)

        create_task("AUTH", "AUTH-0", "Support WebAuthn passkeys", "Allow passwordless hardware security keys during sign-in.", "Admin", status="not_started")

        auth_files = Path(data_dir) / "projects" / "AUTH" / "files"
        auth_files.mkdir(parents=True, exist_ok=True)
        (auth_files / "architecture.html").write_text(_AUTH_ARCH_HTML, encoding="utf-8")
        (auth_files / "design.md").write_text(_AUTH_DESIGN_MD, encoding="utf-8")

        save_cached_briefing(
            "AUTH",
            (
                "PKCE validation (`AUTH-1`) and login rate limiting (`AUTH-3`) are complete. "
                "Refresh-token rotation middleware (`AUTH-2`) is waiting for Admin approval, while audit log "
                "redaction (`AUTH-4`) is ready for the next AI step."
            ),
            source="worker_step",
            harness="mock",
            model="mock-agent-v1",
        )

        # Project 2: SEARCH — multi-project dashboard showcase
        search_root = create_project(
            "SEARCH",
            "Hybrid Semantic Search Engine",
            "Combine BM25 lexical matching with dense vector embeddings using Reciprocal Rank Fusion.",
            prompt,
        )
        search_root.harness_config = HarnessConfig(harness="mock", model="mock-agent-v1", max_steps=12, max_tokens=120000)
        save_task("SEARCH", search_root)

        s1 = create_task("SEARCH", "SEARCH-0", "Build BM25 inverted index", "Tokenize and score repository docs cleanly.", "Admin", status="complete")
        if s1:
            s1.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=2, total_tokens=2940, total_duration_seconds=3.1)
            save_task("SEARCH", s1)
            add_comment("SEARCH", "SEARCH-1", "AI", "BM25 index built and achieving 11 ms p95 lookup latency.")

        s2 = create_task("SEARCH", "SEARCH-0", "Implement RRF rank fusion", "Fuse BM25 and cosine vector scores via Reciprocal Rank Fusion.", "Admin", status="need_confirm")
        if s2:
            s2.execution_meta = ExecutionMeta(last_harness="mock", last_model="mock-agent-v1", total_steps=2, total_tokens=4180, total_duration_seconds=4.5)
            save_task("SEARCH", s2)
            add_comment("SEARCH", "SEARCH-2", "AI", "Hybrid RRF pipeline pushed Recall@10 from 0.74 to 0.92.")

        search_files = Path(data_dir) / "projects" / "SEARCH" / "files"
        search_files.mkdir(parents=True, exist_ok=True)
        (search_files / "benchmark.html").write_text(_SEARCH_BENCH_HTML, encoding="utf-8")

        save_cached_briefing(
            "SEARCH",
            "BM25 indexer is complete and Hybrid RRF Fusion (`SEARCH-2`) raised Recall@10 to 0.92; awaiting review.",
            source="worker_step",
            harness="mock",
            model="mock-agent-v1",
        )

        # Seed realistic trace spans for the Telemetry dashboard strip
        append_trace_span("AUTH", "AUTH-1", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=2.1, total_tokens=1850, tool_count=3)
        append_trace_span("AUTH", "AUTH-2", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=2.8, total_tokens=2410, tool_count=4)
        append_trace_span("AUTH", "AUTH-3", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=2.1, total_tokens=1890, tool_count=2)
        append_trace_span("SEARCH", "SEARCH-1", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=1.9, total_tokens=1640, tool_count=3)
        append_trace_span("SEARCH", "SEARCH-2", "mock", "mock-agent-v1", "SUCCESS", duration_seconds=2.4, total_tokens=2260, tool_count=4)

        return ["AUTH", "SEARCH"]
    finally:
        if prev_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = prev_dir


if __name__ == "__main__":
    import tempfile
    from .storage import load_projects

    with tempfile.TemporaryDirectory() as tmpdir:
        pids = seed_demo_workspace(tmpdir)
        assert pids == ["AUTH", "SEARCH"]
        prev = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tmpdir
        try:
            loaded = load_projects()
            assert "AUTH" in loaded and "SEARCH" in loaded
            assert load_task("AUTH", "AUTH-2").status == "need_confirm"
        finally:
            if prev is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = prev
    print("orchestrator.demo self-test passed!")
