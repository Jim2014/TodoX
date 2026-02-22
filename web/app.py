import os
import sys
import multiprocessing
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.storage import (
    load_projects, load_task, get_data_dir,
    create_project, delete_project, update_project,
    create_task, delete_task, update_task,
    add_comment, delete_comment, update_comment
)

app = FastAPI(title="TodoX Web Panel")

# --- Pydantic Models ---
class ProjectCreate(BaseModel):
    pid: str
    title: str
    desc: str

class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None

class TaskCreate(BaseModel):
    title: str
    desc: str
    assignee: str = "Admin"

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None

class CommentCreate(BaseModel):
    msg: str

class CommentUpdate(BaseModel):
    msg: str

# --- API Endpoints ---
@app.post("/api/projects")
def api_create_project(data: ProjectCreate):
    pid = data.pid.upper()
    prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "orchestrator", "prompts", "default_gemini.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        seed_prompt = f.read()
    create_project(pid, data.title, data.desc, seed_prompt)
    gemini_md_path = os.path.join(get_data_dir(), "projects", pid, "GEMINI.md")
    with open(gemini_md_path, "w", encoding="utf-8") as f:
        f.write(seed_prompt)
    return {"status": "success", "pid": pid}

@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    if delete_project(pid.upper()):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.put("/api/projects/{pid}")
def api_update_project(pid: str, data: ProjectUpdate):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if update_project(pid.upper(), **updates):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.post("/api/tasks/{pid}/{parent_tid}")
def api_create_task(pid: str, parent_tid: str, data: TaskCreate):
    t = create_task(pid.upper(), parent_tid.upper(), data.title, data.desc, data.assignee)
    if t:
        return {"status": "success", "tid": t.id}
    raise HTTPException(status_code=404, detail="Parent not found")

@app.delete("/api/tasks/{pid}/{tid}")
def api_delete_task(pid: str, tid: str):
    if delete_task(pid.upper(), tid.upper()):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.put("/api/tasks/{pid}/{tid}")
def api_update_task(pid: str, tid: str, data: TaskUpdate):
    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if update_task(pid.upper(), tid.upper(), **updates):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.post("/api/comments/{pid}/{tid}")
def api_create_comment(pid: str, tid: str, data: CommentCreate):
    if add_comment(pid.upper(), tid.upper(), "Admin", data.msg):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.delete("/api/comments/{pid}/{tid}/{index}")
def api_delete_comment(pid: str, tid: str, index: int):
    if delete_comment(pid.upper(), tid.upper(), index):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

@app.put("/api/comments/{pid}/{tid}/{index}")
def api_update_comment(pid: str, tid: str, index: int, data: CommentUpdate):
    if update_comment(pid.upper(), tid.upper(), index, data.msg):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Not found")

# --- HTML Views ---
HTML_STYLE = """
<style>
body { font-family: sans-serif; margin: 0; padding: 20px; background: #fff; }
.container { max-width: 800px; margin: 0 auto; }
.layout-container { display: flex; min-height: 90vh; max-width: 1400px; margin: 0 auto; gap: 20px; }
.sidebar { width: 400px; border-right: 1px solid #eee; padding-right: 20px; overflow-y: auto; }
.main-pane { flex: 1; display: flex; flex-direction: column; }
.iframe-container { flex: 1; min-height: 80vh; border: 1px solid #ccc; width: 100%; border-radius: 4px; }
.file-link { display: block; margin: 5px 0; padding: 8px; background: #f8f9fa; border-radius: 4px; cursor: pointer; text-decoration: none; color: #333; border: 1px solid #eee; }
.file-link:hover { background: #e2e6ea; border-color: #dae0e5; }
.card { border: 1px solid #ddd; padding: 15px; margin-bottom: 15px; border-radius: 5px; background: #fafafa; display: flex; justify-content: space-between; align-items: center; }
.card-content { flex: 1; }
.card-actions { margin-left: 20px; }
input, select, button, textarea { margin: 5px 0; padding: 8px; border-radius: 4px; border: 1px solid #ccc; }
button { background: #333; color: white; cursor: pointer; border: none; }
button:hover { background: #555; }
.danger { background: #dc3545; }
.danger:hover { background: #c82333; }
hr { border: 0; border-top: 1px solid #eee; margin: 30px 0; }
a { color: #007bff; text-decoration: none; }
a:hover { text-decoration: underline; }
textarea { width: 100%; min-height: 80px; font-family: inherit; }
pre { background: #eee; padding: 10px; border-radius: 4px; white-space: pre-wrap; }
</style>
<script>
async function apiCall(method, url, body=null) {
    const opts = { method: method, headers: {} };
    if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    try {
        const res = await fetch(url, opts);
        if (!res.ok) {
            const err = await res.text();
            alert("Error: " + err);
        } else {
            location.reload();
        }
    } catch (e) {
        alert("Request failed: " + e);
    }
}
</script>
"""

