import sys
import os

# Prevent crash when running via pythonw on Windows
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import threading
from orchestrator.state import state
from orchestrator.bot import run_bot
from orchestrator.watcher import start_watcher
from web.app import run_web

from datetime import datetime

def main():
    state.reset_model()
    
    start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state.set_start_time(start_time_str)
    
    print("--------------------------------------------------")
    print(f"TodoX Orchestrator System Starting at {start_time_str}...")
    print("--------------------------------------------------")
    
    # Start Web UI in separate process
    web_process = run_web()
    print("Web UI Dashboard running on port 8899...")
    
    # Start Watcher in background thread
    watcher_thread = threading.Thread(target=start_watcher, daemon=True)
    watcher_thread.start()
    
    # Run Telegram Bot in main thread (blocks execution loop)
    # Check if TG_TOKEN exists, otherwise it will just print warning and return immediately
    run_bot()
    
    print("TodoX Bot loop exited. If Telegram Token was missing, Web API and watcher will continue to run in background.")
    try:
        web_process.join()  # Prevent script from exiting immediately if bot fails
    except KeyboardInterrupt:
        print("Shutting down TodoX...")

if __name__ == "__main__":
    main()
