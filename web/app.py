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
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.api import router as api_router
from web.views import router as views_router

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="TodoX Web Panel")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(api_router)
app.include_router(views_router)


def run_web_process():
    port = int(os.environ.get("WEB_PORT", "8899") or "8899")
    uvicorn.run("web.app:app", host="0.0.0.0", port=port)


def run_web():
    p = multiprocessing.Process(target=run_web_process, daemon=True)
    p.start()
    return p


if __name__ == "__main__":
    assert app.title == "TodoX Web Panel"
    assert STATIC_DIR.is_dir()
    print("web.app self-test passed!")
