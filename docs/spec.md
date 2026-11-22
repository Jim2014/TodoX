# TodoX Architecture & Specification (v2)

## 1. System Overview & Goals

TodoX is an AI-driven project management and autonomous development orchestrator built on a flat filesystem layout, the 9-Field JSON state protocol, a pluggable AI CLI harness layer, and an asynchronous Telegram command-and-control interface.

## 2. Core Design Philosophy

- **Narrative History**: Every output artifact summary, AI progress update, error log, and Admin instruction is persisted inside the `comments` array, creating an auditable timeline alongside the codebase.
- **Stateless Step Execution**: By default (`session_mode="stateless"`), each AI Worker step executes atomically by inspecting local `project.json` and `task.json` files—ensuring zero context rot and straightforward fault recovery. Tasks may also opt into `session_mode="sticky"` to resume warm CLI sessions when supported by the active harness.
- **Explicit Hand-Off Protocol**: Control passes asynchronously between `Admin` and `AI` via the `assignee` and `status` fields.
- **Flat Physical Storage**: All subtasks reside directly under `DATA_DIR/projects/<PID>/tasks/<TID>/` without nested directory recursion.

## 3. System Components

### 3.1 Pluggable Agent Harness Layer (`harness/`)
- **`BaseHarness`**: Abstract adapter interface defining `check_health()`, `list_models()`, `run_stream()`, `steer()`, `stop()`, and typed `ErrorCategory` classification (`rate_limit`, `auth_error`, `timeout`, `binary_not_found`, `cancelled`, `process_crash`). Configures a `16 MiB` pipe buffer to prevent NDJSON line truncation.
- **`AgyHarness` (Default)**: Integrates Google Antigravity CLI (`agy`) via bidirectional `--input-format stream-json --output-format stream-json`, sandbox permission presets (`auto`, `accept-edits`, `plan`, `turbo`), dynamic `agy models` discovery, reasoning effort flags, and mid-flight steering.
- **`HarnessRegistry` & `HarnessRouter`**: Discovers available adapters and resolves effective execution profiles in **Task > Project > Global** order, stepping along the configured fallback chain only on rate-limit errors.

### 3.2 Unified Async Orchestrator (`orchestrator/`)
- **`AsyncWatcher`**: Runs inside the unified `asyncio` event loop with per-project locks, concurrency limits (`MAX_CONCURRENT_WORKERS`), global/per-project pause controls, live stop/steer tracking, and periodic `git` auto-commits inside `DATA_DIR`.
- **`Worker`**: Injects `system_prompt.md` (seeded from `orchestrator/prompts/default_worker.md`), snapshots project JSON before each step, validates JSON syntax + Pydantic `Task` schema immediately after each step, attempts single-shot repair on corruption, and restores the pre-step snapshot if repair fails.
- **Data Storage Layer**: Atomic JSON writes (`mkstemp` + `os.replace`) maintaining the 9-Field Schema plus optional `harness_config` and `execution_meta`.

### 3.3 Async Telegram UI (`telegram_ui/`) & Optional Web Panel (`web/`)
- **`TelegramClient`**: Lightweight async `httpx` client with bot-token redaction, `429` retry handling, `409` polling backoff, idempotent retries, and HTML-to-plain-text fallback.
- **Interactive Controls & Live Streaming**: Deny-by-default authorization (`ALLOWED_CHAT_IDS` / `ADMIN_CHAT_ID`), inline keyboards for projects/tasks/files/harnesses/models/effort/permissions, direct reply-to-card commenting, live streaming worker progress cards with `<blockquote expandable>` tool traces, offline standalone HTML file renderers (zero external CDNs), and backwards-compatible multi-line VOP batch commands.
- **Optional Web Panel**: FastAPI dashboard on port `8899` (toggleable via `WEB_ENABLED=true|false`).

## 4. Directory Layout

```text
TodoX/
├── todox_main.py               # Unified asyncio entrypoint
├── harness/                    # Pluggable CLI agent harness layer (agy + extensible adapters)
├── telegram_ui/                # Async Telegram client, keyboards, live notifier, offline HTML renderers
├── orchestrator/               # Models, atomic storage, state, async watcher, worker, prompts
├── web/                        # Optional FastAPI dashboard (:8899)
└── tests/                      # Isolated unittest modules

DATA_DIR/                       # Runtime workspace (gitignored from TodoX code repo)
├── system_state.json
├── latest_worker_output.log
└── projects/
    └── <PID>/
        ├── project.json        # Root PID-0 task
        ├── system_prompt.md    # Project-specific worker behavior prompt
        ├── files/
        └── tasks/
            └── <TID>/
                ├── task.json
                └── files/
```

## 5. Core 9-Field JSON Schema

Every `project.json` and `task.json` contains:
- `id`: Unique `PID-N` identifier (`PID-0` for project root).
- `status`: `not_started`, `in_progress`, `blocked`, `need_confirm`, `complete`, `failed`.
- `title`: One-line task summary.
- `descriptions`: Detailed specifications and constraints.
- `creator`: `Admin` or `AI`.
- `assignee`: `Admin` or `AI`.
- `parent_id`: Parent TID (`null` for `PID-0`).
- `child_ids`: List of child TIDs.
- `comments`: Array of `{author, time, content}` entries.
- Optional v2 fields: `harness_config` (per-project/task overrides) and `execution_meta` (telemetry).
