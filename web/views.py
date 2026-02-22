import os
import markdown
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from orchestrator.storage import (
    load_projects, load_task, get_data_dir
)

router = APIRouter()

HTML_STYLE = """
<style>
body { font-family: sans-serif; margin: 0; padding: 20px; background: #f4f6f8; }
.container { max-width: 800px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.layout-container { display: flex; min-height: 90vh; max-width: 1400px; margin: 0 auto; gap: 20px; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.sidebar { width: 350px; border-right: 1px solid #eee; padding-right: 20px; overflow-y: auto; flex-shrink: 0; }
.main-pane { flex: 1; display: flex; flex-direction: column; overflow-y: auto; }
.iframe-container { width: 100%; height: 70vh; border: 1px solid #ccc; border-radius: 4px; background: #fff; }
.file-link { display: block; margin: 2px 0 2px 10px; padding: 4px 8px; background: #f8f9fa; border-radius: 4px; cursor: pointer; text-decoration: none; color: #333; border: 1px solid #eee; font-size: 13px; }
.file-link:hover { background: #e2e6ea; border-color: #dae0e5; }
.card { border: 1px solid #ddd; padding: 15px; margin-bottom: 15px; border-radius: 5px; background: #fafafa; display: flex; justify-content: space-between; align-items: center; }
.card-content { flex: 1; }
.card-actions { margin-left: 20px; }
input, select, button, textarea { margin: 5px 0; padding: 8px; border-radius: 4px; border: 1px solid #ccc; }
button { background: #333; color: white; cursor: pointer; border: none; }
button:hover { background: #555; }
.danger { background: #dc3545; }
.danger:hover { background: #c82333; }
hr { border: 0; border-top: 1px solid #eee; margin: 20px 0; }
a { color: #007bff; text-decoration: none; }
a:hover { text-decoration: underline; }
textarea { width: 100%; min-height: 80px; font-family: inherit; }
pre { background: #eee; padding: 10px; border-radius: 4px; white-space: pre-wrap; font-size: 13px; }

/* Tree view styles */
details { margin-left: 15px; }
summary { cursor: pointer; font-weight: 500; margin: 4px 0; font-size: 14px; user-select: none; }
summary:hover { color: #007bff; }
.tree-root { margin-top: 20px; border-top: 1px solid #ddd; padding-top: 15px; }

/* Tabs Styles */
.tabs-container { display: flex; border-bottom: 2px solid #ddd; margin-bottom: 20px; }
.tab-btn { background: none; border: none; padding: 10px 20px; font-size: 16px; cursor: pointer; color: #555; }
.tab-btn.active { color: #007bff; border-bottom: 2px solid #007bff; margin-bottom: -2px; font-weight: bold; }
.tab-btn:hover:not(.active) { color: #333; background: #f8f9fa; }
.tab-content { display: none; }
.tab-content.active { display: block; }
</style>
<script>
function switchTab(tabId) {
    // Hide all tab contents
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    // Deactivate all tab buttons
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    
    // Show selected tab content
    document.getElementById(tabId).classList.add('active');
    // Activate selected tab button
    document.getElementById('btn-' + tabId).classList.add('active');
}

function showFileViewer() {
    switchTab('tab-viewer');
}
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

def generate_file_tree(root_dir, pid, current_path=""):
    html = ""
    dir_path = os.path.join(root_dir, current_path) if current_path else root_dir
    
    if not os.path.exists(dir_path):
        return ""
        
    items = sorted(os.listdir(dir_path))
    dirs = [item for item in items if item != ".DS_Store" and os.path.isdir(os.path.join(dir_path, item))]
    files = [item for item in items if item != ".DS_Store" and os.path.isfile(os.path.join(dir_path, item))]
    
    for d in dirs:
        html += f"<details open><summary>&#128193; {d}</summary>"
        rel_path = os.path.join(current_path, d) if current_path else d
        html += generate_file_tree(root_dir, pid, rel_path)
        html += "</details>"
        
    for f in files:
        rel_path = os.path.join(current_path, f) if current_path else f
        url = f"/project/{pid}/files/{rel_path}"
        html += f"<a href='{url}' target='file_viewer' class='file-link' onclick='showFileViewer()'>&#128196; {f}</a>"
        
    return html

@router.get("/", response_class=HTMLResponse)
def read_root():
    projects = load_projects()
    html = f"<html><head><title>TodoX Panel</title>{HTML_STYLE}</head><body><div class='container'>"
    
    # Navigation Breadcrumb
    html += f"<p style='font-size:14px; margin-bottom: 20px;'>Home</p>"
    
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

@router.get("/project/{pid}", response_class=HTMLResponse)
def read_project(pid: str):
    pid = pid.upper()
    proj_task = load_task(pid, f"{pid}-0")
    if not proj_task:
        raise HTTPException(status_code=404, detail="Project not found")
        
    html = f"<html><head><title>Project {pid}</title>{HTML_STYLE}</head><body>"
    html += "<div class='layout-container'><div class='sidebar'>"
    
    # Navigation Breadcrumb
    html += f"<p style='font-size:14px; margin-bottom: 20px;'><a href='/'>Home</a> / {pid}</p>"
    
    # LEFT PANE: Project Info and File Tree
    html += f"<h2>Project: {pid}</h2>"
    html += f"<p><b>Title:</b> {proj_task.title}</p>"
    html += f"<p><b>Status:</b> <span style='background:#007bff;color:white;padding:2px 6px;border-radius:3px;font-size:12px;'>{proj_task.status}</span></p>"
    html += f"<p><b>Assignee:</b> {proj_task.assignee}</p>"
    html += f"<details><summary style='font-size:14px;color:#555;'><b>Description</b></summary><p style='font-size:14px;color:#555;margin-left:15px;'>{proj_task.descriptions}</p></details>"
    
    html += "<div class='tree-root'>"
    html += f"<h3>&#128193; {pid}</h3>"
    
    project_dir = os.path.join(get_data_dir(), "projects", pid)
    
    if os.path.exists(project_dir):
        html += generate_file_tree(project_dir, pid)
    else:
        html += f"<p style='font-size:12px;color:#999;'>No project folder found.</p>"
    html += "</div>" # close tree-root
    
    
    html += "</div>" # close sidebar
    
    # RIGHT PANE: Details and Subtasks + File Viewer
    html += "<div class='main-pane'>"
    
    # Tabs Header
    html += "<div class='tabs-container'>"
    html += "<button id='btn-tab-info' class='tab-btn active' onclick=\"switchTab('tab-info')\">Project Info & Tasks</button>"
    html += "<button id='btn-tab-viewer' class='tab-btn' onclick=\"switchTab('tab-viewer')\">File Viewer</button>"
    html += "</div>"
    
    # Tab Content: Project Info
    html += "<div id='tab-info' class='tab-content active'>"
    
    # Edit details block
    html += "<details open><summary><h3 style='display:inline;'>Edit Project Details</h3></summary>"
    html += "<div style='margin-left:15px; max-width:600px;'>"
    html += f"<input id='edit_title' value='{proj_task.title}' placeholder='Title' style='width:100%; box-sizing:border-box;'><br>"
    html += f"<textarea id='edit_desc' placeholder='Description' style='width:100%; box-sizing:border-box;'>{proj_task.descriptions}</textarea><br>"
    html += f"<select id='status' style='width:48%;'> \
        <option value='in_progress' {'selected' if proj_task.status=='in_progress' else ''}>in_progress</option> \
        <option value='complete' {'selected' if proj_task.status=='complete' else ''}>complete</option> \
        <option value='failed' {'selected' if proj_task.status=='failed' else ''}>failed</option> \
        </select> "
    html += f"<select id='edit_assignee' style='width:48%; float:right;'> \
        <option value='Admin' {'selected' if proj_task.assignee=='Admin' else ''}>Admin</option> \
        <option value='AI' {'selected' if proj_task.assignee=='AI' else ''}>AI</option> \
        </select><br>"
    html += f"<button style='width:100%; margin-top:10px;' onclick='apiCall(\"PUT\", \"/api/projects/{pid}\", {{title: document.getElementById(\"edit_title\").value, descriptions: document.getElementById(\"edit_desc\").value, status: document.getElementById(\"status\").value, assignee: document.getElementById(\"edit_assignee\").value}})'>Update Details</button>"
    html += "</div></details>" # close Edit details
    
    html += "<hr>"

    # Tasks block
    html += "<details open><summary><h3 style='display:inline;'>Create Task</h3></summary>"
    html += "<div style='margin-left:15px; max-width:600px;'>"
    html += "<input id='title' placeholder='Task Title' style='width:100%; box-sizing:border-box;'><br>"
    html += "<textarea id='desc' placeholder='Description' style='width:100%; box-sizing:border-box; min-height:40px;'></textarea><br>"
    html += "<div style='display:flex; gap:10px;'>"
    html += "<select id='assignee' style='flex:1;'><option value='Admin'>Admin</option><option value='AI'>AI</option></select>"
    html += f"<button style='flex:1;' onclick='apiCall(\"POST\", \"/api/tasks/{pid}/{pid}-0\", {{title: document.getElementById(\"title\").value, desc: document.getElementById(\"desc\").value, assignee: document.getElementById(\"assignee\").value}})'>Create Task</button>"
    html += "</div></div></details>" # close create task block

    html += "<hr>"

    # Subtasks list block
    html += "<details open><summary><h3 style='display:inline;'>Task List</h3></summary>"
    html += "<div style='margin-left:15px;'>"
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        task_ids = os.listdir(tasks_dir)
        for tid in sorted(task_ids):
            task = load_task(pid, tid)
            if task:
                html += f"<div class='card' style='padding:10px; margin-bottom:10px;'><div class='card-content'><b><a href='/task/{pid}/{tid}'>{task.id}</a></b>: {task.title} &nbsp; <span style='font-size:11px;background:#eee;padding:2px 4px;border-radius:3px;'>{task.status}</span> <span style='font-size:11px;color:#777;'>({task.assignee})</span></div>"
                html += f"<div class='card-actions'><button class='danger' style='padding:4px 8px; font-size:11px;' onclick='if(confirm(\"Delete {tid}?\")) apiCall(\"DELETE\", \"/api/tasks/{pid}/{tid}\")'>Delete</button></div></div>"
    html += "</div></details>" # close Task List block
    
    html += "</div>" # close tab-info

    # Tab Content: File Viewer
    html += "<div id='tab-viewer' class='tab-content'>"
    html += "<div style='display:flex; justify-content:space-between; align-items:center;'><h3>File Viewer</h3><button onclick=\"switchTab('tab-info')\" style='padding:4px 8px; font-size:12px;'>Close Viewer</button></div>"
    html += "<iframe name='file_viewer' class='iframe-container' src='about:blank'></iframe>"
    html += "</div>" # close tab-viewer

    html += "</div>" # close main-pane
    
    html += "</div></body></html>"
    return html

@router.get("/project/{pid}/files/{path:path}")
def get_project_file(pid: str, path: str):
    pid = pid.upper()
    project_dir = os.path.join(get_data_dir(), "projects", pid)
    file_path = os.path.join(project_dir, path)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
        
    if file_path.endswith(".md"):
        with open(file_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        html_content = markdown.markdown(md_content, extensions=['fenced_code', 'tables'])
        # Add a bit of styling for the rendered markdown so it looks nice in the iframe
        full_html = f"""
        <html><head><style>
            body {{ font-family: sans-serif; padding: 20px; line-height: 1.6; color: #333; }}
            pre {{ background: #f4f6f8; padding: 10px; border-radius: 4px; overflow-x: auto; }}
            code {{ background: #f4f6f8; padding: 2px 4px; border-radius: 3px; font-family: monospace; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f8f9fa; }}
            blockquote {{ border-left: 4px solid #ccc; margin-left: 0; padding-left: 16px; color: #666; }}
        </style></head><body>
        {html_content}
        </body></html>
        """
        return HTMLResponse(content=full_html)
        
    return FileResponse(file_path)

@router.get("/task/{pid}/{tid}", response_class=HTMLResponse)
def read_task(pid: str, tid: str):
    pid = pid.upper()
    tid = tid.upper()
    task = load_task(pid, tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    html = f"<html><head><title>Task {tid}</title>{HTML_STYLE}</head><body><div class='container'>"
    
    # Navigation Breadcrumb
    html += f"<p style='font-size:14px; margin-bottom: 20px;'><a href='/'>Home</a> / <a href='/project/{pid}'>{pid}</a> / {tid}</p>"
    
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

    html += "</div></body></html>"
    return html
