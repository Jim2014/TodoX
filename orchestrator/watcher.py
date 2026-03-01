import os
import time
import asyncio
import subprocess
from datetime import datetime
from .storage import get_data_dir, load_projects, load_task
from .worker import execute_ai_task
from .state import state

def scan_and_trigger():
    data_dir = get_data_dir()
    projects = load_projects()
    
    for pid in projects.keys():
        project_dir = os.path.join(data_dir, "projects", pid)
        if not os.path.exists(project_dir):
            continue
        should_trigger = False
        
        # Check project task (PID-0)
        task = load_task(pid, f"{pid}-0")
        if task and task.assignee == "AI" and task.status == "in_progress":
            should_trigger = True
            
        # Check subtasks
        if not should_trigger:
            tasks_dir = os.path.join(project_dir, "tasks")
            if os.path.exists(tasks_dir):
                for tid in os.listdir(tasks_dir):
                    task = load_task(pid, tid)
                    if task and task.assignee == "AI" and task.status == "in_progress":
                        should_trigger = True
                        break
                        
        if should_trigger:
            execute_ai_task(pid)

def check_and_commit_data(data_dir: str):
    if not os.path.exists(data_dir):
        return
        
    git_dir = os.path.join(data_dir, ".git")
    if not os.path.exists(git_dir):
        return
        
    try:
        status_output = subprocess.check_output(
            ["git", "status", "--porcelain"], 
            cwd=data_dir, 
            stderr=subprocess.STDOUT,
            text=True
        )
        if status_output.strip():
            subprocess.run(["git", "add", "."], cwd=data_dir, check=True)
            commit_message = f"Auto-commit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            subprocess.run(["git", "commit", "-m", commit_message], cwd=data_dir, check=True)
            print(f"Committed changes in data directory: {commit_message}")
    except subprocess.CalledProcessError as e:
        print(f"Failed to check or commit data directory: {e}")
    except Exception as e:
        print(f"Error during auto-commit: {e}")

def start_watcher():
    print(f"Starting orchestrator watcher loop")
    last_commit_time = time.time()
    
    while True:
        try:
            scan_and_trigger()
        except Exception as e:
            print(f"Watcher error: {e}")
            
        current_time = time.time()
        if (current_time - last_commit_time) >= 300: # 5 minutes
            try:
                check_and_commit_data(get_data_dir())
            except Exception as e:
                print(f"Auto-commit error: {e}")
            last_commit_time = current_time
            
        time.sleep(state.get_interval())
