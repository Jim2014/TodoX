# Legacy Scripts Archive

This directory preserves superseded v1 scripts removed from the main source tree so the active codebase remains focused on the modern async orchestrator (`harness/`, `orchestrator/`, `telegram_ui/`, `web/`, and `todox_main.py`).

| Archived File | Replaced By | Notes |
| :--- | :--- | :--- |
| `bot_v1.py` | [`telegram_ui/bot.py`](../telegram_ui/bot.py) | Early synchronous `python-telegram-bot` polling prototype and v1 `/fix_data` handler |
| `test_bot_fix.py` | [`tests/test_worker_and_validation.py`](../tests/test_worker_and_validation.py) | Standalone verification script for `bot_v1.handle_fix_data` |
| `test_gcli_flags.py` | [`tests/test_agy_harness.py`](../tests/test_agy_harness.py) | Flag-parsing unit test for legacy Gemini CLI wrapper |
| `tests/test_commit_data.py` | Automatic git hygiene & snapshots | Experimental auto-commit helper |
| `tests/test_fix_data.py` | [`orchestrator/storage.py`](../orchestrator/storage.py) | Early JSON checker draft |
| `tests/test_state.py` | [`tests/test_harness_router_and_state.py`](../tests/test_harness_router_and_state.py) | Manual FastAPI lifecycle test requiring live server |
| `tests/test_storage.py` | [`tests/test_worker_and_validation.py`](../tests/test_worker_and_validation.py) | Manual project listing sanity script |

*(Note: `gemini_cli_agent_main.py` and `orchestrator/gemini_runner.py` remain at their original paths for backwards compatibility with existing installations.)*
