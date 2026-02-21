import os
import time
import asyncio
from .storage import get_data_dir, load_projects, load_task
from .worker import execute_ai_task

def scan_and_trigger():
    data_dir = get_data_dir()
    projects = load_projects()
    
    for pid in projects.keys():
        project_dir = os.path.join(data_dir, "projects", pid)
        if not os.path.exists(project_dir):
            continue
            
        # Check project task (PID-0)
        task = load_task(pid, f"{pid}-0")
        if task and task.assignee == "AI" and task.status == "in_progress":
            execute_ai_task(pid, task.id)
            
        # Check subtasks
        tasks_dir = os.path.join(project_dir, "tasks")
        if os.path.exists(tasks_dir):
            for tid in os.listdir(tasks_dir):
                task = load_task(pid, tid)
                if task and task.assignee == "AI" and task.status == "in_progress":
                    execute_ai_task(pid, task.id)

def start_watcher(interval_seconds=5):
    print(f"Starting orchestrator watcher loop (interval: {interval_seconds}s)")
    while True:
        try:
            scan_and_trigger()
        except Exception as e:
            print(f"Watcher error: {e}")
        time.sleep(interval_seconds)
