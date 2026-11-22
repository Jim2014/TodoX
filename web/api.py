import os
from fastapi import APIRouter, HTTPException
from orchestrator.storage import (
    get_data_dir,
    get_default_worker_prompt,
    create_project, delete_project, update_project,
    create_task, delete_task, update_task,
    add_comment, delete_comment, update_comment
)
from web.models import (
    ProjectCreate, ProjectUpdate,
    TaskCreate, TaskUpdate,
    CommentCreate, CommentUpdate
)

router = APIRouter()

@router.post("/api/projects")
def api_create_project(data: ProjectCreate):
    pid = data.pid.upper()
    seed_prompt = get_default_worker_prompt()
    create_project(pid, data.title, data.desc, seed_prompt)
    return {"status": "success", "pid": pid}

@router.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    if delete_project(pid.upper()):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.put("/api/projects/{pid}")
def api_update_project(pid: str, data: ProjectUpdate):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if update_project(pid.upper(), **updates):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.post("/api/tasks/{pid}/{parent_tid}")
def api_create_task(pid: str, parent_tid: str, data: TaskCreate):
    t = create_task(pid.upper(), parent_tid.upper(), data.title, data.desc, data.assignee)
    if t:
        return {"status": "success", "tid": t.id}
    raise HTTPException(status_code=404, detail="Parent not found")

@router.delete("/api/tasks/{pid}/{tid}")
def api_delete_task(pid: str, tid: str):
    if delete_task(pid.upper(), tid.upper()):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.put("/api/tasks/{pid}/{tid}")
def api_update_task(pid: str, tid: str, data: TaskUpdate):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if update_task(pid.upper(), tid.upper(), **updates):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.post("/api/comments/{pid}/{tid}")
def api_create_comment(pid: str, tid: str, data: CommentCreate):
    if add_comment(pid.upper(), tid.upper(), "Admin", data.msg):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.delete("/api/comments/{pid}/{tid}/{index}")
def api_delete_comment(pid: str, tid: str, index: int):
    if delete_comment(pid.upper(), tid.upper(), index):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@router.put("/api/comments/{pid}/{tid}/{index}")
def api_update_comment(pid: str, tid: str, index: int, data: CommentUpdate):
    if update_comment(pid.upper(), tid.upper(), index, data.msg):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

from orchestrator.state import state
@router.get("/api/system_state")
def api_system_state():
    return state.get_state()
