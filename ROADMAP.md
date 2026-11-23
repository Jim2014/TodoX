# TodoX Engineering Roadmap

TodoX is evolving from a flat-file AI script runner into a resilient, observable, multi-CLI project orchestrator and control plane.

## Completed Milestones

- **v1 — Flat 9-Field JSON Engine & Autonomous Step Loop:** Atomic storage (`orchestrator/storage.py`), Pydantic task validation (`orchestrator/models.py`), and JSON self-repair workflows.
- **v2 — Pluggable Multi-CLI Agent Harness Layer (`harness/`):**
  - Unified `BaseHarness` contract with normalized `HarnessEvent` streams, canonical tools, typed `ErrorCategory`, and 16 MiB pipe buffering.
  - Production adapters for **Google Antigravity CLI (`AgyHarness`)** and **Anthropic Claude Code CLI (`ClaudeCodeHarness`)**, plus builtin **`MockHarness`** (`--demo` zero-config simulator).
  - 3-level **Task > Project > Global** `HarnessRouter` resolution with automated `429` rate-limit fallback chains.
- **v3 — Upgraded Async Telegram & Voice Cockpit (`telegram_ui/`):**
  - Non-blocking `429`/`409` delivery resilience, HTML repair/masking, standalone mobile HTML file viewers, deny-by-default authorization, `/use <PID>` persistent project context, `/summary` executive briefs, `faster-whisper` STT + `edge-tts`/`ffmpeg` OGG voice round-trips, and clean modular mixin decomposition (`commands.py`, `callbacks.py`, `voice_handlers.py`, `vop.py`).
- **v4 — Offline Kanban + Live SSE Web Panel (`web/`):**
  - 5-column drag-and-drop Kanban board, cross-process `live_events.jsonl` SSE streaming, mid-flight steer/stop control bus (`web_control.jsonl`), Slide-Over Task Inspector, offline file preview, and light/dark theming with zero external CDNs.
- **v5 — Production AI Infra Reliability & Evaluations:**
  - Pre-step JSON snapshots with auto-repair & rollback (`orchestrator/worker.py`).
  - Two-layer project briefings (`orchestrator/briefing.py`) & shared multilingual chat router (`orchestrator/chat_router.py`).
  - **Loop & Budget Guardrails + Structured Spans (`orchestrator/guardrails.py`):** Per-task/project `max_steps`, `max_tokens`, and `max_consecutive_failures` circuit-breakers, auto-rotated `traces.jsonl` spans, and Web telemetry strip.
  - **Offline Intent Evals (`evals/chat_router_eval.py`):** 30-case multilingual evaluation benchmark enforcing >=95% intent precision/recall in CI.

## Next Milestones (Planned)

- **v6 — OpenAI Codex CLI & Gemini CLI Harness Adapters:** Add first-class `CodexHarness` and headless `GeminiHarness` implementations to expand cross-provider failover options.
- **v7 — Subtask Dependency DAGs & Parallel Git Worktrees:** Add `depends_on` edges between sibling tickets and allow concurrent `AsyncWatcher` execution across isolated `git worktree` checkouts per subtask.
- **v8 — OpenTelemetry OTLP Trace Exporter & Cost Budgets:** Optional OTLP HTTP export alongside `traces.jsonl` plus per-model USD cost estimation and daily quota alerts.
- **v9 — Optional Web Auth & Webhook Triggers:** Built-in bearer/session auth toggle for untrusted network hosting and GitHub webhook listeners to convert PR comments into tracked TodoX tickets automatically.
