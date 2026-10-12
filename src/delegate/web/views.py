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
)
"""Columns that always show. Done and Cancelled show only when their toggle is on."""
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
TYPE_LABELS: dict[str, str] = {
    "feature": "Feature",
    "bug": "Bug",
    "refactor": "Refactor",
    "test": "Test",
    "docs": "Docs",
    "design": "Design",
    "research": "Research",
    "chore": "Chore",
    "unknown": "Unknown",
}
# SVG path data for task type icons: 24x24 viewBox, drawn as 2px round strokes in
# currentColor. A zero-length segment ("h.01") renders as a dot.
TYPE_ICONS: dict[str, str] = {
    "feature": "M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4l-5.3 3 1.2-6-4.5-4.1 6-.7z",
    "bug": "M8 10h8v4a4 4 0 0 1-8 0z M9 10a3 3 0 0 1 6 0 M12 10v8 M4 14h4 M16 14h4"
    " M5 9l3 2 M19 9l-3 2 M5 20l3-2 M19 20l-3-2",
    "refactor": "M4 10a8 8 0 0 1 14-4l2 2 M20 4v4h-4 M20 14a8 8 0 0 1-14 4l-2-2 M4 20v-4h4",
    "test": "M9 3h6 M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3 M7.5 15h9",
    "docs": "M6 3h8l4 4v14H6z M14 3v4h4 M9 12h6 M9 16h6",
    "design": "M12 3a9 9 0 1 0 0 18c1.4 0 2-.9 2-2 0-1.4-1-1.8 0-3 .8-1 2-1 3-1a4 4 0 0 0 4-4"
    "c0-4.4-4-8-9-8z M7.5 12h.01 M9.5 7.5h.01 M14.5 7.5h.01",
    "research": "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-5-5",
    "chore": "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9"
    "l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z",
    "unknown": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M9.6 9.5a2.5 2.5 0 1 1 3.4 2.3"
    "c-.6.3-1 .8-1 1.5v.4 M12 16.5h.01",
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
        case "project_setup":
            return f"repo set up with delegate {p.get('version', '?')}"
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


@dataclass(frozen=True, slots=True)
class BoardOptions:
    """Which closed columns the board shows. Both are hidden by default."""

    show_done: bool = False
    show_cancelled: bool = False


def board_columns(svc: TaskService, project: Project, opts: BoardOptions) -> list[Column]:
    columns: list[Column] = []
    statuses = [
        *BOARD_COLUMNS,
        *([Status.DONE] if opts.show_done else []),
        *([Status.CANCELLED] if opts.show_cancelled else []),
    ]
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