@app.get("/", response_class=HTMLResponse)
def read_root():
    projects = load_projects()
    html = f"<html><head><title>TodoX Panel</title>{HTML_STYLE}</head><body><div class='container'>"
    html += "<h1>TodoX Project Dashboard</h1>"
    
    html += "<h3>Create Project</h3>"
    html += "<div style='display:flex; flex-direction:column; gap:10px; max-width: 600px;'>"
    html += "<input id='pid' placeholder='PID (e.g. ALPHA)' style='width:100%; box-sizing:border-box;'>"
    html += "<input id='title' placeholder='Title' style='width:100%; box-sizing:border-box;'>"
    html += "<textarea id='desc' placeholder='Description' rows='4' style='width:100%; box-sizing:border-box;'></textarea>"
    html += "<button onclick='apiCall(\"POST\", \"/api/projects\", {pid: document.getElementById(\"pid\").value, title: document.getElementById(\"title\").value, desc: document.getElementById(\"desc\").value})' style='align-self:flex-start;'>Create Project</button>"
    html += "</div><hr>"

    if not projects:
        html += "<p>No projects found.</p>"
    else:
        for pid, proj in projects.items():
            html += f"<div class='card'><div class='card-content'><b><a href='/project/{pid}'>{pid}</a></b>: {proj.title} <br><small>[{proj.status}] - Assignee: {proj.assignee}</small></div>"
            html += f"<div class='card-actions'><button class='danger' onclick='if(confirm(\"Delete {pid} and all its tasks?\")) apiCall(\"DELETE\", \"/api/projects/{pid}\")'>Delete</button></div></div>"
            
    html += "</div></body></html>"
    return html

@app.get("/project/{pid}", response_class=HTMLResponse)
def read_project(pid: str):
    pid = pid.upper()
    proj_task = load_task(pid, f"{pid}-0")
    if not proj_task:
        raise HTTPException(status_code=404, detail="Project not found")
        
    html = f"<html><head><title>Project {pid}</title>{HTML_STYLE}</head><body>"
    html += "<div class='layout-container'><div class='sidebar'>"
    
    html += f"<h1>Project: {pid}</h1>"
    html += f"<p><b>Title:</b> {proj_task.title}</p>"
    html += f"<p><b>Status:</b> {proj_task.status}</p>"
    html += f"<p><b>Assignee:</b> {proj_task.assignee}</p>"
    html += f"<p><b>Description:</b> {proj_task.descriptions}</p>"
    
    html += "<h3>Edit Project Details</h3>"
    html += f"<input id='edit_title' value='{proj_task.title}' placeholder='Title'><br>"
    html += f"<textarea id='edit_desc' placeholder='Description'>{proj_task.descriptions}</textarea><br>"
    html += f"<select id='status'> \
        <option value='in_progress' {'selected' if proj_task.status=='in_progress' else ''}>in_progress</option> \
        <option value='complete' {'selected' if proj_task.status=='complete' else ''}>complete</option> \
        <option value='failed' {'selected' if proj_task.status=='failed' else ''}>failed</option> \
        </select><br>"
    html += f"<select id='edit_assignee'> \
        <option value='Admin' {'selected' if proj_task.assignee=='Admin' else ''}>Admin</option> \
        <option value='AI' {'selected' if proj_task.assignee=='AI' else ''}>AI</option> \
        </select><br>"
    html += f" <button onclick='apiCall(\"PUT\", \"/api/projects/{pid}\", {{title: document.getElementById(\"edit_title\").value, descriptions: document.getElementById(\"edit_desc\").value, status: document.getElementById(\"status\").value, assignee: document.getElementById(\"edit_assignee\").value}})'>Update Details</button>"
    
    html += "<hr><h2>Subtasks</h2>"
    
    html += "<h3>Create Root Task</h3>"
    html += "<input id='title' placeholder='Task Title'><br>"
    html += "<textarea id='desc' placeholder='Description'></textarea><br>"
    html += "<select id='assignee'><option value='Admin'>Admin</option><option value='AI'>AI</option></select><br>"
    html += f"<button onclick='apiCall(\"POST\", \"/api/tasks/{pid}/{pid}-0\", {{title: document.getElementById(\"title\").value, desc: document.getElementById(\"desc\").value, assignee: document.getElementById(\"assignee\").value}})'>Create Task</button>"
    html += "<br><br>"

    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        task_ids = os.listdir(tasks_dir)
        for tid in task_ids:
            task = load_task(pid, tid)
            if task:
                html += f"<div class='card' style='display:block;'><div class='card-content'><b><a href='/task/{pid}/{tid}'>{task.id}</a></b>: {task.title} <br><small>[{task.status}] - Assignee: {task.assignee}</small></div>"
                html += f"<div class='card-actions' style='margin-top:10px;'><button class='danger' onclick='if(confirm(\"Delete {tid}?\")) apiCall(\"DELETE\", \"/api/tasks/{pid}/{tid}\")'>Delete</button></div></div>"
    
    html += "<hr><h2>Project Files</h2>"
    files_dir = os.path.join(get_data_dir(), "projects", pid, "files")
    if os.path.exists(files_dir):
        for root, _, files in os.walk(files_dir):
            for f in files:
                rel_path = os.path.relpath(os.path.join(root, f), files_dir)
                url = f"/project/{pid}/files/{rel_path}"
                html += f"<a href='{url}' target='file_viewer' class='file-link'>&#128196; {rel_path}</a>"
                
    html += "<hr><p><a href='/'>&larr; Back to Dashboard</a></p>"
    html += "</div>" # close sidebar
    
    html += "<div class='main-pane'>"
    html += "<h2>File Viewer</h2>"
    html += "<iframe name='file_viewer' class='iframe-container' src='about:blank'></iframe>"
    html += "</div>" # close main-pane
    
    html += "</div></body></html>"
    return html

