# TodoX

TodoX is an AI-driven project management and development orchestrator built on a flat filesystem layout, the 9-Field JSON task schema, a pluggable multi-CLI AI agent harness layer, and an asynchronous Telegram control interface.

## Features

- **Pluggable AI Agent Harness Layer (`harness/`)**:
  - Unified `BaseHarness` abstraction with normalized `HarnessEvent` streaming, canonical tool mapping (`shell`, `read`, `write`, `edit`, `search`, `web`, `agent`, `mcp`), typed `ErrorCategory` classification, and a 16 MiB pipe buffer to prevent NDJSON line truncation.
  - **`AgyHarness` (Default)**: Integrates Google Antigravity CLI (`agy`) via bidirectional `--input-format stream-json --output-format stream-json`, dynamic `agy models` discovery, reasoning effort flags, permission presets (`auto`, `accept-edits`, `plan`, `turbo`), and mid-flight steering.
  - **3-Level Routing & Fallback**: `HarnessRouter` resolves overrides in **Task > Project > Global** order and steps along a configurable fallback chain on rate-limit (`429`) errors.
- **Upgraded Async Telegram Interface (`telegram_ui/`)**:
  - Lightweight async `httpx` client (`TelegramClient`) with bot-token redaction, non-blocking `429` rate-limit protection for live edits, `409` polling backoff, and HTML-stripped plain-text fallback.
  - **Live Worker Progress Cards**: Streams expandable tool traces (`<blockquote expandable>`), elapsed duration, token usage, adaptive throttling, raw HTML/protocol masking during live edits, auto-exported mobile HTML reports, clean `/stop` delivery suppression, and one-tap **Stop** / **Live Log** / **Approve & Resume** / **Retry** buttons directly in Telegram.
  - **Interactive Controls + Reply-to-Card UX**: Inline keyboards for `/projects`, `/use`, `/summary`, `/status`, `/harness`, `/model`, `/effort`, `/permissions`, `/pause`, `/resume`, and `/stop`, plus replying directly to any Project or Task card to attach Admin comments.
  - **Voice Notes & Conversational Project Chat (`orchestrator/voice.py`, `orchestrator/briefing.py`, `orchestrator/chat_router.py`)**:
    - Two-layer **Current Project Briefing** card on `/use <PID>` or project switch (`✨ AI Summary` read-only executive synthesis + `🔊 Voice Brief` spoken TTS audio + instant `worker_step` / latest-ticket fallback).
    - Multilingual Speech-to-Text (`faster-whisper`) and Text-to-Speech (`edge-tts` / `ffmpeg`, with macOS `say` / browser speech fallback) across both Telegram voice notes and the Web UI **Voice & Chat Command Bar**.
    - Shared natural-language command router (`handle_project_chat`) to summarize status, create & run tickets, add comments & re-run, approve `need_confirm` tickets, or stop/steer running workers.
  - **Offline Mobile HTML File Viewers**: Renders Markdown, diffs, and source code into self-contained dark/light HTML reports without external CDN dependencies.
  - **Deny-by-Default Security**: Enforces `ALLOWED_CHAT_IDS` / `ADMIN_CHAT_ID` on all incoming messages and inline callbacks (`ALLOW_ALL_CHATS=false` by default).
  - **Batch VOP Compatibility**: Retains full support for multi-line VOP commands (`/new-project`, `/new-task`, `/update-task`, `/new-comment`, `/get-project`, `/get-file`, `/fix-data`).
- **Modern Offline Kanban Board + Real-Time SSE Web UI (`web/`, `orchestrator/event_bus.py`)**:
  - **Project Hub & 5-Column Kanban Board**: Responsive multi-project dashboard, progress bars, drag-and-drop columns (`Backlog`, `In Progress`, `Needs Review`, `Done`, `Failed`), one-click **Run AI** / **Approve** / **Stop** controls, and per-task **Slide-Over Inspector** with Markdown rendering and optional Harness/Model/Preset overrides.
  - **Cross-Process Live SSE Stream (`DATA_DIR/live_events.jsonl`, `web_control.jsonl`)**: Streams real-time worker tool execution (`ACTIVE`/`DONE`), output tokens, and board updates to the browser via Server-Sent Events (`/api/events/stream`), with live **Steer** and **Stop** worker controls.
  - **Integrated File Explorer & Preview + Offline Theme Toggle**: Searchable workspace tree with in-browser live HTML, offline Markdown, and syntax-highlighted code previews, plus system/light/dark theme switching (`<meta name="color-scheme">`) and zero external CDN dependencies.
- **Concurrent Async Watcher & Post-Step JSON Guardrails**:
  - `AsyncWatcher` executes independent projects concurrently with per-project locks, concurrency limits, 0.5s Web control queue polling, pre-step JSON snapshots, post-step syntax + Pydantic validation, automatic single-shot JSON repair, and snapshot rollback on persistent corruption.

