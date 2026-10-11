"""Shared wiring for adapters: settings -> connection -> TaskService -> current project.

The CLI, MCP server, and web app all start here so they agree on DB path, lease
length, and how the current project is found.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from delegate.core import ANONYMOUS, Project, Settings, TaskService, connect, load_settings


@dataclass(slots=True)
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    service: TaskService

    def close(self) -> None:
        self.conn.close()

    def project_for(self, cwd: Path | None = None, key: str | None = None) -> Project:
        """Project by explicit key, else by ``DELEGATE_PROJECT``, else detected from ``cwd``.

        Detection auto-registers the project on first use.
        """
        key = key or os.environ.get("DELEGATE_PROJECT")
        if key:
            return self.service.get_project(key)
        return self.service.ensure_project(cwd or Path.cwd())


def resolve_actor(explicit: str | None = None) -> str:
    """Actor id from an explicit value, else ``DELEGATE_AGENT``, else ``anonymous``.

    There is deliberately no implicit ``human``: anonymous actors can read but every
    write is refused, so a misconfigured agent cannot bypass holder checks. People
    opt in with ``--as human`` or ``DELEGATE_AGENT=human``.
    """
    return (explicit or os.environ.get("DELEGATE_AGENT") or "").strip() or ANONYMOUS


def open_runtime(actor: str | None = None, *, settings: Settings | None = None) -> Runtime:
    settings = settings or load_settings()
    conn = connect(settings.db_path)
    service = TaskService(conn, resolve_actor(actor), lease_minutes=settings.lease_minutes)
    return Runtime(settings=settings, conn=conn, service=service)
