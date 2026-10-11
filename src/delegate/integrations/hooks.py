"""Hook handlers. Each takes parsed input and returns the text/JSON to print.

Claude Code hook contract (stdin JSON includes ``cwd``; Stop also has ``stop_hook_active``):

* SessionStart: plain stdout is added to the model's context.
* Stop: ``{"decision": "block", "reason": ...}`` makes the agent continue once with
  ``reason`` as its prompt. We block at most once (``stop_hook_active`` guard) so a
  session can never get stuck.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from delegate.core import DelegateError, Project, Status, TaskService
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


def stop_decision(
    service: TaskService, project: Project, hook_input: dict[str, Any]
) -> dict[str, str] | None:
    """Ask the agent once to close out tasks held by agents running in this cwd."""
    if hook_input.get("stop_hook_active"):
        return None
    cwd = str(hook_input.get("cwd") or "")
    agent_ids = {a.id for a in service.list_agents(project_id=project.id) if a.cwd == cwd}
    if not agent_ids:
        return None
    held = [
        t
        for t in service.list_tasks(project.id, statuses=[Status.IN_PROGRESS])
        if t.assignee in agent_ids
    ]
    if not held:
        return None
    refs = ", ".join(f"{t.ref} ({t.title})" for t in held)
    return {
        "decision": "block",
        "reason": (
            f"You still hold delegate tasks: {refs}. Before stopping, for each one either "
            "call complete_task with a summary and links, or release_task with a handoff note "
            "(or add a progress note if you will continue later)."
        ),
    }


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
