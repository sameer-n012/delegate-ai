"""Core domain layer. All business rules live here; adapters (CLI, MCP, web) stay thin."""

from delegate.core.config import Settings, load_settings
from delegate.core.db import connect
from delegate.core.errors import (
    ConflictError,
    CycleError,
    DelegateError,
    InvalidTransitionError,
    NotFoundError,
    NotHolderError,
    ValidationError,
)
from delegate.core.models import (
    HUMAN,
    Actor,
    Agent,
    Event,
    Link,
    LinkKind,
    Note,
    NoteKind,
    Priority,
    Project,
    ProjectStatus,
    Status,
    Task,
    TaskDetail,
    TaskSummary,
)
from delegate.core.service import NewTask, TaskService, TaskUpdate, new_agent_id

__all__ = [
    "HUMAN",
    "Actor",
    "Agent",
    "ConflictError",
    "CycleError",
    "DelegateError",
    "Event",
    "InvalidTransitionError",
    "Link",
    "LinkKind",
    "NewTask",
    "Note",
    "NoteKind",
    "NotFoundError",
    "NotHolderError",
    "Priority",
    "Project",
    "ProjectStatus",
    "Settings",
    "Status",
    "Task",
    "TaskDetail",
    "TaskService",
    "TaskSummary",
    "TaskUpdate",
    "ValidationError",
    "connect",
    "load_settings",
    "new_agent_id",
]