@app.get("/project/{pid}/files/{path:path}")
def get_project_file(pid: str, path: str):
    pid = pid.upper()
    file_path = os.path.join(get_data_dir(), "projects", pid, "files", path)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.get("/task/{pid}/{tid}", response_class=HTMLResponse)
def read_task(pid: str, tid: str):
    pid = pid.upper()
    tid = tid.upper()
    task = load_task(pid, tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    html = f"<html><head><title>Task {tid}</title>{HTML_STYLE}</head><body><div class='container'>"
    html += f"<h1>Task: {tid}</h1>"
    html += f"<p><b>Project ID:</b> <a href='/project/{pid}'>{pid}</a></p>"
    html += f"<p><b>Parent ID:</b> {task.parent_id}</p>"
    html += f"<p><b>Title:</b> {task.title}</p>"
    html += f"<p><b>Status:</b> {task.status}</p>"
    html += f"<p><b>Assignee:</b> {task.assignee}</p>"
    html += f"<p><b>Description:</b> {task.descriptions}</p>"
    
    html += "<h3>Edit Task Details</h3>"
    html += f"<input id='edit_title' value='{task.title}' placeholder='Title'><br>"
    html += f"<textarea id='edit_desc' placeholder='Description'>{task.descriptions}</textarea><br>"
    html += f"<select id='status'> \
        <option value='not_started' {'selected' if task.status=='not_started' else ''}>not_started</option> \
        <option value='in_progress' {'selected' if task.status=='in_progress' else ''}>in_progress</option> \
        <option value='need_confirm' {'selected' if task.status=='need_confirm' else ''}>need_confirm</option> \
        <option value='complete' {'selected' if task.status=='complete' else ''}>complete</option> \
        <option value='failed' {'selected' if task.status=='failed' else ''}>failed</option> \
        </select><br>"
    html += f"<select id='assignee'> \
        <option value='Admin' {'selected' if task.assignee=='Admin' else ''}>Admin</option> \
        <option value='AI' {'selected' if task.assignee=='AI' else ''}>AI</option> \
        </select><br>"
    html += f"<button onclick='apiCall(\"PUT\", \"/api/tasks/{pid}/{tid}\", {{title: document.getElementById(\"edit_title\").value, descriptions: document.getElementById(\"edit_desc\").value, status: document.getElementById(\"status\").value, assignee: document.getElementById(\"assignee\").value}})'>Update Details</button>"

    html += "<hr><h2>Comments & Execution History</h2>"
    html += "<h3>Add Entry</h3>"
    html += "<textarea id='msg' placeholder='Type a message...'></textarea><br>"
    html += f"<button onclick='apiCall(\"POST\", \"/api/comments/{pid}/{tid}\", {{msg: document.getElementById(\"msg\").value}})'>Add Comment</button>"
    html += "<br><br>"
    
    for i, c in enumerate(task.comments):
        html += f"<div class='card' style='display:block;'><div class='card-content'><p><b>{c.author}</b> <span style='color:#777;font-size:0.8em;'>({c.time})</span>:</p><pre>{c.content}</pre></div>"
        html += f"<div class='card-actions' style='margin-left:0; margin-top:10px;'><button class='danger' onclick='if(confirm(\"Delete comment?\")) apiCall(\"DELETE\", \"/api/comments/{pid}/{tid}/{i}\")'>Delete Comment</button></div></div>"

    html += f"<hr><p><a href='/project/{pid}'>&larr; Back to Project {pid}</a></p>"
    html += "</div></body></html>"
    return html

def run_web_process():
    uvicorn.run(app, host="0.0.0.0", port=8000)

def run_web():
    p = multiprocessing.Process(target=run_web_process, daemon=True)
    p.start()
    return p
