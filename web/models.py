from pydantic import BaseModel
from typing import Optional

class ProjectCreate(BaseModel):
    pid: str
    title: str
    desc: str

class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None

class TaskCreate(BaseModel):
    title: str
    desc: str
    assignee: str = "Admin"

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    descriptions: Optional[str] = None

class CommentCreate(BaseModel):
    msg: str

class CommentUpdate(BaseModel):
    msg: str
