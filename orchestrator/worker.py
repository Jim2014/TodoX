import os
import subprocess
from datetime import datetime
from .storage import load_task, save_task, get_data_dir
from .models import Comment

def execute_ai_task(pid: str, tid: str):
    task = load_task(pid, tid)
    if not task:
        return
        
    try:
        print(f"[{tid}] Starting AI Worker execution using Gemini CLI.")
        
        project_dir = os.path.join(get_data_dir(), "projects", pid)
        system_prompt_path = os.path.join(project_dir, "system_prompt.md")
        system_prompt = ""
        if os.path.exists(system_prompt_path):
            with open(system_prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()
                
        context = f"Task ID: {task.id}\nTitle: {task.title}\nDescription: {task.descriptions}\nComments: "
        for c in task.comments:
            context += f"\n[{c.time}] {c.author}: {c.content}"
            
        prompt = f"{system_prompt}\n\nTask Context:\n{context}\n\nPlease analyze the task and execute it."
        
        # Write to gemini.md so it acts as default for the CLI in that folder
        gemini_md_path = os.path.join(project_dir, "gemini.md")
        with open(gemini_md_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        
        # Call Gemini CLI
        result = subprocess.run(
            ["gemini"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False
        )
        
        summary = result.stdout.strip()
        if result.stderr:
            summary += f"\n\nErrors/Warnings:\n{result.stderr.strip()}"
        
        if result.returncode == 0:
            task.status = "complete"
            task.assignee = "Admin"
            task.comments.append(Comment(
                author="AI",
                time=datetime.now().isoformat(),
                content=f"Executed task via Gemini CLI. Output:\n{summary}"
            ))
            print(f"[{tid}] AI Worker execution finished successfully.")
        else:
            task.status = "failed"
            task.assignee = "Admin"
            task.comments.append(Comment(
                author="AI",
                time=datetime.now().isoformat(),
                content=f"Execution Failed with code {result.returncode}.\nOutput:\n{summary}"
            ))
            print(f"[{tid}] AI Worker execution failed with code {result.returncode}.")
            
        save_task(pid, task)
    except Exception as e:
        print(f"[{tid}] AI Worker failed (CLI invocation error): {e}")
        task.status = "failed"
        task.assignee = "Admin"
        task.comments.append(Comment(
            author="AI",
            time=datetime.now().isoformat(),
            content=f"Execution System Error: {e}"
        ))
        save_task(pid, task)

