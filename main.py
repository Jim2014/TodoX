import threading
from orchestrator.bot import run_bot
from orchestrator.watcher import start_watcher
from web.app import run_web

def main():
    print("--------------------------------------------------")
    print("TodoX Orchestrator System Starting...")
    print("--------------------------------------------------")
    
    # Start Web UI in separate process
    web_process = run_web()
    print("Web UI Dashboard running on port 8000...")
    
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
