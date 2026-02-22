import os
import json
import shutil
from datetime import datetime
from typing import Dict, Optional, Any
from dotenv import load_dotenv
from .models import Task, ProjectSummary, Comment

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
        status="in_progress",
        assignee="AI"
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

def delete_project(pid: str) -> bool:
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)
    
    if not os.path.exists(project_dir):
        return False
        
    shutil.rmtree(project_dir)
    
    projects = load_projects()
    if pid in projects:
        del projects[pid]
        save_projects(projects)
        
    return True

def update_project(pid: str, **kwargs) -> Optional[Task]:
    # Updates the PID-0 task which acts as the project root metadata
    task = load_task(pid, f"{pid}-0")
    if not task:
        return None
        
    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)
            
    save_task(pid, task)
    
    # Sync with global projects.json
    projects = load_projects()
    if pid in projects:
        if "title" in kwargs:
            projects[pid].title = kwargs["title"]
        if "status" in kwargs:
            projects[pid].status = kwargs["status"]
        if "assignee" in kwargs:
            projects[pid].assignee = kwargs["assignee"]
        if "descriptions" in kwargs:
            # We don't store descriptions in ProjectSummary, but we should make sure 
            # if we do in the future, it's addressed. The root task already got it.
            pass
        save_projects(projects)
        
    return task

def create_task(pid: str, parent_tid: str, title: str, descriptions: str, assignee: str = "Admin") -> Optional[Task]:
    parent_task = load_task(pid, parent_tid)
    if not parent_task:
        return None
        
    data_dir = get_data_dir()
    tasks_dir = os.path.join(data_dir, "projects", pid, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)
    
    # Generate new TID
    existing_tids = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d))]
    
    # Simple increment logic for TIDs
    max_num = 0
    for t in existing_tids:
        try:
            num = int(t.split('-')[1])
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            pass
            
    new_tid = f"{pid}-{max_num + 1}"
    
    new_task = Task(
        id=new_tid,
        title=title,
        descriptions=descriptions,
        assignee=assignee,
        parent_id=parent_tid,
        status="in_progress" if assignee.upper() == "AI" else "not_started"
    )
    
    save_task(pid, new_task)
    
    # Update parent
    parent_task.child_ids.append(new_tid)
    save_task(pid, parent_task)
    
    return new_task

def delete_task(pid: str, tid: str) -> bool:
    if tid == f"{pid}-0":
        return delete_project(pid) # Cannot delete root task independently
        
    task = load_task(pid, tid)
    if not task:
        return False
        
    data_dir = get_data_dir()
    task_dir = os.path.join(data_dir, "projects", pid, "tasks", tid)
    
    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)
        
    # Remove from parent
    if task.parent_id:
        parent_task = load_task(pid, task.parent_id)
        if parent_task and tid in parent_task.child_ids:
            parent_task.child_ids.remove(tid)
            save_task(pid, parent_task)
            
    return True

def update_task(pid: str, tid: str, **kwargs) -> Optional[Task]:
    if tid == f"{pid}-0":
        return update_project(pid, **kwargs)
        
    task = load_task(pid, tid)
    if not task:
        return None
        
    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)
            
    save_task(pid, task)
    return task

def add_comment(pid: str, tid: str, author: str, content: str) -> Optional[Task]:
    task = load_task(pid, tid)
    if not task:
        return None
        
    task.comments.append(Comment(
        author=author,
        time=datetime.now().isoformat(),
        content=content
    ))
    save_task(pid, task)
    return task

def delete_comment(pid: str, tid: str, comment_index: int) -> Optional[Task]:
    task = load_task(pid, tid)
    if not task:
        return None
        
    if 0 <= comment_index < len(task.comments):
        task.comments.pop(comment_index)
        save_task(pid, task)
        return task
    return None

def update_comment(pid: str, tid: str, comment_index: int, content: str) -> Optional[Task]:
    task = load_task(pid, tid)
    if not task:
        return None
        
    if 0 <= comment_index < len(task.comments):
        task.comments[comment_index].content = content
        # Optionally update time to reflect edit, but keeping original might be better for an audit log.
        save_task(pid, task)
        return task
    return None
