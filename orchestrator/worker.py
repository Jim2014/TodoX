import os
import subprocess
from datetime import datetime
from .storage import load_task, save_task, get_data_dir
from .models import Comment

def execute_ai_task(pid: str):
    task = load_task(pid, f"{pid}-0")
    if not task:
        return
        
    try:
        print(f"[{pid}] Starting AI Worker execution using Gemini CLI.")
        
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
        cmd = ["gemini"]
        print(f"[{pid}] Executing headless command: {' '.join(cmd)} (cwd: {project_dir})")
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False
        )
        
        summary = result.stdout.strip()
        if result.stderr:
            summary += f"\n\nErrors/Warnings:\n{result.stderr.strip()}"
        
        if result.returncode == 0:
            print(f"[{pid}] AI Worker execution step finished successfully.")
            # Status management is deferred to the AI's file I/O operations directly.
        else:
            print(f"[{pid}] AI Worker execution failed with code {result.returncode}.")
            # Log failure defensively and disable infinite loop
            task.status = "failed"
            task.assignee = "Admin"
            task.comments.append(Comment(
                author="System",
                time=datetime.now().isoformat(),
                content=f"Execution Failed with code {result.returncode}.\nOutput:\n{summary}"
            ))
            save_task(pid, task)
            
    except Exception as e:
        print(f"[{pid}] AI Worker failed (CLI invocation error): {e}")
        task.status = "failed"
        task.assignee = "Admin"
        task.comments.append(Comment(
            author="System",
            time=datetime.now().isoformat(),
            content=f"Execution System Error: {e}"
        ))
        save_task(pid, task)

