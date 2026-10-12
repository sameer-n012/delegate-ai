"""Hook handlers. Each takes parsed input and returns the text/JSON to print.

Claude Code hook contract (stdin JSON includes ``cwd``):

* SessionStart: plain stdout is added to the model's context.

There is no Stop hook. It found the session's agent by ``cwd``, and subagents share the
MCP agent id of their parent, so it flagged worker claims on the orchestrator. Leases
expire on their own, so it was removed in 0.2.1.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from delegate.core import DelegateError, Project, ProjectState, TaskService
from delegate.core.gitinfo import GIT_TIMEOUT_S

_REF_IN_TEXT = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")


def session_start_context(service: TaskService, project: Project) -> str:
    status = service.project_status(project.id, ready_limit=5)
    c = status.counts
    lines = [
        f"[delegate] Project {project.name} ({project.key}): "
        f"{c['todo']} todo, {c['in_progress']} in progress, {c['review']} review, "
        f"{c['blocked']} blocked.",
    ]
    if project.state != ProjectState.IN_PROGRESS:
        lines.append(
            f"Project state is {project.state.value}: you cannot change tasks until a human "
            "sets it to in_progress. Ask the user before doing tracked work."
        )
    if status.ready:
        lines.append(
            "Ready: " + " | ".join(f"{t.ref} {t.priority} {t.title}" for t in status.ready)
        )
    if status.in_progress:
        lines.append(
            "In progress: "
            + " | ".join(f"{t.ref} {t.title} [{t.assignee}]" for t in status.in_progress)
        )
    for b in status.blocked:
        lines.append(f"Blocked: {b.task.ref} {b.task.title}" + (f" - {b.reason}" if b.reason else ""))
    lines.append(
        "Use the delegate MCP tools: claim_task before working; add_note for decisions; "
        "complete_task or release_task when you stop."
    )
    return "\n".join(lines)


def refs_in_message(message: str, project_key: str) -> list[str]:
    """Task refs for this project mentioned in a commit message, de-duplicated, in order."""
    seen: dict[str, None] = {}
    for key, seq in _REF_IN_TEXT.findall(message):
        if key == project_key:
            seen.setdefault(f"{key}-{seq}", None)
    return list(seen)


def head_commit(cwd: Path) -> tuple[str, str] | None:
    """(sha, full message) of HEAD, or None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H%n%B"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    sha, _, message = out.stdout.partition("\n")
    return sha.strip(), message


def link_commit(service: TaskService, project: Project, sha: str, message: str) -> list[str]:
    """Attach ``sha`` to every existing task the message mentions. Returns linked refs.

    Unknown refs are skipped: a typo in a commit message must not fail the hook.
    """
    linked: list[str] = []
    for ref in refs_in_message(message, project.key):
        try:
            service.add_links(project.id, ref, [("commit", sha)])
        except DelegateError:  # e.g. the ref does not exist; see docstring
            continue
        linked.append(ref)
    return linked
