# TodoX

TodoX is an AI-driven project management and development orchestrator built on a flat filesystem layout, the 9-Field JSON task schema, and a pluggable multi-CLI AI agent harness layer.

## Features

- **Pluggable AI Agent Harness Layer (`harness/`)**:
  - Clean `BaseHarness` abstraction with normalized `HarnessEvent` streaming, canonical tool mapping (`shell`, `read`, `write`, `edit`, `search`, `web`, `agent`, `mcp`), typed `ErrorCategory` handling, and a 16 MiB pipe buffer to prevent large NDJSON line truncation.
  - **`AgyHarness` (Default)**: Integrates Google Antigravity CLI (`agy`) via bidirectional `--input-format stream-json --output-format stream-json`, dynamic `agy models` discovery, reasoning effort controls, permission presets (`auto`, `accept-edits`, `plan`, `turbo`), and mid-flight steering support.
  - **Extensible Registry & 3-Level Routing**: `HarnessRegistry` and `HarnessRouter` resolve overrides in **Task > Project > Global** order and automatically step through a configurable fallback chain on rate-limit (`429`) errors (ready for upcoming Claude Code and Codex CLI adapters).
- **Post-Step JSON Guardrails**: Snapshots project/task JSON before each AI worker step, validates syntax + Pydantic schema immediately after completion, attempts automatic repair if malformed, and restores the snapshot if repair fails.
- **C&C via Telegram & Web Panel**: Manage projects, subtasks, and narrative `comments` through Telegram commands or the FastAPI Web UI on port `8899`.

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

   # AI Agent Harness Defaults
   DEFAULT_HARNESS=agy
   AGY_BIN_PATH=
   DEFAULT_MODEL=default
   DEFAULT_EFFORT=high
   DEFAULT_PERMISSION_PRESET=auto
   WORKER_TIMEOUT_SECONDS=300
   ```

## Usage

1. Start the Orchestrator, Web UI (`http://localhost:8899`), and background Watcher:
   ```bash
   conda activate todox
   python todox_main.py
   ```
2. Optional Telegram commands:
   - `/projects`
   - `/new-project -pid [PID] -title "[Title]" -des "[Description]"`
   - `/get-project -pid [PID]`
   - `/update-task -pid [PID] -tid [TID] -field assignee -val AI`
   - `/sys` (Inspect active harness, model, effort, permission preset, and fallback chain)
   - `/upmodel [Index]` (Switch active entry along the fallback chain)
   - `/output [number]` (Inspect recent worker log lines)
   - `/fix-data` (Validate and repair malformed JSON files via the active harness)

## Module Self-Tests & Verification

Every core module can be tested independently from the command line without starting the live Telegram bot or burning LLM tokens:

```bash
# Run isolated unit & integration tests
conda run -n todox python -m unittest tests/test_harness_types.py tests/test_agy_harness.py tests/test_harness_router_and_state.py tests/test_worker_and_validation.py -v

# Run standalone module checks
conda run -n todox python -m harness.types
conda run -n todox python -m harness.base
conda run -n todox python -m harness.agy_harness
conda run -n todox python -m harness.registry
conda run -n todox python -m harness.router
conda run -n todox python -m orchestrator.state
conda run -n todox python -m orchestrator.storage
conda run -n todox python -m orchestrator.worker
conda run -n todox python orchestrator/test_bot_fix.py
```

## Maintenance

- **Automated Restart**: Use `/restart` in the Telegram bot or run `./restart.sh` to pull code, update dependencies, and restart `todox_main.py`.
- **Logs**: Worker logs are written to `DATA_DIR/latest_worker_output.log`.