## Prerequisites & Installation

This project uses the Conda environment named **`todox`** (defined in `environment.yml`) and **`agy`** (Antigravity CLI) as the default AI worker harness.

1. Create and activate the `todox` Conda environment:
   ```bash
   conda env create -f environment.yml
   conda activate todox
   ```
2. Verify that `agy` is installed and authenticated (`agy --version` and `agy models`).
3. Configure environment variables by copying `.env.template` to `.env`:
   ```env
   DATA_DIR=./data
   TG_TOKEN=your_telegram_bot_token_here
   ADMIN_CHAT_ID=your_personal_chat_id_here
   ALLOWED_CHAT_IDS=
   ALLOW_ALL_CHATS=false

   # AI Agent Harness Defaults
   DEFAULT_HARNESS=agy
   AGY_BIN_PATH=
   DEFAULT_MODEL=default
   DEFAULT_EFFORT=high
   DEFAULT_PERMISSION_PRESET=auto
   WORKER_TIMEOUT_SECONDS=300

   # Scheduler & Optional Web Panel
   STREAM_THROTTLE_SECONDS=1.5
   MAX_CONCURRENT_WORKERS=2
   WEB_ENABLED=true
   WEB_PORT=8899

   # Voice STT/TTS
   WHISPER_MODEL_SIZE=base
   TTS_VOICE_EN=en-US-JennyNeural
   TTS_VOICE_ZH=zh-CN-XiaoxiaoNeural
   ```

## Usage

1. Start the Orchestrator, Telegram UI, Async Watcher, and optional Web UI (`http://localhost:8899`):
   ```bash
   conda activate todox
   python todox_main.py
   ```
   *(Set `WEB_ENABLED=false` in `.env` if you want to run Telegram-only with zero open local HTTP ports.)*

2. Primary Telegram Commands:
   - `/help` — Show interactive command center
   - `/projects` — Browse, inspect, pause, or assign projects via inline cards
   - `/use [PID]` — Switch active project for conversational text & voice messages (shows Current Project Briefing)
   - `/summary [PID]` — Generate a fresh read-only AI executive briefing for the active or target project
   - `/status` — View live orchestrator dashboard (optional `/pin` and `/unpin` manage pinned status messages)
   - `/harness`, `/model`, `/effort`, `/permissions` — Configure active CLI harness, model, reasoning effort, and sandbox preset
   - `/pause [PID]` / `/resume [PID]` — Pause or resume watcher scheduling
   - `/stop [PID]` & `/steer [PID] <msg>` — Interrupt or guide an active worker
   - `/output [lines]` — Inspect recent worker output log lines
   - `/fix_data` — Validate and repair malformed project JSON files

## Module Self-Tests & Verification

Every module can be verified independently from the command line without starting the live Telegram bot or spending LLM tokens:

```bash
# Run isolated unit & integration tests
conda run -n todox python -m unittest tests/test_harness_types.py tests/test_agy_harness.py tests/test_harness_router_and_state.py tests/test_worker_and_validation.py tests/test_telegram_client_and_formatter.py tests/test_telegram_bot_and_notifier.py tests/test_async_watcher.py tests/test_web_kanban_and_sse.py tests/test_voice_briefing_and_chat.py -v

# Run standalone module checks
conda run -n todox python -m harness.types
conda run -n todox python -m harness.base
conda run -n todox python -m harness.agy_harness
conda run -n todox python -m harness.registry
conda run -n todox python -m harness.router
conda run -n todox python -m telegram_ui.client
conda run -n todox python -m telegram_ui.formatter
conda run -n todox python -m telegram_ui.file_renderers
conda run -n todox python -m telegram_ui.keyboards
conda run -n todox python -m telegram_ui.notifier
conda run -n todox python -m telegram_ui.bot
conda run -n todox python -m orchestrator.state
conda run -n todox python -m orchestrator.storage
conda run -n todox python -m orchestrator.event_bus
conda run -n todox python -m orchestrator.voice
conda run -n todox python -m orchestrator.briefing
conda run -n todox python -m orchestrator.chat_router
conda run -n todox python -m orchestrator.worker
conda run -n todox python -m orchestrator.watcher
conda run -n todox python -m web.models
conda run -n todox python -m web.api
conda run -n todox python -m web.views
conda run -n todox python -m web.app
conda run -n todox python orchestrator/test_bot_fix.py
```

## Maintenance

- **Automated Restart**: Use `/restart` in Telegram or run `./restart.sh` to pull latest changes, update dependencies, and restart `todox_main.py`.
- **Logs**: Worker logs are written to `DATA_DIR/latest_worker_output.log`.
