import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import multiprocessing
import uvicorn

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.storage import load_projects, load_task, get_data_dir

app = FastAPI(title="TodoX Web Panel")

@app.get("/", response_class=HTMLResponse)
def read_root():
    projects = load_projects()
    
    html = "<html><head><title>TodoX Panel</title><style>body { font-family: sans-serif; max-width: 800px; margin: 40px auto; }</style></head><body>"
    html += "<h1>TodoX Project Dashboard</h1>"
    if not projects:
        html += "<p>No projects found.</p>"
    else:
        html += "<ul>"
        for pid, proj in projects.items():
            html += f"<li><a href='/project/{pid}'>{pid} - {proj.title}</a> [{proj.status}] - Assignee: {proj.assignee}</li>"
        html += "</ul>"
    html += "</body></html>"
    return html

@app.get("/project/{pid}", response_class=HTMLResponse)
def read_project(pid: str):
    pid = pid.upper()
    proj_task = load_task(pid, f"{pid}-0")
    if not proj_task:
        raise HTTPException(status_code=404, detail="Project not found")
        
    html = f"<html><head><title>Project {pid}</title><style>body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; }}</style></head><body>"
    html += f"<h1>Project: {pid}</h1>"
    html += f"<p><b>Title:</b> {proj_task.title}</p>"
    html += f"<p><b>Status:</b> {proj_task.status}</p>"
    html += f"<p><b>Assignee:</b> {proj_task.assignee}</p>"
    html += f"<p><b>Description:</b> {proj_task.descriptions}</p>"
    html += "<hr>"
    html += "<h2>Subtasks</h2>"
    
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        task_ids = os.listdir(tasks_dir)
        if task_ids:
            html += "<ul>"
            for tid in task_ids:
                task = load_task(pid, tid)
                if task:
                    html += f"<li><a href='/task/{pid}/{tid}'><b>{task.id}</b></a>: {task.title} [{task.status}] - Assignee: {task.assignee}</li>"
            html += "</ul>"
        else:
            html += "<p>No subtasks.</p>"
    else:
        html += "<p>No subtasks.</p>"
        
    html += "<p><a href='/'>Back to Dashboard</a></p>"
    html += "</body></html>"
    return html

@app.get("/task/{pid}/{tid}", response_class=HTMLResponse)
def read_task(pid: str, tid: str):
    pid = pid.upper()
    tid = tid.upper()
    task = load_task(pid, tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    html = f"<html><head><title>Task {tid}</title><style>body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; }} .comment {{ border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; border-radius: 5px; background: #f9f9f9; }}</style></head><body>"
    html += f"<h1>Task: {tid}</h1>"
    html += f"<p><b>Project ID:</b> <a href='/project/{pid}'>{pid}</a></p>"
    html += f"<p><b>Title:</b> {task.title}</p>"
    html += f"<p><b>Status:</b> {task.status}</p>"
    html += f"<p><b>Assignee:</b> {task.assignee}</p>"
    html += f"<p><b>Description:</b> {task.descriptions}</p>"
    html += "<hr>"
    html += "<h2>Comments & Execution History</h2>"
    
    if not task.comments:
        html += "<p>No comments or history recorded.</p>"
    else:
        for c in task.comments:
            html += f"<div class='comment'><p><b>{c.author}</b> ({c.time}):</p><pre>{c.content}</pre></div>"

    html += f"<p><a href='/project/{pid}'>Back to Project</a></p>"
    html += "</body></html>"
    return html

def run_web_process():
    uvicorn.run(app, host="0.0.0.0", port=8000)

def run_web():
    # Use multiprocessing to avoid blocking/asyncio loop conflicts with telegram bot
    p = multiprocessing.Process(target=run_web_process, daemon=True)
    p.start()
    return p
