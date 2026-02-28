import os
import json
from datetime import datetime
from .storage import load_task, get_data_dir

def generate_file_tree_data(root_dir, current_path=""):
    tree = []
    dir_path = os.path.join(root_dir, current_path) if current_path else root_dir
    
    if not os.path.exists(dir_path):
        return tree
        
    items = sorted(os.listdir(dir_path))
    dirs = [item for item in items if item != ".DS_Store" and os.path.isdir(os.path.join(dir_path, item))]
    files = [item for item in items if item != ".DS_Store" and os.path.isfile(os.path.join(dir_path, item))]
    
    for d in dirs:
        rel_path = os.path.join(current_path, d) if current_path else d
        tree.append({
            "name": d,
            "type": "directory",
            "path": rel_path,
            "children": generate_file_tree_data(root_dir, rel_path)
        })
        
    for f in files:
        rel_path = os.path.join(current_path, f) if current_path else f
        tree.append({
            "name": f,
            "type": "file",
            "path": rel_path
        })
        
    return tree

def generate_project_html(pid: str) -> bytes:
    pid = pid.upper()
    proj_task = load_task(pid, f"{pid}-0")
    if not proj_task:
        return None

    # Load all tasks
    tasks = [proj_task.model_dump()]
    
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        for tid in os.listdir(tasks_dir):
            task = load_task(pid, tid)
            if task:
                tasks.append(task.model_dump())

    # Build file tree
    project_dir = os.path.join(get_data_dir(), "projects", pid)
    file_tree = generate_file_tree_data(project_dir)

    # Prepare JSON data payload
    app_data = {
        "pid": pid,
        "project": proj_task.model_dump(),
        "tasks": tasks,
        "files": file_tree
    }
    
    json_data = json.dumps(app_data, ensure_ascii=False)
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Project {pid} ({gen_time})</title>
    <style>
        :root {{
            --primary: #4F46E5;
            --primary-light: #EEF2FF;
            --secondary: #6B7280;
            --bg: #F9FAFB;
            --bg-card: #FFFFFF;
            --text-main: #111827;
            --border: #E5E7EB;
            --danger: #EF4444;
            --success: #10B981;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg);
            color: var(--text-main);
            font-size: 16px;
            line-height: 1.5;
            padding-bottom: 80px; /* Space for operations bar */
            -webkit-tap-highlight-color: transparent;
        }}
        
        /* Layout components */
        header {{
            background: var(--bg-card);
            padding: 16px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            border-bottom: 1px solid var(--border);
        }}
        .header-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
        .header-title {{ font-size: 1.25rem; font-weight: 700; }}
        .badge {{
            padding: 4px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600;
            background: var(--primary-light); color: var(--primary);
        }}
        .badge.success {{ background: #D1FAE5; color: #065F46; }}
        .badge.danger {{ background: #FEE2E2; color: #991B1B; }}
        
        .tabs {{
            display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px;
            -ms-overflow-style: none; scrollbar-width: none;
        }}
        .tabs::-webkit-scrollbar {{ display: none; }}
        .tab {{
            padding: 8px 16px; border-radius: 8px; font-size: 0.875rem; font-weight: 500;
            color: var(--secondary); background: transparent; border: none; cursor: pointer;
            white-space: nowrap; transition: 0.2s ease;
        }}
        .tab.active {{ background: var(--border); color: var(--text-main); }}
        
        .tab-pane {{ display: none; padding: 16px; animation: fadeIn 0.3s ease; }}
        .tab-pane.active {{ display: block; }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}

        /* Cards & Forms */
        .card {{
            background: var(--bg-card); border-radius: 12px; padding: 16px;
            margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); border: 1px solid var(--border);
        }}
        .card-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }}
        .card-title {{ font-weight: 600; font-size: 1rem; }}
        
        .form-group {{ margin-bottom: 16px; }}
        .form-label {{ display: block; font-size: 0.875rem; font-weight: 500; color: var(--secondary); margin-bottom: 4px; }}
        .form-control {{
            width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
            font-size: 1rem; background: var(--bg); transition: border-color 0.2s;
        }}
        .form-control:focus {{ outline: none; border-color: var(--primary); background: var(--bg-card); }}
        textarea.form-control {{ resize: vertical; min-height: 80px; }}
        
        /* Buttons */
        .btn {{
            display: inline-flex; align-items: center; justify-content: center;
            padding: 10px 16px; border-radius: 8px; font-weight: 600; font-size: 0.875rem;
            cursor: pointer; border: none; transition: 0.2s ease; width: 100%;
        }}
        .btn-primary {{ background: var(--primary); color: white; }}
        .btn-primary:active {{ transform: scale(0.98); background: #4338CA; }}
        .btn-outline {{ background: transparent; border: 1px solid var(--border); color: var(--text-main); }}
        .btn-outline:active {{ background: var(--border); }}
        
        .btn-group {{ display: flex; gap: 8px; }}
        .btn-group .btn {{ flex: 1; }}

        /* Accordion (Details) */
        details {{ border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; background: var(--bg-card); overflow: hidden; }}
        summary {{
            padding: 12px 16px; font-weight: 500; cursor: pointer; list-style: none;
            display: flex; justify-content: space-between; align-items: center; user-select: none;
        }}
        summary::-webkit-details-marker {{ display: none; }}
        summary::after {{ content: '+'; font-size: 1.25rem; color: var(--secondary); }}
        details[open] summary::after {{ content: '-'; }}
        .details-content {{ padding: 16px; border-top: 1px solid var(--border); background: var(--bg); }}

        /* Tree View */
        .tree-node {{ margin-left: 16px; border-left: 1px solid var(--border); padding-left: 12px; margin-top: 8px; }}
        .tree-item {{ padding: 8px 0; display: flex; align-items: center; gap: 8px; font-size: 0.875rem; }}
        .tree-icon {{ font-size: 1.1rem; }}

        /* Floating Operations Block */
        .ops-panel {{
            position: fixed; bottom: 0; left: 0; right: 0;
            background: var(--bg-card); border-top: 1px solid var(--border);
            padding: 12px 16px; box-shadow: 0 -4px 6px -1px rgba(0,0,0,0.05);
            z-index: 200; transform: translateY(100%); transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        .ops-panel.show {{ transform: translateY(0); }}
        .ops-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
        .ops-textarea {{
            width: 100%; height: 100px; font-family: monospace; font-size: 0.8rem;
            padding: 8px; border: 1px solid var(--border); border-radius: 6px;
            background: #111827; color: #10B981; resize: none; margin-bottom: 8px;
        }}
        
        .fab {{
            position: fixed; bottom: 20px; right: 20px;
            width: 56px; height: 56px; border-radius: 28px;
            background: var(--primary); color: white;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1); border: none;
            font-size: 24px; cursor: pointer; z-index: 150; transition: transform 0.2s;
        }}
        .fab:active {{ transform: scale(0.9); }}
        .fab-badge {{
            position: absolute; top: 0; right: 0;
            background: var(--danger); color: white;
            font-size: 10px; font-weight: bold; width: 20px; height: 20px;
            border-radius: 10px; display: flex; justify-content: center; align-items: center;
            opacity: 0; transition: 0.2s;
        }}
        .fab-badge.show {{ opacity: 1; }}

        .comment-item {{ margin-bottom: 12px; padding: 12px; background: var(--bg-card); border-radius: 8px; border: 1px solid var(--border); }}
        .comment-meta {{ font-size: 0.75rem; color: var(--secondary); margin-bottom: 4px; display: flex; justify-content: space-between; }}
        .comment-meta strong {{ color: var(--text-main); }}
        
        .helper-text {{ font-size: 0.8rem; color: var(--secondary); margin-top: 4px; display: block; }}
    </style>
</head>
<body>

    <header>
        <div class="header-top">
            <div class="header-title" id="h-title">Loading...</div>
            <div class="badge" id="h-status">State</div>
        </div>
        <div class="tabs">
            <button class="tab active" onclick="switchTab('info')">Project Info</button>
            <button class="tab" onclick="switchTab('tasks')">Tasks</button>
            <button class="tab" onclick="switchTab('files')">Files</button>
            <button class="tab" onclick="switchTab('ops')">📋 Operations <span id="tab-ops-badge" class="badge" style="display:none; margin-left:4px;">0</span></button>
        </div>
    </header>

    <!-- Project Info Tab -->
    <main id="tab-info" class="tab-pane active">
        <div class="card">
            <h3 class="card-title" style="margin-bottom:16px;">Edit Project</h3>
            <div class="form-group">
                <label class="form-label">Title</label>
                <input type="text" class="form-control" id="p-title" onchange="queueUpdate('project', 'title', this.value)">
            </div>
            <div class="form-group">
                <label class="form-label">Description</label>
                <textarea class="form-control" id="p-desc" onchange="queueUpdate('project', 'des', this.value)"></textarea>
            </div>
            <div style="display:flex; gap:12px;">
                <div class="form-group" style="flex:1;">
                    <label class="form-label">Status</label>
                    <select class="form-control" id="p-status" onchange="queueUpdate('project', 'status', this.value)">
                        <option value="in_progress">In Progress</option>
                        <option value="complete">Complete</option>
                        <option value="failed">Failed</option>
                    </select>
                </div>
                <div class="form-group" style="flex:1;">
                    <label class="form-label">Assignee</label>
                    <select class="form-control" id="p-assignee" onchange="queueUpdate('project', 'assignee', this.value)">
                        <option value="Admin">Admin</option>
                        <option value="AI">AI</option>
                    </select>
                </div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:16px; margin-bottom:8px;">
                <h4 style="font-size:0.875rem;">Comments</h4>
                <button class="btn btn-outline" style="width:auto; padding:4px 8px; font-size:0.7rem;" onclick="addCommentModal('{pid}-0')">+ Add Comment</button>
            </div>
            <div id="p-comments-container"></div>
        </div>
    </main>

    <!-- Tasks Tab -->
    <main id="tab-tasks" class="tab-pane">
        <div style="margin-bottom: 16px;">
            <button class="btn btn-primary" onclick="newTaskModal()">+ Create New Task</button>
        </div>
        <div id="tasks-container"></div>
    </main>

    <!-- Files Tab -->
    <main id="tab-files" class="tab-pane">
        <div class="card">
            <h3 class="card-title" style="margin-bottom:12px;">Project Files</h3>
            <span class="helper-text" style="margin-bottom:16px;">Copy relative paths to use with <code>/get-file</code>.</span>
            <div id="files-container"></div>
        </div>
    </main>

    <!-- Operations Tab -->
    <main id="tab-ops" class="tab-pane">
        <div class="card">
            <h3 class="card-title" style="margin-bottom:12px;">Operations Queue</h3>
            <span class="helper-text" style="margin-bottom:16px;">These commands have been generated by your actions. They are copied to your clipboard automatically on each change. Connect to Telegram and paste them to apply.</span>
            <textarea class="ops-textarea" id="ops-text" readonly style="min-height: 200px;"></textarea>
            <div class="btn-group" style="margin-top: 12px;">
                <button class="btn btn-primary" onclick="copyOps(true)">Copy to Clipboard</button>
                <button class="btn btn-outline" style="color:var(--danger); border-color:var(--border);" onclick="clearOps()">Clear Queue</button>
            </div>
        </div>
    </main>

    <!-- New Task Modal (Inline for simplicity) -->
    <div id="new-task-form" class="card" style="display:none; margin: 16px; border-color: var(--primary);">
        <h3 class="card-title" style="margin-bottom:12px; color: var(--primary);">Create Task</h3>
        <div class="form-group">
            <label class="form-label">Parent ID</label>
            <input type="text" class="form-control" id="nt-ptid" value="{pid}-0">
        </div>
        <div class="form-group">
            <label class="form-label">Title</label>
            <input type="text" class="form-control" id="nt-title" placeholder="e.g. Implement Auth">
        </div>
        <div class="form-group">
            <label class="form-label">Description</label>
            <textarea class="form-control" id="nt-desc"></textarea>
        </div>
        <div class="btn-group">
            <button class="btn btn-primary" onclick="generateNewTask()">Generate Command</button>
            <button class="btn btn-outline" onclick="document.getElementById('new-task-form').style.display='none'">Cancel</button>
        </div>
    </div>
    
    <!-- New Comment Form -->
    <div id="new-comment-form" class="card" style="display:none; margin: 16px; border-color: var(--primary);">
        <h3 class="card-title" style="margin-bottom:12px;">Add Comment to <span id="nc-tid" style="font-family:monospace;"></span></h3>
        <div class="form-group">
            <textarea class="form-control" id="nc-msg" placeholder="Your message..."></textarea>
        </div>
        <div class="btn-group">
            <button class="btn btn-primary" onclick="generateNewComment()">Generate</button>
            <button class="btn btn-outline" onclick="document.getElementById('new-comment-form').style.display='none'">Cancel</button>
        </div>
    </div>

    <!-- Hidden Textarea for auto-copy -->
    <textarea id="hidden-copy" style="position:absolute; left:-9999px;"></textarea>

    <!-- DATA INJECTION -->
    <script>
        const appState = {json_data};
        const pid = appState.pid;
        let commandQueue = [];

        function init() {{
            renderProjectInfo();
            renderTasks();
            renderFiles();
        }}

        function switchTab(tabId) {{
            document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            let tabs = document.querySelectorAll('.tab');
            if(tabId === 'info') tabs[0].classList.add('active');
            if(tabId === 'tasks') tabs[1].classList.add('active');
            if(tabId === 'files') tabs[2].classList.add('active');
            window.scrollTo(0,0);
        }}

        function renderProjectInfo() {{
            const p = appState.project;
            document.getElementById('h-title').innerText = p.id + " ({gen_time})";
            document.getElementById('h-status').innerText = p.status;
            
            // Set initial forms
            document.getElementById('p-title').value = p.title || '';
            document.getElementById('p-desc').value = p.descriptions || '';
            document.getElementById('p-status').value = p.status || 'in_progress';
            document.getElementById('p-assignee').value = p.assignee || 'Admin';

            // Render project comments
            const pCommentsContainer = document.getElementById('p-comments-container');
            if (p.comments && p.comments.length > 0) {{
                pCommentsContainer.innerHTML = p.comments.map((c, idx) => `
                    <div class="comment-item">
                        <div class="comment-meta">
                            <strong>${{c.author}}</strong>
                            <span>${{c.time.split('T')[0]}}</span>
                        </div>
                        <div style="font-size:0.875rem; white-space:pre-wrap; margin-bottom:8px;">${{c.content}}</div>
                        <button class="btn btn-outline" style="color:var(--danger); border-color:var(--danger); width:auto; padding:2px 8px; font-size:0.7rem;" onclick="queueDeleteComment('${{pid}}-0', ${{idx}})">Delete</button>
                    </div>
                `).join('');
            }} else {{
                pCommentsContainer.innerHTML = '<span style="font-size:0.8rem; color:var(--secondary);">No comments yet.</span>';
            }}
        }}

        function renderTasks() {{
            const container = document.getElementById('tasks-container');
            container.innerHTML = '';
            
            appState.tasks.forEach(t => {{
                if (t.id === pid + "-0") return; // Skip root project task from the list here
                
                const det = document.createElement('details');
                
                // Status mapping color
                let bg = t.status === 'in_progress' ? '#E0E7FF' : (t.status === 'complete' ? '#D1FAE5' : '#F3F4F6');
                let fg = t.status === 'in_progress' ? '#3730A3' : (t.status === 'complete' ? '#065F46' : '#4B5563');

                det.innerHTML = `
                    <summary>
                        <div style="display:flex; flex-direction:column; gap:4px;">
                            <span style="font-weight:600;">${{t.id}}: ${{t.title}}</span>
                            <div style="display:flex; gap:8px;">
                                <span class="badge" style="background:${{bg}}; color:${{fg}}">${{t.status}}</span>
                                <span style="font-size:0.75rem; color:var(--secondary)">Assignee: ${{t.assignee}}</span>
                            </div>
                        </div>
                    </summary>
                    <div class="details-content">
                        <div class="form-group">
                            <label class="form-label">Title</label>
                            <input type="text" class="form-control" value="${{t.title || ''}}" onchange="queueUpdate('task', 'title', this.value, '${{t.id}}')">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Description</label>
                            <textarea class="form-control" onchange="queueUpdate('task', 'des', this.value, '${{t.id}}')">${{t.descriptions || ''}}</textarea>
                        </div>
                        <div style="display:flex; gap:12px; margin-bottom: 16px;">
                            <div style="flex:1;">
                                <label class="form-label">Status</label>
                                <select class="form-control" onchange="queueUpdate('task', 'status', this.value, '${{t.id}}')">
                                    <option value="not_started" ${{t.status==='not_started'?'selected':''}}>Not Started</option>
                                    <option value="in_progress" ${{t.status==='in_progress'?'selected':''}}>In Progress</option>
                                    <option value="complete" ${{t.status==='complete'?'selected':''}}>Complete</option>
                                    <option value="failed" ${{t.status==='failed'?'selected':''}}>Failed</option>
                                </select>
                            </div>
                            <div style="flex:1;">
                                <label class="form-label">Assignee</label>
                                <select class="form-control" onchange="queueUpdate('task', 'assignee', this.value, '${{t.id}}')">
                                    <option value="Admin" ${{t.assignee==='Admin'?'selected':''}}>Admin</option>
                                    <option value="AI" ${{t.assignee==='AI'?'selected':''}}>AI</option>
                                </select>
                            </div>
                        </div>
                        <div style="margin-bottom: 16px;">
                            <h4 style="font-size:0.875rem; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
                                Comments 
                                <button class="btn btn-outline" style="width:auto; padding:4px 8px; font-size:0.7rem;" onclick="addCommentModal('${{t.id}}')">+ Add</button>
                            </h4>
                            ${{ t.comments && t.comments.length > 0 ? t.comments.map((c, idx) => `
                                <div class="comment-item">
                                    <div class="comment-meta">
                                        <strong>${{c.author}}</strong>
                                        <span>${{c.time.split('T')[0]}}</span>
                                    </div>
                                    <div style="font-size:0.875rem; white-space:pre-wrap; margin-bottom:8px;">${{c.content}}</div>
                                    <button class="btn btn-outline" style="color:var(--danger); border-color:var(--danger); width:auto; padding:2px 8px; font-size:0.7rem;" onclick="queueDeleteComment('${{t.id}}', ${{idx}})">Delete</button>
                                </div>
                            `).join('') : '<span style="font-size:0.8rem; color:var(--secondary);">No comments yet.</span>' }}
                        </div>
                        <div class="btn-group">
                            <button class="btn btn-outline" style="color:var(--danger); border-color:var(--danger);" onclick="queueDeleteTask('${{t.id}}')">Delete Task</button>
                        </div>
                    </div>
                `;
                container.appendChild(det);
            }});
        }}

        function buildTreeHTML(nodes) {{
            if (!nodes || nodes.length === 0) return '';
            let html = '<div class="tree-node">';
            nodes.forEach(n => {{
                if (n.type === 'directory') {{
                    html += `
                        <details open style="border:none; margin:0; padding:0; background:transparent;">
                            <summary style="padding:4px 0;">
                                <div class="tree-item"><span class="tree-icon">📁</span> <span style="font-weight:600;">${{n.name}}</span></div>
                            </summary>
                            ${{buildTreeHTML(n.children)}}
                        </details>
                    `;
                }} else {{
                    html += `
                        <div class="tree-item" style="padding:4px 0; display:flex; justify-content:space-between; align-items:flex-start;">
                            <div style="display:flex; align-items:center; gap:8px;">
                                <span class="tree-icon">📄</span> 
                                <div style="display:flex; flex-direction:column;">
                                    <span>${{n.name}}</span>
                                    <span style="font-size:0.7rem; color:var(--secondary); font-family:monospace;">${{n.path}}</span>
                                </div>
                            </div>
                            <button class="btn btn-outline" style="width:auto; padding:2px 8px; font-size:0.7rem; color:var(--primary); border-color:var(--primary);" onclick="copyCmdText('/get-file -pid ${{pid}} -path &quot;${{n.path}}&quot;')">cmd</button>
                        </div>
                    `;
                }}
            }});
            html += '</div>';
            return html;
        }}

        function renderFiles() {{
            const container = document.getElementById('files-container');
            if (appState.files.length === 0) {{
                container.innerHTML = '<span style="font-size:0.875rem; color:var(--secondary);">No files found.</span>';
                return;
            }}
            container.innerHTML = buildTreeHTML(appState.files);
        }}

        /* ----- Queue Logic ----- */
        function escapeString(str) {{
            return str.replace(/"/g, '\\"');
        }}

        function pushCommand(cmd) {{
            commandQueue.push(cmd);
            updateBadge();
            renderQueue();
            copyOps(false); // quietly auto-copy
        }}

        function queueUpdate(type, field, value, tid=null) {{
            let strVal = typeof value === 'string' ? `"${{escapeString(value)}}"` : value;
            if (type === 'project') {{
                pushCommand(`/update-project -pid ${{pid}} -field ${{field}} -val ${{strVal}}`);
            }} else if (type === 'task') {{
                pushCommand(`/update-task -pid ${{pid}} -tid ${{tid}} -field ${{field}} -val ${{strVal}}`);
            }}
        }}

        function queueDeleteTask(tid) {{
            if(confirm('Queue delete task? (Requires applying in Telegram)')) {{
                pushCommand(`/del-task -pid ${{pid}} -tid ${{tid}}`);
            }}
        }}

        function queueDeleteComment(tid, cidx) {{
            if(confirm('Queue delete comment?')) {{
                pushCommand(`/del-comment -pid ${{pid}} -tid ${{tid}} -cidx ${{cidx}}`);
                
                // Optimistically update UI
                if(tid === `${{pid}}-0`) {{
                    appState.project.comments.splice(cidx, 1);
                    renderProjectInfo();
                }} else {{
                    let targetTask = appState.tasks.find(t => t.id === tid);
                    if (targetTask) {{
                        targetTask.comments.splice(cidx, 1);
                        renderTasks();
                    }}
                }}
            }}
        }}

        function newTaskModal() {{
            document.getElementById('new-task-form').style.display = 'block';
            window.scrollTo(0, document.body.scrollHeight);
        }}
        
        function generateNewTask() {{
            const ptid = document.getElementById('nt-ptid').value;
            const title = document.getElementById('nt-title').value;
            const desc = document.getElementById('nt-desc').value;
            pushCommand(`/new-task -pid ${{pid}} -ptid ${{ptid}} -title "${{escapeString(title)}}" -des "${{escapeString(desc)}}"`);
            
            // Optimistically update UI
            appState.tasks.push({{
                id: `${{pid}}-NEW`,
                title: title,
                descriptions: desc,
                status: 'not_started',
                assignee: 'Admin',
                comments: []
            }});
            renderTasks();
            
            document.getElementById('new-task-form').style.display = 'none';
        }}

        function addCommentModal(tid) {{
            document.getElementById('nc-tid').innerText = tid;
            document.getElementById('new-comment-form').style.display = 'block';
            document.getElementById('nc-msg').value = '';
            window.scrollTo(0, document.body.scrollHeight);
        }}

        function generateNewComment() {{
            const tid = document.getElementById('nc-tid').innerText;
            const msg = document.getElementById('nc-msg').value;
            pushCommand(`/new-comment -pid ${{pid}} -tid ${{tid}} -msg "${{escapeString(msg)}}"`);
            
            // Optimistically update UI
            if (tid === `${{pid}}-0`) {{
                if (!appState.project.comments) appState.project.comments = [];
                appState.project.comments.push({{
                    author: "Admin",
                    time: new Date().toISOString(),
                    content: msg
                }});
                renderProjectInfo();
            }} else {{
                let targetTask = appState.tasks.find(t => t.id === tid);
                if (targetTask) {{
                    if (!targetTask.comments) targetTask.comments = [];
                    targetTask.comments.push({{
                        author: "Admin",
                        time: new Date().toISOString(),
                        content: msg
                    }});
                    // We only re-render the task container. Because details elements will collapse on re-render,
                    // in a real app we'd target just the comment node. For now, doing full re-render is okay, 
                    // but we ensure the current scroll position is roughly maintained.
                    renderTasks();
                }}
            }}
            
            document.getElementById('new-comment-form').style.display = 'none';
        }}

        /* ----- Operations Panel Overlay UI ----- */
        function updateBadge() {{
            const badge = document.getElementById('tab-ops-badge');
            if (commandQueue.length > 0) {{
                badge.innerText = commandQueue.length;
                badge.style.display = 'inline-block';
            }} else {{
                badge.style.display = 'none';
            }}
        }}

        function renderQueue() {{
            document.getElementById('ops-text').value = commandQueue.join('\\n');
        }}

        function clearOps() {{
            commandQueue = [];
            updateBadge();
            renderQueue();
        }}

        function copyOps(showToast=false) {{
            const textToCopy = commandQueue.join('\\n');
            const hiddenTa = document.getElementById('hidden-copy');
            hiddenTa.value = textToCopy;
            hiddenTa.select();
            document.execCommand('copy');
            if(showToast) {{
                alert('Copied to clipboard. Now go back to Telegram and paste this!');
            }}
        }}

        function copyCmdText(cmdString) {{
            const hiddenTa = document.getElementById('hidden-copy');
            hiddenTa.value = cmdString;
            hiddenTa.select();
            document.execCommand('copy');
            alert('Copied command: ' + cmdString);
        }}

        // Init
        document.addEventListener('DOMContentLoaded', init);

    </script>
</body>
</html>
"""
    return html_template.encode('utf-8')
