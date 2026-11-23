import os
import json
import asyncio
from unittest.mock import MagicMock
from telegram import Update
from telegram.ext import ContextTypes

# We will test the checker logic here
def check_project_data(data_dir: str):
    """
    Checks project.json and all task.json files for JSON decode errors.
    Returns a list of error tuples: (file_path, error_message)
    """
    errors = []
    projects_dir = os.path.join(data_dir, "projects")
    
    if not os.path.exists(projects_dir):
        return [("nonexistent_data", "Data directory not found")]

    for pid in os.listdir(projects_dir):
        project_dir = os.path.join(projects_dir, pid)
        if not os.path.isdir(project_dir):
            continue
            
        project_json_path = os.path.join(project_dir, "project.json")
        if os.path.exists(project_json_path):
            try:
                with open(project_json_path, "r", encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append((project_json_path, f"JSONDecodeError: {e}"))
            except Exception as e:
                errors.append((project_json_path, f"Error reading file: {e}"))
                
        tasks_dir = os.path.join(project_dir, "tasks")
        if os.path.exists(tasks_dir):
            for tid in os.listdir(tasks_dir):
                task_dir = os.path.join(tasks_dir, tid)
                if os.path.isdir(task_dir):
                    task_json_path = os.path.join(task_dir, "task.json")
                    if os.path.exists(task_json_path):
                        try:
                            with open(task_json_path, "r", encoding="utf-8") as f:
                                json.load(f)
                        except json.JSONDecodeError as e:
                            errors.append((task_json_path, f"JSONDecodeError: {e}"))
                        except Exception as e:
                            errors.append((task_json_path, f"Error reading file: {e}"))
                        
    return errors

if __name__ == "__main__":
    # Create a dummy bad project for testing
    test_pid = "TESTFIX"
    test_data_dir = "./test_data"
    
    import shutil
    if os.path.exists(test_data_dir):
        shutil.rmtree(test_data_dir)
        
    os.makedirs(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTFIX-1"), exist_ok=True)
    
    with open(os.path.join(test_data_dir, "projects", test_pid, "project.json"), "w") as f:
        f.write("{ bad json ")
        
    with open(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTFIX-1", "task.json"), "w") as f:
        f.write("{ \"title\": \"good\", \"status\": \"bad\" }") # good json
        
    with open(os.path.join(test_data_dir, "projects", test_pid, "tasks", "TESTFIX-2"), "w") as f:
        pass # Not a dir

    errors = check_project_data(test_data_dir)
    print(f"Found errors: {errors}")
    assert len(errors) == 1
    assert "project.json" in errors[0][0]
    
    print("Test passed!")
