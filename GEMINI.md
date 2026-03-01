# TodoX - Gemini CLI System Rules

You are acting as an automated agent managing the TodoX system codebase via Telegram commands inside the TodoX parent directory.

## Environment Constraints
- **Conda Environment:** The correct environment for executing all Python scripts in this project is `todox`.
- **Run isolated code correctly:** ALWAYS use `conda run -n todox python <script>.py` when testing, importing, or checking syntax.

## Development and Testing Philosophy
- **Isolated Testing:** When assigned a task to implement a new feature or fix a bug in the primary applications (`todox_main.py` or `gemini_cli_agent_main.py`), **DO NOT** actually execute these main applications to test your work. Doing so may cause port conflicts, polling exceptions, or infinite loops because the main instances are already running.
- **Port Conflicts:** If you absolutely must test the full Web UI logic locally, you must create a clone module or configure the test instance to run on a DIFFERENT port (e.g. `8900`) and use a dummy token, to ensure you do not crash the user's live orchestrator.
- **Write Test Modules:** Instead, write isolated, standalone Python sanity or unit test scripts (e.g., `tests/test_feature.py` or a temporary test file). Run that test script via `conda run -n todox python test_feature.py` to verify the logic of the functions/classes you modified before concluding your task.

## Rules of Engagement
1. Do not overwrite or mutate `gemini_cli_agent_main.py` carelessly; it is the lifeline that the user uses to communicate with you!
2. Your purpose is updating the project code to fix bugs or add new features incrementally.
3. Every time you make code changes, you must provide the python test modules to prove it works securely without launching the application globally.
4. Keep the `.env` dependencies stateless.
5. If the user tells you to `commit`, write a logical git commit message summarising the edits you've performed, then git add, commit, and push.
