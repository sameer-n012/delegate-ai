"""Compact plain-text renderers for CLI output. Agents read this, so keep it short."""

import json
import shlex
from datetime import datetime

from delegate import __version__
from delegate.integrations.setup import InitResult, SetupInfo
from delegate.core import (
    Agent,
    Event,
    Link,
    Project,
    ProjectState,
    ProjectStatus,
    Task,
    TaskDetail,
    TaskSummary,
    TaskType,
)

_COUNT_ORDER = ("todo", "in_progress", "review", "blocked", "done", "cancelled")


def _hhmm(iso: str) -> str:
    """``2026-01-01T12:30:00.000Z`` -> ``12:30Z``."""
    return datetime.fromisoformat(iso).strftime("%H:%MZ")


def summary_line(t: TaskSummary) -> str:
    # The type column stays blank for unknown so typed tasks stand out.
    kind = "" if t.type is TaskType.UNKNOWN else t.type.value
    parts = [f"{t.ref:<8}", str(t.priority), f"{t.status.value:<11}", f"{kind:<8}"]
    if t.ready:
        parts.append("[ready]")
    parts.append(t.title)
    if t.parent:
        parts.append(f"(parent {t.parent})")
    if t.assignee:
        parts.append(f"@{t.assignee}")
    return " ".join(parts)


def task_line(t: Task) -> str:
    """One line for a task just written; includes the version for ``--expect-version``."""
    tail = f" @{t.assignee}" if t.assignee else ""
    lease = f" (lease until {_hhmm(t.lease_expires_at)})" if t.lease_expires_at else ""
    return f"{t.ref} {t.priority} {t.status.value} v{t.version} {t.title}{tail}{lease}"


def _short(t: TaskSummary) -> str:
    return f"{t.ref} {t.priority} {t.title}"


def status_text(s: ProjectStatus) -> str:
    counts = " · ".join(
        f"{s.counts[k]} {k}" for k in _COUNT_ORDER if s.counts.get(k)
    )
    lines = [f"{s.project.name} ({s.project.key}): {counts or 'no tasks'}"]
    if s.project.state != ProjectState.IN_PROGRESS:
        lines.append(
            f"Project is {s.project.state.value}: agents cannot change tasks until a human "
            "runs `delegate --as human project state in_progress`."
        )
    if s.ready:
        lines.append("Ready:   " + " | ".join(_short(t) for t in s.ready))
    if s.mine:
        lines.append(
            "Yours:   "
            + " | ".join(
                f"{t.ref} {t.title}"
                + (f" (lease until {_hhmm(t.lease_expires_at)})" if t.lease_expires_at else "")
                for t in s.mine
            )
        )
    if s.in_progress:
        lines.append(
            "Others:  " + " | ".join(f"{t.ref} {t.title} [{t.assignee}]" for t in s.in_progress)
        )
    if s.blocked:
        lines.append(
            "Blocked: "
            + " | ".join(
                f"{b.task.ref} {b.task.title}" + (f" — {b.reason}" if b.reason else "")
                for b in s.blocked
            )
        )
    return "\n".join(lines)


def detail_text(d: TaskDetail) -> str:
    t = d.task
    head = f"{t.ref} {t.priority} {t.status.value}" + (" [ready]" if d.ready else "")
    lines = [f"{head}  {t.title}"]
    fields = [f"type: {t.type.value}", f"version: {t.version}"]
    if t.assignee:
        fields.append(f"assignee: {t.assignee}")
    if t.lease_expires_at:
        fields.append(f"lease until {_hhmm(t.lease_expires_at)}")
    if t.parent:
        fields.append(f"parent: {t.parent}")
    fields += [f"created: {t.created_at} by {t.created_by}", f"updated: {t.updated_at}"]
    if t.closed_at:
        fields.append(f"closed: {t.closed_at}")
    lines.append(" | ".join(fields))
    if t.description:
        lines += ["", "Description:", t.description]
    if t.acceptance:
        lines += ["", "Acceptance:", t.acceptance]
    for label, items in (
        ("Depends on", d.depends_on),
        ("Dependents", d.dependents),
        ("Children", d.children),
    ):
        if items:
            lines += ["", f"{label}:", *(f"  {summary_line(i)}" for i in items)]
    if d.notes:
        lines += ["", "Notes:"]
        for n in d.notes:
            lines.append(f"  [{n.created_at}] {n.author} ({n.kind.value}): {n.body}")
    if d.links:
        lines += ["", "Links:", *(f"  {link.kind.value}: {link.ref}" for link in d.links)]
    return "\n".join(lines)


def links_text(ref: str, links: list[Link]) -> str:
    return f"{ref} links: " + ", ".join(f"{link.kind.value}={link.ref}" for link in links)


def project_line(p: Project) -> str:
    return f"{p.key}  {p.state.value:<11}  {p.name}  {p.root_path}" + (
        f"  {p.git_remote}" if p.git_remote else ""
    )


def setup_info_text(info: SetupInfo) -> str:
    p = info.project
    lines = [f"{p.key} ({p.name}) at {info.root} [{p.state.value}]"]
    lines.append(f"installed: delegate {info.installed_version}")
    if info.options is None:
        lines.append("setup: not set up (run `delegate init`)")
        return "\n".join(lines)
    if info.recorded:
        lines.append(f"setup: delegate {info.recorded.version} on {info.recorded.updated_at}")
    else:
        lines.append("setup: older than 0.2.0 (no record; options inferred from the repo files)")
    o = info.options
    steps = [name for name, on in (("mcp", o.mcp), ("hooks", o.hooks), ("git-hook", o.git_hook)) if on]
    lines.append(
        f"options: command={shlex.join(o.command)!r} file={o.instructions_file} "
        f"steps={','.join(steps) or 'none'}"
    )
    if info.update_available:
        lines.append(f"update available: {info.installed_version}. Run `delegate project update`.")
    else:
        lines.append("up to date")
    return "\n".join(lines)


def setup_update_text(project: Project, previous: str | None, result: InitResult) -> str:
    from_version = previous or "an untracked version"
    lines = [f"{project.key}: updated repo files from delegate {from_version} to {__version__}"]
    lines += [f"  updated   {name}" for name in result.changed]
    lines += [f"  unchanged {name}" for name in result.unchanged]
    return "\n".join(lines)


def agent_line(a: Agent) -> str:
    where = " ".join(x for x in (a.branch, a.cwd) if x)
    return f"{a.id}  last seen {a.last_heartbeat}" + (f"  {where}" if where else "")


def event_line(e: Event) -> str:
    return " ".join(
        [f"#{e.id}", e.ts, e.agent or "-", e.type, e.task or "-"]
        + ([json.dumps(e.payload)] if e.payload else [])
    )
