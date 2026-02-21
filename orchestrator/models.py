from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Dict

TaskStatus = Literal['not_started', 'in_progress', 'blocked', 'need_confirm', 'complete', 'failed']

class Comment(BaseModel):
    author: str
    time: str
    content: str

class Task(BaseModel):
    id: str
    status: TaskStatus = 'not_started'
    title: str = ""
    descriptions: str = ""
    creator: str = "Admin"
    assignee: str = "Admin"
    parent_id: Optional[str] = None
    child_ids: List[str] = Field(default_factory=list)
    comments: List[Comment] = Field(default_factory=list)

class ProjectSummary(BaseModel):
    id: str
    title: str
    status: str
    assignee: str
