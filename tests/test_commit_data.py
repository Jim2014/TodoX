import os
import subprocess
import tempfile
import time
from datetime import datetime

def check_and_commit_data(data_dir):
    if not os.path.exists(data_dir):
        print(f"Directory doesn't exist: {data_dir}")
        return
        
    git_dir = os.path.join(data_dir, ".git")
    if not os.path.exists(git_dir):
        print(f"Not a git repo: {data_dir}")
        return
        
    try:
        status_output = subprocess.check_output(
            ["git", "status", "--porcelain"], 
            cwd=data_dir, 
            stderr=subprocess.STDOUT,
            text=True
        )
        if status_output.strip():
            subprocess.run(["git", "add", "."], cwd=data_dir, check=True)
            commit_message = f"Auto-commit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            subprocess.run(["git", "commit", "-m", commit_message], cwd=data_dir, check=True)
            print(f"Committed changes in data directory: {commit_message}")
        else:
            print("No changes to commit.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to check or commit data directory: {e}")
    except Exception as e:
        print(f"Error during auto-commit: {e}")

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Created temp dir: {temp_dir}")
        subprocess.run(["git", "init"], cwd=temp_dir, check=True)
        
        # Check empty
        print("Checking empty repo...")
        check_and_commit_data(temp_dir)
        
        # Add a file
        print("Adding a file...")
        with open(os.path.join(temp_dir, "test.txt"), "w") as f:
            f.write("Hello world")
            
        check_and_commit_data(temp_dir)
        
        # Check logs
        log = subprocess.check_output(["git", "log", "--oneline"], cwd=temp_dir, text=True)
        print("Git log:")
        print(log)
