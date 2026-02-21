import os
import json
from typing import Dict, Optional
from dotenv import load_dotenv
from .models import Task, ProjectSummary

load_dotenv()

def get_data_dir() -> str:
    return os.environ.get("DATA_DIR", "./data")

def get_projects_file() -> str:
    return os.path.join(get_data_dir(), "projects.json")

def load_projects() -> Dict[str, ProjectSummary]:
    projects_file = get_projects_file()
    if not os.path.exists(projects_file):
        return {}
    with open(projects_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        return {k: ProjectSummary(**v) for k, v in data.items()}

def save_projects(projects: Dict[str, ProjectSummary]):
    projects_file = get_projects_file()
    os.makedirs(os.path.dirname(projects_file), exist_ok=True)
    with open(projects_file, "w", encoding="utf-8") as f:
        json.dump({k: v.model_dump() for k, v in projects.items()}, f, indent=4)

def _get_task_path(pid: str, tid: str) -> str:
    data_dir = get_data_dir()
    if tid == f"{pid}-0":
        return os.path.join(data_dir, "projects", pid, "project.json")
    else:
        return os.path.join(data_dir, "projects", pid, "tasks", tid, "task.json")

def load_task(pid: str, tid: str) -> Optional[Task]:
    path = _get_task_path(pid, tid)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return Task(**json.load(f))

def save_task(pid: str, task: Task):
    path = _get_task_path(pid, task.id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task.model_dump(), f, indent=4)
        
def create_project(pid: str, title: str, descriptions: str, system_prompt: str) -> Task:
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)
    
    # Create directory structure
    os.makedirs(os.path.join(project_dir, "files"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "tasks"), exist_ok=True)
    
    # Write system_prompt.md
    with open(os.path.join(project_dir, "system_prompt.md"), "w", encoding="utf-8") as f:
        f.write(system_prompt)
        
    # Create PID-0 project task
    tid = f"{pid}-0"
    project_task = Task(
        id=tid,
        title=title,
        descriptions=descriptions,
        status="in_progress"
    )
    save_task(pid, project_task)
    
    # Update global projects.json
    projects = load_projects()
    projects[pid] = ProjectSummary(
        id=pid,
        title=title,
        status=project_task.status,
        assignee=project_task.assignee
    )
    save_projects(projects)
    
    return project_task
