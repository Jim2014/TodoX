"""Pydantic data models for TodoX projects, tasks, comments, and optional harness metadata."""

from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

TaskStatus = Literal["not_started", "in_progress", "blocked", "need_confirm", "complete", "failed"]


class Comment(BaseModel):
    author: str
    time: str
    content: str


class HarnessConfig(BaseModel):
    """Optional per-project or per-task harness routing and guardrail overrides."""

    harness: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_preset: Optional[str] = None
    session_mode: Optional[Literal["stateless", "sticky"]] = None
    max_steps: Optional[int] = None
    max_tokens: Optional[int] = None


class ExecutionMeta(BaseModel):
    """Optional execution telemetry persisted on project/task state files."""

    last_harness: Optional[str] = None
    last_model: Optional[str] = None
    session_id: Optional[str] = None
    total_steps: int = 0
    total_tokens: int = 0
    total_duration_seconds: float = 0.0
    last_run_at: Optional[str] = None
    consecutive_failures: int = 0


class Task(BaseModel):
    """Core 9-Field Schema with backwards-compatible optional v2 harness metadata."""

    id: str
    status: TaskStatus = "not_started"
    title: str = ""
    descriptions: str = ""
    creator: str = "Admin"
    assignee: str = "Admin"
    parent_id: Optional[str] = None
    child_ids: List[str] = Field(default_factory=list)
    comments: List[Comment] = Field(default_factory=list)
    harness_config: Optional[HarnessConfig] = None
    execution_meta: Optional[ExecutionMeta] = None


class ProjectSummary(BaseModel):
    id: str
    title: str
    status: str
    assignee: str
