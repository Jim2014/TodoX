import os
import json
import shutil
import tempfile
from datetime import datetime
from typing import Dict, Optional, Any
from dotenv import load_dotenv
from .models import Task, ProjectSummary, Comment

load_dotenv()

def _atomic_write_json(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(temp_path, path)
    except Exception:
        os.unlink(temp_path)
        raise

def get_data_dir() -> str:
    raw_dir = os.environ.get("DATA_DIR", "./data")
    if not os.path.isabs(raw_dir):
        # Resolve relative to the project root directory (which is 2 levels up from this file)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(project_root, raw_dir))
    return raw_dir

def load_projects() -> Dict[str, ProjectSummary]:
    data_dir = get_data_dir()
    projects_dir = os.path.join(data_dir, "projects")
    result = {}
    
    if not os.path.exists(projects_dir):
        return result
        
    for pid in os.listdir(projects_dir):
        project_path = os.path.join(projects_dir, pid)
        if os.path.isdir(project_path):
            task = load_task(pid, f"{pid}-0")
            if task:
                result[pid] = ProjectSummary(
                    id=pid,
                    title=task.title,
                    status=task.status,
                    assignee=task.assignee
                )
    return result

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
    _atomic_write_json(path, task.model_dump())
        

        
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
    

    
    return project_task

def delete_project(pid: str) -> bool:
    data_dir = get_data_dir()
    project_dir = os.path.join(data_dir, "projects", pid)
    
    if not os.path.exists(project_dir):
        return False
        
    shutil.rmtree(project_dir)
    

        
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
    

        
    return task

def create_task(pid: str, parent_tid: str, title: str, descriptions: str, assignee: str = "AI", status: str = "not_started") -> Optional[Task]:
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
        status=status
    )
    
    save_task(pid, new_task)
    
    # Update parent
    parent_task.child_ids.append(new_tid)
    save_task(pid, parent_task)
    
    # Update project task
    project_task = load_task(pid, f"{pid}-0")
    if project_task:
        changed = False
        if project_task.status != "in_progress":
            project_task.status = "in_progress"
            changed = True
        if str(project_task.assignee).upper() != "AI":
            project_task.assignee = "AI"
            changed = True
        if changed:
            save_task(pid, project_task)
            
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
    
    if str(kwargs.get('assignee', '')).upper() == 'AI':
        project_task = load_task(pid, f"{pid}-0")
        if project_task:
            changed = False
            if project_task.status != "in_progress":
                project_task.status = "in_progress"
                changed = True
            if str(project_task.assignee).upper() != "AI":
                project_task.assignee = "AI"
                changed = True
            if changed:
                save_task(pid, project_task)
                
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
