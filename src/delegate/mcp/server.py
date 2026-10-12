"""MCP server: thin tools over ``TaskService``. Business rules live in ``delegate.core``.

Stdout is the protocol channel. Never print to it; log to stderr only.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from delegate.core import (
    DelegateError,
    NewTask,
    Project,
    Status,
    TaskType,
    TaskUpdate,
    ValidationError,
    gitinfo,
    new_agent_id,
)
from delegate.mcp import format as fmt
from delegate.runtime import Runtime, open_runtime

log = logging.getLogger("delegate.mcp")

INSTRUCTIONS = """\
Delegate tracks tasks for coding agents. Task refs look like DLG-42.
1. Call project_status at the start of a session.
2. claim_task before working on a task. Only the holder can change or complete it.
3. Add decision and progress notes with add_note as you go.
4. When done, call complete_task with a summary and links (commits, PRs, files).
   If you stop early, call release_task with a handoff note.
5. Split big work into subtasks with create_tasks (use parent and depends_on)."""

T = TypeVar("T")

NoteKindArg = Literal["progress", "decision", "blocker", "handoff"]


class LinkIn(BaseModel):
    kind: Literal["file", "commit", "pr", "url"]
    ref: str


_db_lock = threading.Lock()
"""MCP 2.x runs sync tools in worker threads, so parallel tool calls could share the
single SQLite connection. ``transaction()`` joins an open transaction, which would
merge two calls' writes; serializing tool bodies prevents that."""


