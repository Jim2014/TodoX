import os
import sys

# Prevent crash when running via pythonw on Windows by overriding None streams, and log into files!
if sys.stdout is None:
    sys.stdout = open("todox_web_stdout.log", "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open("todox_web_stderr.log", "w", encoding="utf-8")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")

import multiprocessing
import uvicorn
from fastapi import FastAPI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.api import router as api_router
from web.views import router as views_router

app = FastAPI(title="TodoX Web Panel")

app.include_router(api_router)
app.include_router(views_router)

def run_web_process():
    uvicorn.run("web.app:app", host="0.0.0.0", port=8899)

def run_web():
    p = multiprocessing.Process(target=run_web_process, daemon=True)
    p.start()
    return p
