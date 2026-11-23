import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message, Update

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import legacy.bot_v1 as bot_module
from legacy.bot_v1 import handle_fix_data


async def run_test():
    update = AsyncMock(spec=Update)
    update.message = AsyncMock(spec=Message)

    with tempfile.TemporaryDirectory() as tmpdir:
        test_pid = "TESTBOT"
        os.makedirs(os.path.join(tmpdir, "projects", test_pid, "tasks", "TESTBOT-1"), exist_ok=True)
        with open(os.path.join(tmpdir, "projects", test_pid, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "TESTBOT-0", "title": "Test Title"}, f)
        with open(os.path.join(tmpdir, "projects", test_pid, "tasks", "TESTBOT-1", "task.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "TESTBOT-1", "title": "Test Task 1"}, f)

        # 1. Data dir not found
        with patch("legacy.bot_v1.get_data_dir", return_value=os.path.join(tmpdir, "missing")):
            await handle_fix_data(update, {})
            update.message.reply_text.assert_called_with("Data directory not found.")

        update.message.reply_text.reset_mock()

        # 2. All JSON valid
        with patch("legacy.bot_v1.get_data_dir", return_value=tmpdir), \
             patch.object(bot_module, "run_harness_fix_stream", new_callable=AsyncMock) as mock_exec:
            await handle_fix_data(update, {})
            update.message.reply_text.assert_called_with("✅ All json files in all projects are valid.")
            mock_exec.assert_not_called()

        update.message.reply_text.reset_mock()

        # 3. Invalid JSON triggers harness repair flow
        with open(os.path.join(tmpdir, "projects", test_pid, "project.json"), "w", encoding="utf-8") as f:
            f.write("{ bad json ")

        mock_state = MagicMock()
        mock_state.get_current_harness.return_value = "agy"

        with patch("legacy.bot_v1.get_data_dir", return_value=tmpdir), \
             patch.object(bot_module, "state", mock_state), \
             patch.object(bot_module, "run_harness_fix_stream", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = ["Fixed project.json"]
            await handle_fix_data(update, {})

            update.message.reply_text.assert_any_call("Found 1 JSON errors. Invoking AI harness (agy) to fix...")
            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["harness_name"] == "agy"
            assert call_kwargs["out_lines"] == 10
            assert "project.json" in call_kwargs["instruction"]

            send_chunk_cb = call_kwargs["send_chunk_callback"]
            await send_chunk_cb('<pre><code class="language-text">Simulated output from agy harness</code></pre>')
            update.message.reply_text.assert_any_call(
                '<pre><code class="language-text">Simulated output from agy harness</code></pre>',
                parse_mode="HTML",
            )
            update.message.reply_text.assert_any_call("✅ AI harness (agy) finished working on fixes.")

    print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(run_test())
