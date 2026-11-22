"""HTML page routes and workspace file preview endpoints for the TodoX Kanban Web UI."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from orchestrator.storage import get_data_dir, load_task
from telegram_ui.file_renderers import is_binary_file, render_file_to_html, render_markdown_to_html

router = APIRouter()

_INLINE_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='16' fill='%234f46e5'/%3E"
    "%3Cpath d='M18 22h10v24H18zm18 0h10v14H36z' fill='%23ffffff'/%3E"
    "%3C/svg%3E"
)


def _render_app_shell(
    page_mode: str,
    title: str,
    pid: str = "",
    tid: str = "",
) -> str:
    """Render the self-contained offline HTML5 shell for Dashboard and Kanban Board."""
    esc_title = html.escape(title, quote=True)
    esc_mode = html.escape(page_mode, quote=True)
    esc_pid = html.escape(pid, quote=True)
    esc_tid = html.escape(tid, quote=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<script>
try {{
  var savedScheme = localStorage.getItem("color-scheme");
  if (savedScheme === "light" || savedScheme === "dark") {{
    document.querySelector('meta[name="color-scheme"]').content = savedScheme;
  }}
}} catch (e) {{}}
</script>
<title>{esc_title}</title>
<link rel="icon" href="{_INLINE_FAVICON}">
<link rel="stylesheet" href="/static/styles.css">
<script src="/static/app.js" defer></script>
</head>
<body data-page="{esc_mode}" data-pid="{esc_pid}" data-tid="{esc_tid}">
  <header class="top-bar" aria-label="TodoX Navigation">
    <div class="top-bar-left">
      <a href="/" class="brand">
        <span class="brand-mark" aria-hidden="true">TX</span>
        <span class="brand-name">TodoX</span>
      </a>
      <nav id="breadcrumbs" class="breadcrumbs" aria-label="Breadcrumb"></nav>
    </div>
    <div class="top-bar-right">
      <span id="sse-badge" class="status-chip chip-muted" title="Real-time SSE stream status">
        <span class="pulse-dot"></span>
        <span id="sse-label">Connecting</span>
      </span>
      <button type="button" id="watcher-toggle-btn" class="btn btn-sm btn-ghost" title="Pause or resume autonomous watcher">
        Watching
      </button>
      <button type="button" id="settings-btn" class="btn btn-sm btn-secondary">
        Settings
      </button>
    </div>
  </header>

  <main id="app-root" class="app-root">
    <div class="loading-shell">
      <p class="muted">Loading TodoX workspace...</p>
    </div>
  </main>

  <div id="toast-region" class="toast-region" aria-live="polite"></div>

  <!-- Slide-over Task Inspector Drawer -->
  <dialog id="task-drawer" class="drawer-dialog" closedby="any" aria-labelledby="drawer-title">
    <div id="task-drawer-body" class="drawer-shell"></div>
  </dialog>

  <!-- Create Project Modal -->
  <dialog id="create-project-dialog" class="modal-dialog" closedby="any" aria-labelledby="cp-title">
    <form id="create-project-form" class="modal-shell">
      <div class="modal-header">
        <h2 id="cp-title">New Project</h2>
        <button type="button" class="btn-icon" data-close-dialog="create-project-dialog" aria-label="Close">✕</button>
      </div>
      <div class="modal-body">
        <label class="field">
          <span>Project ID (e.g. ALPHA)</span>
          <input name="pid" required pattern="[A-Za-z0-9_-]+" maxlength="24" placeholder="ALPHA" autofocus>
        </label>
        <label class="field">
          <span>Project Title</span>
          <input name="title" required placeholder="Silver Mean Reversion Research">
        </label>
        <label class="field">
          <span>Project Goal &amp; Instructions</span>
          <textarea name="desc" rows="4" required placeholder="Describe goals, constraints, and acceptance criteria..."></textarea>
        </label>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-close-dialog="create-project-dialog">Cancel</button>
        <button type="submit" class="btn btn-primary">Create Project</button>
      </div>
    </form>
  </dialog>

  <!-- Create Ticket Modal -->
  <dialog id="create-task-dialog" class="modal-dialog" closedby="any" aria-labelledby="ct-title">
    <form id="create-task-form" class="modal-shell">
      <div class="modal-header">
        <h2 id="ct-title">New Ticket</h2>
        <button type="button" class="btn-icon" data-close-dialog="create-task-dialog" aria-label="Close">✕</button>
      </div>
      <div class="modal-body">
        <label class="field">
          <span>Title</span>
          <input name="title" required placeholder="Implement dark mode toggle" autofocus>
        </label>
        <label class="field">
          <span>Description</span>
          <textarea name="desc" rows="4" placeholder="Requirements, edge cases, verification plan..."></textarea>
        </label>
        <div class="field-row">
          <label class="field">
            <span>Initial Column</span>
            <select name="status">
              <option value="not_started" selected>Backlog</option>
              <option value="in_progress">In Progress</option>
              <option value="need_confirm">Needs Review</option>
              <option value="blocked">Blocked</option>
            </select>
          </label>
          <label class="field">
            <span>Assignee</span>
            <select name="assignee">
              <option value="AI">AI Worker</option>
              <option value="Admin" selected>Admin</option>
            </select>
          </label>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-close-dialog="create-task-dialog">Cancel</button>
        <button type="submit" class="btn btn-primary">Create Ticket</button>
      </div>
    </form>
  </dialog>

  <!-- Orchestrator & Appearance Settings Modal -->
  <dialog id="settings-dialog" class="modal-dialog" closedby="any" aria-labelledby="st-title">
    <form id="settings-form" class="modal-shell">
      <div class="modal-header">
        <h2 id="st-title">Orchestrator Settings</h2>
        <button type="button" class="btn-icon" data-close-dialog="settings-dialog" aria-label="Close">✕</button>
      </div>
      <div id="settings-body" class="modal-body"></div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-close-dialog="settings-dialog">Close</button>
        <button type="submit" class="btn btn-primary">Save Settings</button>
      </div>
    </form>
  </dialog>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
def read_root():
    """Serve the modern TodoX project overview and live stream hub."""
    return _render_app_shell("dashboard", "TodoX Orchestrator")


@router.get("/project/{pid}", response_class=HTMLResponse)
def read_project(pid: str):
    """Serve the Kanban board, live SSE stream, and file explorer for a project."""
    clean_pid = pid.strip().upper()
    proj_task = load_task(clean_pid, f"{clean_pid}-0")
    if not proj_task:
        raise HTTPException(status_code=404, detail="Project not found")
    return _render_app_shell("project", f"{clean_pid} — TodoX Kanban", pid=clean_pid)


@router.get("/task/{pid}/{tid}", response_class=HTMLResponse)
def read_task(pid: str, tid: str):
    """Serve the Kanban board with the target task drawer opened directly."""
    clean_pid = pid.strip().upper()
    clean_tid = tid.strip().upper()
    task = load_task(clean_pid, clean_tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _render_app_shell("task", f"{clean_tid} — TodoX Ticket", pid=clean_pid, tid=clean_tid)


@router.get("/project/{pid}/files/{path:path}")
def get_project_file(
    pid: str,
    path: str,
    preview: Optional[int] = Query(default=0),
):
    """Serve or offline-render a file inside DATA_DIR/projects/<PID> with traversal protection."""
    clean_pid = pid.strip().upper()
    project_dir = (Path(get_data_dir()) / "projects" / clean_pid).resolve()
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    if ".." in Path(path).parts:
        raise HTTPException(status_code=403, detail="Access denied")

    target = (project_dir / path).resolve()
    if not target.is_relative_to(project_dir):
        raise HTTPException(status_code=403, detail="Access denied")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = target.suffix.lower()
    if suffix in (".md", ".markdown"):
        raw_md = target.read_text(encoding="utf-8", errors="replace")
        rendered = render_markdown_to_html(
            title=target.name,
            rel_path=path,
            md_content=raw_md,
            pid=clean_pid,
        )
        return HTMLResponse(content=rendered)

    if preview and suffix not in (".html", ".htm") and not is_binary_file(target):
        rendered_bytes = render_file_to_html(target, rel_path=path, pid=clean_pid)
        if rendered_bytes is not None:
            return HTMLResponse(content=rendered_bytes.decode("utf-8"))

    return FileResponse(target)


if __name__ == "__main__":
    shell = _render_app_shell("project", "GO — TodoX", pid="GO")
    assert "/static/styles.css" in shell
    assert "/static/app.js" in shell
    assert "jsdelivr" not in shell and "cloudflare" not in shell
    assert "https://" not in shell
    assert 'closedby="any"' in shell
    print("web.views self-test passed!")
