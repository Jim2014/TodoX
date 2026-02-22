import os
import sys
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
    uvicorn.run(app, host="0.0.0.0", port=8000)

def run_web():
    p = multiprocessing.Process(target=run_web_process, daemon=True)
    p.start()
    return p