def _guard(fn: Callable[[], T]) -> T:
    """Run ``fn`` under the DB lock; turn every failure into a ToolError the agent can read."""
    try:
        with _db_lock:
            return fn()
    except ToolError:
        raise
    except DelegateError as exc:
        raise ToolError(f"{exc.code}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface any failure as a tool error, never crash
        log.exception("Tool failed")
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def build_server(runtime: Runtime, project: Project, agent_id: str) -> MCPServer:
    """Create the MCP server with tools bound to ``runtime``, ``project`` and ``agent_id``."""
    server = MCPServer("delegate", instructions=INSTRUCTIONS)
    svc = runtime.service

    def target(key: str | None) -> Project:
        return project if key is None else svc.get_project(key)

    @server.tool()
    def project_status(project: str | None = None) -> str:
        """Summary of a project: counts, ready tasks, your tasks, others' work, blockers.
        Call this first. `project` is an optional project key (default: current project)."""
        return _guard(
            lambda: fmt.project_status(svc.project_status(target(project).id), agent_id)
        )

    @server.tool()
    def list_tasks(
        status: list[str] | None = None,
        assignee: str | None = None,
        parent: str | None = None,
        type: TaskType | None = None,
        ready_only: bool = False,
        query: str | None = None,
        include_closed: bool = False,
        limit: int = 50,
        project: str | None = None,
    ) -> str:
        """List tasks, one line each. Closed tasks are hidden unless include_closed or a
        closed status is given. status: todo, in_progress, review, blocked, done, cancelled.
        parent is a task ref like DLG-1. type filters by task type. query matches title
        and description."""

        def run() -> str:
            try:
                statuses = [Status(s) for s in status] if status else None
            except ValueError as exc:
                raise ValidationError(f"Invalid status in {status}: {exc}") from exc
            return fmt.task_list(
                svc.list_tasks(
                    target(project).id,
                    statuses=statuses,
                    assignee=assignee,
                    parent=parent,
                    task_type=type,
                    ready_only=ready_only,
                    query=query,
                    include_closed=include_closed,
                    limit=limit,
                )
            )

        return _guard(run)

    @server.tool()
    def get_task(ref: str, project: str | None = None) -> str:
        """Full detail of one task (ref like DLG-42): fields, description, acceptance,
        dependencies, subtasks, notes and links. Includes `version` for expected_version."""
        return _guard(lambda: fmt.task_detail(svc.get_task_detail(ref, target(project).id)))

    @server.tool()
    def create_tasks(tasks: list[NewTask], project: str | None = None) -> str:
        """Create tasks in one atomic batch. Priority is P0 (highest) to P3, default P2.
        type: feature, bug, refactor, test, docs, design, research, chore (default unknown).
        Give an item a `local_id` (a word, not a ref) to let later items in the same batch
        use it in `parent` or `depends_on`. Those fields also accept existing refs (DLG-4)."""

        def run() -> str:
            created = svc.create_tasks(target(project).id, tasks)
            return "Created:\n" + "\n".join(f"{t.ref} {t.title}" for t in created)

        return _guard(run)

    @server.tool()
    def claim_task(
        ref: str | None = None,
        parent: str | None = None,
        max_priority: str | None = None,
        project: str | None = None,
    ) -> str:
        """Claim a task and start work. Without `ref`, claims the highest-priority ready
        task (optionally under `parent`, and at least as urgent as `max_priority`, e.g. P1).
        Returns the full task so you can start at once. Claims expire if you go idle."""

        def run() -> str:
            pid = target(project).id
            task = svc.claim_task(pid, ref, parent=parent, max_priority=max_priority)
            if task is None:
                return "No ready tasks."
            return "Claimed.\n" + fmt.task_detail(svc.get_task_detail(task.ref, pid))

        return _guard(run)

    @server.tool()
    def update_task(
        ref: str,
        title: str | None = None,
        description: str | None = None,
        acceptance: str | None = None,
        priority: str | None = None,
        type: TaskType | None = None,
        status: Status | None = None,
        parent: str | None = None,
        clear_parent: bool = False,
        add_deps: list[str] | None = None,
        remove_deps: list[str] | None = None,
        expected_version: int | None = None,
        project: str | None = None,
    ) -> str:
        """Change fields of a task you hold (or any non-claimed task). Omitted fields stay
        as they are. clear_parent=true removes the parent. status cannot be in_progress (use
        claim_task); use complete_task to finish. expected_version guards against edits
        by others."""

        def run() -> str:
            update = TaskUpdate(
                title=title,
                description=description,
                acceptance=acceptance,
                priority=priority,
                type=type,
                status=status,
                parent=parent,
                clear_parent=clear_parent,
                add_deps=add_deps or [],
                remove_deps=remove_deps or [],
            )
            task = svc.update_task(
                target(project).id, ref, update, expected_version=expected_version
            )
            return "Updated " + fmt.task_line(task)

        return _guard(run)

    @server.tool()
    def add_note(ref: str, kind: NoteKindArg, body: str, project: str | None = None) -> str:
        """Add a note to a task. kind: progress, decision (why you chose something),
        blocker (what stops you), handoff (state for the next agent)."""

        def run() -> str:
            note = svc.add_note(target(project).id, ref, kind, body)
            return f"Added {note.kind.value} note to {note.task}."

        return _guard(run)

    @server.tool()
    def complete_task(
        ref: str,
        summary: str,
        links: list[LinkIn] | None = None,
        to_review: bool = False,
        project: str | None = None,
    ) -> str:
        """Finish a task you hold. summary is required (what was done). links are
        {kind: file|commit|pr|url, ref}. to_review=true moves it to review instead of done."""

        def run() -> str:
            task = svc.complete_task(
                target(project).id,
                ref,
                summary,
                links=[(link.kind, link.ref) for link in links or []],
                to_review=to_review,
            )
            return "Completed " + fmt.task_line(task)

        return _guard(run)

    @server.tool()
    def release_task(ref: str, reason: str, project: str | None = None) -> str:
        """Give a task you hold back to the queue. reason becomes a handoff note: say what
        is done, what is left, and any pitfalls."""

        def run() -> str:
            task = svc.release_task(target(project).id, ref, reason)
            return "Released " + fmt.task_line(task)

        return _guard(run)

    return server


def run() -> None:
    """Entry point for ``delegate mcp``: open the DB, register this session, serve stdio."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    kind = os.environ.get("DELEGATE_AGENT_KIND", "claude")
    agent_id = os.environ.get("DELEGATE_AGENT") or new_agent_id(kind)
    runtime = open_runtime(agent_id)
    try:
        cwd = Path.cwd()
        project = runtime.project_for(cwd)
        runtime.service.register_agent(
            kind, cwd=str(cwd), branch=gitinfo.detect(cwd).branch, project_id=project.id
        )
        log.info("delegate mcp: agent %s, project %s", agent_id, project.key)
        build_server(runtime, project, agent_id).run()
    finally:
        runtime.close()
