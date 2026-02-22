import os
import subprocess
import shutil
from datetime import datetime
from .storage import load_task, save_task, get_data_dir
from .models import Comment
from .state import state

def execute_ai_task(pid: str):
    task = load_task(pid, f"{pid}-0")
    if not task:
        return
        
    try:
        current_model = state.get_current_model()
        print(f"[{pid}] Starting AI Worker execution using Gemini CLI (Model: {current_model}).")
        
        project_dir = os.path.join(get_data_dir(), "projects", pid)
        system_prompt_path = os.path.join(project_dir, "system_prompt.md")
        system_prompt = ""
        if os.path.exists(system_prompt_path):
            with open(system_prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()
                
        prompt = f"{system_prompt}\n\nPlease check the ./project.json, complete the project according to the description, plan and create tasks, execute tasks. Each time only complete one step (e.g. create a new task, execute a task step, update task status, update project status, add comments). ALWAYS update the local JSON files directly."
        
        # Write to gemini.md so it acts as default for the CLI in that folder
        gemini_md_path = os.path.join(project_dir, "gemini.md")
        with open(gemini_md_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        
        # Call Gemini CLI
        gemini_exec = shutil.which("gemini")
        if not gemini_exec:
            raise RuntimeError("gemini executable not found in PATH")
            
        cmd = [gemini_exec, "-y", "--model", current_model, "-p", '"plan and execute the project.json"']
        print(f"[{pid}] Executing headless command: {' '.join(cmd)} (cwd: {project_dir})")
        
        env = os.environ.copy()
        
        process = subprocess.Popen(
            cmd,
            cwd=project_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            text=True,
            bufsize=1  # Line buffered
        )
        
        summary_lines = []
        for line in iter(process.stdout.readline, ''):
            print(f"[{pid} | gemini] {line}", end='')
            summary_lines.append(line)
            
        process.stdout.close()
        returncode = process.wait()
        
        summary = "".join(summary_lines).strip()
        
        if returncode == 0:
            print(f"[{pid}] AI Worker execution step finished successfully.")
            state.reset_success()
            return
        else:
            print(f"[{pid}] AI Worker execution failed with code {returncode}.")
            error_msg = f"Execution Failed with code {returncode}."
            
            if "429" in error_msg or any("429" in line for line in summary_lines):
                is_backoff = state.handle_429()
                if is_backoff:
                    print(f"[{pid}] Rate limit 429 detected on last model fallback. Backing off for 10 minutes.")
                    send_telegram_alert(pid, "⏳ [TodoX] Rate Limit 429 hit on all fallback models. Pausing AI Worker for 10 minutes.", summary_lines)
                else:
                    new_model = state.get_current_model()
                    print(f"[{pid}] Rate limit 429 detected. Switching to fallback model: {new_model}")
                    send_telegram_alert(pid, f"⚠️ [TodoX] Rate Limit 429 hit on {current_model}. Switching fallback model to {new_model}.", summary_lines)
                return
                
            fail_active_tasks(pid, error_msg, summary_lines)
            
    except Exception as e:
        print(f"[{pid}] AI Worker failed (CLI invocation error): {e}")
        error_msg = f"Execution System Error: {e}"
        fail_active_tasks(pid, error_msg, [])

def send_telegram_alert(pid: str, text: str, summary_lines: list):
    tg_token = os.environ.get("TG_TOKEN")
    admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
    if tg_token and admin_chat_id:
        try:
            import httpx
            import html
            import tempfile
            
            msg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            httpx.post(msg_url, json={"chat_id": admin_chat_id, "text": text}, timeout=10.0)
            
            if summary_lines:
                last_100 = summary_lines[-100:]
                html_log = f"<html><body style='background:#1e1e1e;color:#d4d4d4;padding:10px;'><pre>{html.escape(''.join(last_100))}</pre></body></html>"
                
                doc_url = f"https://api.telegram.org/bot{tg_token}/sendDocument"
                with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
                    tmp.write(html_log)
                    tmp_path = tmp.name
                    
                with open(tmp_path, "rb") as f:
                    files = {"document": (f"{pid}_error_log.html", f, "text/html")}
                    data = {"chat_id": admin_chat_id, "caption": f"Last {len(last_100)} lines of output log"}
                    httpx.post(doc_url, data=data, files=files, timeout=10.0)
                    
                os.remove(tmp_path)
        except Exception as e:
            print(f"[{pid}] Failed to send Telegram alert: {e}")

def fail_active_tasks(pid: str, error_msg: str, summary_lines: list):
    tasks_to_fail = []
    
    root_task = load_task(pid, f"{pid}-0")
    if root_task and root_task.assignee == "AI" and root_task.status == "in_progress":
        tasks_to_fail.append(root_task)
        
    tasks_dir = os.path.join(get_data_dir(), "projects", pid, "tasks")
    if os.path.exists(tasks_dir):
        for tid in os.listdir(tasks_dir):
            if tid == ".DS_Store": continue
            task = load_task(pid, tid)
            if task and task.assignee == "AI" and task.status == "in_progress":
                tasks_to_fail.append(task)
                
    if not tasks_to_fail and root_task:
        tasks_to_fail.append(root_task)
        
    comment_content = error_msg
    if summary_lines:
        last_100 = "".join(summary_lines[-100:])
        comment_content += f"\n\nOutput Log:\n{last_100}"
        
    for t in tasks_to_fail:
        t.status = "failed"
        t.assignee = "Admin"
        t.comments.append(Comment(
            author="System",
            time=datetime.now().isoformat(),
            content=comment_content
        ))
        save_task(pid, t)
        
    failed_tids = ", ".join(t.id for t in tasks_to_fail)
    alert_text = f"🚨 [TodoX] Project {pid} Execution Failed!\nTasks affected: {failed_tids}\nError: {error_msg}"
    send_telegram_alert(pid, alert_text, summary_lines)

