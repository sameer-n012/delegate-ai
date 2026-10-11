"""Presentation helpers: relative times, event text, and the view models used by templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Sequence
from typing import cast

from delegate.core import (
    Agent,
    Event,
    Project,
    ProjectState,
    Status,
    TaskService,
    TaskSummary,
)

ACTIVE_AGENT_MINUTES = 30
DONE_LIMIT = 20
ACTIVITY_LIMIT = 30
BOARD_COLUMNS: tuple[Status, ...] = (
    Status.TODO,
    Status.IN_PROGRESS,
    Status.REVIEW,
    Status.BLOCKED,
    Status.DONE,
)
COLUMN_LABELS: dict[str, str] = {
    "todo": "To do",
    "in_progress": "In progress",
    "review": "Review",
    "blocked": "Blocked",
    "done": "Done",
    "cancelled": "Cancelled",
}
STATE_LABELS: dict[str, str] = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "complete": "Complete",
    "inactive": "Inactive",
    "deleted": "Deleted",
}
# What the board banner says when agents are locked out. Keys are non-in_progress states.
STATE_BANNERS: dict[str, str] = {
    "not_started": "This project has not been started",
    "complete": "This project is complete",
    "inactive": "This project is inactive",
    "deleted": "This project is deleted and hidden from the project list",
}


def parse_ts(value: str) -> datetime | None:
    """Parse the UTC ISO timestamps written by the core. Returns None for bad input."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def ago(value: str | None, now: datetime | None = None) -> str:
    """Relative time such as ``just now``, ``2m ago``, ``3h ago``, ``5d ago``."""
    if not value:
        return ""
    moment = parse_ts(value)
    if moment is None:
        return value
    seconds = int(((now or datetime.now(UTC)) - moment).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def describe_event(event: Event) -> str:
    """One-line, plain-text summary of an event. Templates escape it."""
    p = event.payload
    ref = event.task or ""
    match event.type:
        case "task_created":
            return f"created {ref}: {p.get('title', '')}"
        case "task_claimed":
            return f"claimed {ref}"
        case "status_changed":
            verb = "released" if p.get("released") else "moved"
            return f"{verb} {ref}: {p.get('from', '?')} to {p.get('to', '?')}"
        case "task_updated":
            fields: object = p.get("fields", [])
            names = ", ".join(map(str, cast("list[object]", fields))) if isinstance(fields, list) else ""
            return f"edited {ref} ({names})"
        case "note_added":
            return f"added a {p.get('kind', '')} note to {ref}"
        case "link_added":
            return f"linked {p.get('kind', '')} to {ref}"
        case "lease_expired":
            return f"lease expired on {ref}; returned to todo"
        case "agent_registered":
            return "registered"
        case "project_created":
            return "project created"
        case "project_state_changed":
            target = str(p.get("to", "?")).replace("_", " ")
            return f"project \u2192 {target}"
        case other:
            return f"{other} {ref}".strip()


@dataclass(frozen=True, slots=True)
class AgentView:
    agent: Agent
    tasks: list[TaskSummary]


def active_agents(svc: TaskService, project: Project) -> list[AgentView]:
    """Agents with a recent heartbeat that belong to, or hold a task in, ``project``."""
    held = svc.list_tasks(project.id, statuses=[Status.IN_PROGRESS], limit=1000)
    recent = svc.list_agents(active_within_minutes=ACTIVE_AGENT_MINUTES)
    views: list[AgentView] = []
    for agent in recent:
        mine = [t for t in held if t.assignee == agent.id]
        if agent.project_id == project.id or mine:
            views.append(AgentView(agent=agent, tasks=mine))
    return views


def recent_events(svc: TaskService, project_id: int, count: int = ACTIVITY_LIMIT) -> list[Event]:
    """Latest ``count`` events for a project, newest first."""
    return svc.events_recent(project_id=project_id, limit=count)


@dataclass(frozen=True, slots=True)
class Column:
    status: Status
    label: str
    tasks: list[TaskSummary]
    truncated: bool = False


def board_columns(svc: TaskService, project: Project, show_cancelled: bool) -> list[Column]:
    columns: list[Column] = []
    statuses = list(BOARD_COLUMNS) + ([Status.CANCELLED] if show_cancelled else [])
    for status in statuses:
        truncated = False
        if status.is_closed:
            # Closed columns show only the most recently closed tasks. Fetch one extra
            # row to know whether the column was cut.
            tasks = svc.list_tasks(
                project.id, statuses=[status], limit=DONE_LIMIT + 1, order="recent"
            )
            truncated = len(tasks) > DONE_LIMIT
            tasks = tasks[:DONE_LIMIT]
        else:
            tasks = svc.list_tasks(project.id, statuses=[status], limit=1000)
        columns.append(Column(status, COLUMN_LABELS[status.value], tasks, truncated))
    return columns


@dataclass(frozen=True, slots=True)
class ProjectCard:
    project: Project
    counts: dict[str, int]
    agents: int
    open_total: int


def project_card(svc: TaskService, project: Project) -> ProjectCard:
    counts = svc.project_status(project.id).counts
    open_total = sum(n for s, n in counts.items() if s not in ("done", "cancelled"))
    return ProjectCard(project, counts, len(active_agents(svc, project)), open_total)


def project_cards(
    svc: TaskService,
    states: Sequence[ProjectState] | None = None,
    query: str | None = None,
) -> list[ProjectCard]:
    """Cards for the home page. ``states=None`` hides deleted projects (core rule)."""
    return [project_card(svc, p) for p in svc.list_projects(states=states, query=query)]
