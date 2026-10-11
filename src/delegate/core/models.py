"""Pydantic models returned by TaskService. Adapters serialize these directly."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

HUMAN = "human"
"""Reserved actor id for people. Humans bypass holder checks and never get leases."""


class Status(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def is_closed(self) -> bool:
        return self in (Status.DONE, Status.CANCELLED)


CLOSED_STATUSES: tuple[str, ...] = (Status.DONE.value, Status.CANCELLED.value)


class Priority(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3

    @classmethod
    def parse(cls, value: str | int) -> Priority:
        """Accept 0-3, "0"-"3", or "P0"-"P3" (any case)."""
        text = str(value).strip().upper().removeprefix("P")
        try:
            return cls(int(text))
        except ValueError as exc:
            raise ValueError(f"Invalid priority {value!r}; use P0-P3") from exc

    def __str__(self) -> str:
        return f"P{self.value}"


class NoteKind(StrEnum):
    PROGRESS = "progress"
    DECISION = "decision"
    BLOCKER = "blocker"
    HANDOFF = "handoff"
    SUMMARY = "summary"
    """Written by complete_task."""


class LinkKind(StrEnum):
    FILE = "file"
    COMMIT = "commit"
    PR = "pr"
    URL = "url"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Actor(_Model):
    """Who is calling the service: an agent id (e.g. ``claude#a3f``) or ``human``."""

    id: str

    @property
    def is_human(self) -> bool:
        return self.id == HUMAN


class Project(_Model):
    id: int
    key: str
    name: str
    root_path: str
    git_remote: str | None
    created_at: str


class Agent(_Model):
    id: str
    kind: str
    label: str | None
    cwd: str | None
    branch: str | None
    project_id: int | None
    created_at: str
    last_heartbeat: str


class TaskSummary(_Model):
    """Compact task view for lists. ``ref`` is the human id, e.g. ``DLG-42``."""

    ref: str
    title: str
    status: Status
    priority: Priority
    assignee: str | None
    parent: str | None
    ready: bool
    """True when status is todo and every dependency is done or cancelled."""
    updated_at: str
    closed_at: str | None


class Task(_Model):
    id: int
    ref: str
    project_id: int
    seq: int
    parent: str | None
    title: str
    description: str
    acceptance: str
    status: Status
    priority: Priority
    assignee: str | None
    lease_expires_at: str | None
    version: int
    created_by: str
    created_at: str
    updated_at: str
    closed_at: str | None


class Note(_Model):
    id: int
    task: str
    author: str
    kind: NoteKind
    body: str
    created_at: str


class Link(_Model):
    id: int
    task: str
    kind: LinkKind
    ref: str
    created_at: str


class TaskDetail(_Model):
    task: Task
    ready: bool
    depends_on: list[TaskSummary]
    dependents: list[TaskSummary]
    children: list[TaskSummary]
    notes: list[Note]
    links: list[Link]


class Event(_Model):
    id: int
    project_id: int | None
    task: str | None
    agent: str | None
    type: str
    payload: dict[str, Any]
    ts: str


class BlockedItem(_Model):
    task: TaskSummary
    reason: str | None
    """Body of the latest blocker note, if any."""


class ProjectStatus(_Model):
    project: Project
    counts: dict[str, int]
    ready: list[TaskSummary]
    mine: list[Task]
    in_progress: list[TaskSummary]
    blocked: list[BlockedItem]
