"""Compact plain-text renderers for MCP tool output. Agent context is precious."""

from __future__ import annotations

from collections.abc import Sequence

from delegate.core import ProjectState, ProjectStatus, Task, TaskDetail, TaskSummary, TaskType


def summary_line(t: TaskSummary) -> str:
    """``DLG-4 [P1 todo bug] Title (@agent) (parent DLG-1)``. The type is left out when unknown."""
    kind = "" if t.type is TaskType.UNKNOWN else f" {t.type.value}"
    parts = [f"{t.ref} [{t.priority} {t.status.value}{kind}] {t.title}"]
    if t.assignee:
        parts.append(f"(@{t.assignee})")
    if t.parent:
        parts.append(f"(parent {t.parent})")
    if t.ready:
        parts.append("(ready)")
    return " ".join(parts)


def task_list(tasks: Sequence[TaskSummary]) -> str:
    if not tasks:
        return "No matching tasks."
    return "\n".join(summary_line(t) for t in tasks)


def _section(title: str, lines: Sequence[str]) -> list[str]:
    return [f"{title}:", *(f"  {line}" for line in lines)] if lines else []


def project_status(status: ProjectStatus, agent_id: str) -> str:
    counts = " ".join(f"{k}={v}" for k, v in status.counts.items() if v)
    project = status.project
    lines = [
        f"Project {project.key} ({project.name}) [{project.state.value}]: {counts or 'no tasks'}",
        f"You are {agent_id}.",
    ]
    if project.state != ProjectState.IN_PROGRESS:
        lines.append(
            f"This project is {project.state.value}: you cannot change its tasks until a "
            "human sets it to in_progress. Ask the user."
        )
    lines += _section("Ready", [summary_line(t) for t in status.ready])
    lines += _section(
        "Yours",
        [f"{t.ref} [{t.priority}] {t.title} (lease until {t.lease_expires_at})" for t in status.mine],
    )
    lines += _section(
        "Held by others",
        [f"{t.ref} [{t.priority} {t.status.value}] {t.title} (@{t.assignee})" for t in status.in_progress],
    )
    lines += _section(
        "Unclaimed in progress (claim by ref to continue)",
        [summary_line(t) for t in status.unclaimed],
    )
    lines += _section(
        "Blocked",
        [f"{b.task.ref} {b.task.title}: {b.reason or 'no reason noted'}" for b in status.blocked],
    )
    return "\n".join(lines)


def _refs(tasks: Sequence[TaskSummary]) -> str:
    return ", ".join(f"{t.ref} ({t.status.value})" for t in tasks)


def task_detail(d: TaskDetail) -> str:
    t = d.task
    lines = [
        f"{t.ref}: {t.title}",
        f"status={t.status.value} priority={t.priority} type={t.type.value} "
        f"ready={'yes' if d.ready else 'no'} "
        f"version={t.version}",
    ]
    if t.assignee:
        lines.append(f"assignee={t.assignee} lease_until={t.lease_expires_at}")
    if t.parent:
        lines.append(f"parent={t.parent}")
    if d.depends_on:
        lines.append(f"depends_on: {_refs(d.depends_on)}")
    if d.dependents:
        lines.append(f"dependents: {_refs(d.dependents)}")
    if d.children:
        lines.append(f"children: {_refs(d.children)}")
    if t.description:
        lines += ["Description:", t.description]
    if t.acceptance:
        lines += ["Acceptance:", t.acceptance]
    if d.notes:
        lines.append("Notes:")
        lines += [f"  [{n.created_at}] {n.kind.value} by {n.author}: {n.body}" for n in d.notes]
    if d.links:
        lines.append("Links: " + ", ".join(f"{k.kind.value}:{k.ref}" for k in d.links))
    return "\n".join(lines)


def task_line(t: Task) -> str:
    return f"{t.ref} [{t.priority} {t.status.value}] {t.title} (version {t.version})"
