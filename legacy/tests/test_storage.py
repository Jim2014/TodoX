import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from orchestrator.storage import load_projects

projects = load_projects()
for pid, summary in projects.items():
    print(f"Project: {pid}, Title: {summary.title}, Status: {summary.status}")

if len(projects) > 0:
    print("SUCCESS: Loaded projects directly from folders.")
else:
    print("FAILED: No projects found.")
