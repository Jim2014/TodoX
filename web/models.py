"""Pydantic request schemas for TodoX Web UI REST API."""

from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel


class ProjectCreate(BaseModel):
    pid: str
    title: str
    desc: str


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None
    harness_config: Optional[Dict[str, Any]] = None


class TaskCreate(BaseModel):
    title: str
    desc: str
    assignee: str = "Admin"
    status: str = "not_started"
    harness_config: Optional[Dict[str, Any]] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None
    harness_config: Optional[Dict[str, Any]] = None


class CommentCreate(BaseModel):
    msg: str


class CommentUpdate(BaseModel):
    msg: str


class SystemStateUpdate(BaseModel):
    watcher_paused: Optional[bool] = None
    default_harness: Optional[str] = None
    default_model: Optional[str] = None
    default_effort: Optional[str] = None
    default_permission_preset: Optional[str] = None
    pause_project: Optional[str] = None
    resume_project: Optional[str] = None


class SteerRequest(BaseModel):
    msg: str


class ApproveRequest(BaseModel):
    comment: Optional[str] = None


if __name__ == "__main__":
    p = ProjectCreate(pid="DEMO", title="Demo", desc="Check")
    assert p.pid == "DEMO"
    tc = TaskCreate(title="T1", desc="Desc")
    assert tc.status == "not_started" and tc.assignee == "Admin"
    ss = SystemStateUpdate(watcher_paused=True, default_effort="high")
    assert ss.watcher_paused is True
    print("web.models self-test passed!")
