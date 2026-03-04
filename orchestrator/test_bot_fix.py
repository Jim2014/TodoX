import os
import json
import asyncio
from unittest.mock import AsyncMock, patch
from telegram import Update, Message

# Import the handler from the main project
from orchestrator.bot import handle_fix_data

async def run_test():
    # Setup mock update
    update = AsyncMock(spec=Update)
    update.message = AsyncMock(spec=Message)
    
    import orchestrator.bot as bot_module
    
    # Create test data dir
    test_data_dir = "./test_data"
    test_pid = "TESTBOT"
    
    import shutil
    if os.path.exists(test_data_dir):
        shutil.rmtree(test_data_dir)
        
    os.makedirs(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTBOT-1"), exist_ok=True)
    with open(os.path.join(test_data_dir, "projects", test_pid, "project.json"), "w") as f:
        json.dump({"title": "Test Title"}, f)
    with open(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTBOT-1", "task.json"), "w") as f:
        json.dump({"title": "Test Task 1"}, f)
    
    # 1. Test project not found
    args = {"pid": "NOTFOUND"}
    with patch('orchestrator.bot.get_data_dir', return_value=test_data_dir):
        await handle_fix_data(update, args)
        update.message.reply_text.assert_called_with("Error: Project NOTFOUND not found.")
        
    # 2. Test everything valid. Remove the bad files from step 1 first.
    with open(os.path.join(test_data_dir, "projects", test_pid, "project.json"), "w") as f:
        json.dump({"title": "Test Title"}, f)
    with open(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTBOT-1", "task.json"), "w") as f:
        json.dump({"title": "Test Task 1"}, f)
        
    args = {"pid": test_pid}
    with patch('orchestrator.bot.get_data_dir', return_value=test_data_dir), \
         patch.object(bot_module, 'run_gemini_cli_stream', new_callable=AsyncMock) as mock_exec:
        await handle_fix_data(update, args)
        update.message.reply_text.assert_called_with(f"✅ All json files in project {test_pid} are valid.")

    update.message.reply_text.reset_mock()
    if isinstance(mock_exec, AsyncMock):
        mock_exec.reset_mock()

    # 3. Test invalid json
    with open(os.path.join(test_data_dir, "projects", test_pid, "project.json"), "w") as f:
        f.write("{ bad json ")
        
    # We mock asyncio.create_subprocess_exec to simulate the cli returning something
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (b"Simulated output from gemini cli", b"")
    
    with patch('orchestrator.bot.get_data_dir', return_value=test_data_dir), \
         patch.object(bot_module, 'run_gemini_cli_stream', new_callable=AsyncMock) as mock_exec:
        
        mock_exec.return_value = ["Simulated output from gemini cli"]
        
        await handle_fix_data(update, args)
        
        # Verify initial message was sent
        update.message.reply_text.assert_any_call(f"Found 1 JSON errors in project {test_pid}. Invoking Gemini CLI to fix...")
        
        # Verify gemini cmd
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash"
        assert call_kwargs["out_lines"] == 10
        assert "- ./test_data/projects/TESTBOT/project.json:" in call_kwargs["instruction"]
        
        # Simulate sending a chunk to the callback defined inside the function
        send_chunk_cb = call_kwargs["send_chunk_callback"]
        await send_chunk_cb("<pre><code class=\"language-text\">Simulated output from gemini cli</code></pre>")
        
        # Verify final msg from chunk callback was sent correctly
        update.message.reply_text.assert_any_call(
            "<pre><code class=\"language-text\">Simulated output from gemini cli</code></pre>", 
            parse_mode='HTML'
        )
        
        # Verify completely finished text
        update.message.reply_text.assert_any_call("✅ Gemini CLI finished working on fixes.")
        
    print("All tests passed!")

if __name__ == "__main__":
    asyncio.run(run_test())
