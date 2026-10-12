"""TaskService: every business rule for projects, tasks, claims, notes, and events.

Rules enforced here (adapters must not duplicate them):

* Agents start work with ``claim_task``; only the holder may change an
  ``in_progress`` task. Humans (actor id ``human``) bypass holder checks.
* A claim carries a lease. Any write by the holding agent renews all its leases.
  Expired leases are reaped lazily: the task returns to ``todo``.
* A task is *ready* when it is ``todo`` and every dependency is done or cancelled.
  Only ready tasks can be claimed. ``claim_task()`` without a ref also skips
  tasks with open subtasks (epics), so agents pick up leaf work.
* Dependencies and parents stay within one project and must not form cycles.
* The ``anonymous`` actor (no identity given) can read but never write.
* Agents may change tasks only in ``in_progress`` projects; only humans change
  project state.
* Every state change appends one row to ``events``.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from delegate.core import gitinfo
from delegate.core.clock import Clock, plus_minutes, system_clock, to_iso
from delegate.core.db import transaction
from delegate.core.errors import (
    ConflictError,
    CycleError,
    ForbiddenError,
    IdentityRequiredError,
    InvalidTransitionError,
    NotFoundError,
    NotHolderError,
    ProjectClosedError,
    ValidationError,
)
from delegate.core.ids import derive_key, format_ref, parse_ref, validate_key
from delegate.core.models import (
    CLOSED_STATUSES,
    HUMAN,
    Actor,
    Agent,
    BlockedItem,
    Event,
    Link,
    LinkKind,
    Note,
    NoteKind,
    Priority,
    Project,
    ProjectSetup,
    ProjectState,
    ProjectStatus,
    Status,
    Task,
    TaskDetail,
    TaskSummary,
    TaskType,
)

# Status changes allowed through update_task() for agents. Entering in_progress
# is only possible through claim_task(). Humans may make any change.
AGENT_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.TODO: frozenset({Status.BLOCKED, Status.CANCELLED}),
    Status.BLOCKED: frozenset({Status.TODO, Status.CANCELLED}),
    Status.IN_PROGRESS: frozenset({Status.TODO, Status.BLOCKED, Status.REVIEW, Status.CANCELLED}),
    Status.REVIEW: frozenset({Status.DONE, Status.TODO, Status.CANCELLED}),
    Status.DONE: frozenset({Status.TODO}),
    Status.CANCELLED: frozenset({Status.TODO}),
}

_CLOSED_SQL = "('done','cancelled')"

# Shared SELECT for tasks: adds the project key, parent seq, and the derived ready flag.
_TASK_SELECT = f"""
SELECT t.*, p.key AS pkey, pt.seq AS parent_seq,
  (t.status = 'todo' AND NOT EXISTS (
      SELECT 1 FROM task_deps d JOIN tasks u ON u.id = d.depends_on_id
      WHERE d.task_id = t.id AND u.status NOT IN {_CLOSED_SQL})) AS ready
FROM tasks t
JOIN projects p ON p.id = t.project_id
LEFT JOIN tasks pt ON pt.id = t.parent_id
"""

_NO_OPEN_CHILDREN = f"""NOT EXISTS (
    SELECT 1 FROM tasks c WHERE c.parent_id = t.id AND c.status NOT IN {_CLOSED_SQL})"""


class NewTask(BaseModel):
    """Input for ``create_tasks``.

    ``parent`` and ``depends_on`` entries may be existing refs (``DLG-4``) or the
    ``local_id`` of an earlier item in the same batch.
    """

    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    acceptance: str = ""
    priority: int | str = 2
    type: TaskType | str = TaskType.UNKNOWN
    parent: str | None = None
    depends_on: list[str] = Field(default_factory=list[str])
    local_id: str | None = None


class TaskUpdate(BaseModel):
    """Input for ``update_task``. ``None`` means "leave unchanged".

    ``clear_parent=True`` removes the parent. It cannot be combined with ``parent``.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    acceptance: str | None = None
    priority: int | str | None = None
    type: TaskType | str | None = None
    status: Status | None = None
    parent: str | None = None
    clear_parent: bool = False
    add_deps: list[str] = Field(default_factory=list[str])
    remove_deps: list[str] = Field(default_factory=list[str])


def new_agent_id(kind: str) -> str:
    """E.g. ``claude#a3f9``. The kind prefix keeps ids readable on the dashboard."""
    kind = "".join(c for c in kind.lower() if c.isalnum() or c in "-_") or "agent"
    return f"{kind}#{secrets.token_hex(2)}"


class TaskService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        actor: Actor | str,
        *,
        lease_minutes: int = 15,
        clock: Clock = system_clock,
    ) -> None:
        self.conn = conn
        self.actor = actor if isinstance(actor, Actor) else Actor(id=actor)
        self.lease_minutes = lease_minutes
        self._clock = clock

    # ------------------------------------------------------------------ helpers

    def _now(self) -> str:
        return to_iso(self._clock())

    def _lease_deadline(self) -> str:
        return to_iso(plus_minutes(self._clock(), self.lease_minutes))

    def _emit(
        self,
        type_: str,
        project_id: int | None,
        task_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events (project_id, task_id, agent, type, payload, ts) VALUES (?,?,?,?,?,?)",
            (project_id, task_id, self.actor.id, type_, json.dumps(payload or {}), self._now()),
        )

    def _touch(self) -> None:
        """Record agent liveness: upsert the agent row and renew all its leases."""
        if self.actor.is_human:
            return
        now = self._now()
        kind = self.actor.id.split("#", 1)[0] or "agent"
        self.conn.execute(
            """INSERT INTO agents (id, kind, created_at, last_heartbeat) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat""",
            (self.actor.id, kind, now, now),
        )
        self.conn.execute(
            """UPDATE tasks SET lease_expires_at = ?
               WHERE assignee = ? AND status = 'in_progress' AND lease_expires_at IS NOT NULL""",
            (self._lease_deadline(), self.actor.id),
        )

    def _reap(self) -> int:
        """Return expired claims to todo. Must run inside a write transaction."""
        now = self._now()
        expired = self.conn.execute(
            """SELECT id, project_id, assignee FROM tasks
               WHERE status = 'in_progress' AND lease_expires_at IS NOT NULL
                 AND lease_expires_at < ?""",
            (now,),
        ).fetchall()
        for row in expired:
            self.conn.execute(
                """UPDATE tasks SET status = 'todo', assignee = NULL, lease_expires_at = NULL,
                   version = version + 1, updated_at = ? WHERE id = ?""",
                (now, row["id"]),
            )
            self.conn.execute(
                "INSERT INTO events (project_id, task_id, agent, type, payload, ts) VALUES (?,?,?,?,?,?)",
                (
                    row["project_id"],
                    row["id"],
                    None,
                    "lease_expired",
                    json.dumps({"previous_assignee": row["assignee"]}),
                    now,
                ),
            )
        return len(expired)

    def reap_expired_leases(self) -> int:
        """Reap if needed. Takes the write lock only when something has expired."""
        has_expired = self.conn.execute(
            """SELECT 1 FROM tasks WHERE status = 'in_progress' AND lease_expires_at IS NOT NULL
               AND lease_expires_at < ? LIMIT 1""",
            (self._now(),),
        ).fetchone()
        if not has_expired:
            return 0
        with transaction(self.conn):
            return self._reap()

    def _write(self) -> _WriteTxn:
        """Open a write transaction that first reaps leases and records liveness."""
        return _WriteTxn(self)

    # ------------------------------------------------------------------ projects

    def ensure_project(
        self, cwd: Path, *, key: str | None = None, name: str | None = None
    ) -> Project:
        """Find the project for ``cwd`` (git remote first, then root path) or create it."""
        info = gitinfo.detect(cwd)
        with transaction(self.conn):
            row = None
            if info.remote:
                row = self.conn.execute(
                    "SELECT * FROM projects WHERE git_remote = ?", (info.remote,)
                ).fetchone()
            if row is None:
                row = self.conn.execute(
                    "SELECT * FROM projects WHERE root_path = ?", (str(info.root),)
                ).fetchone()
                if row is not None and info.remote and row["git_remote"] is None:
                    # The repo gained a remote since registration; remember it.
                    self.conn.execute(
                        "UPDATE projects SET git_remote = ? WHERE id = ?", (info.remote, row["id"])
                    )
            if row is not None:
                return self._project_by_id(row["id"])

            taken = {r[0] for r in self.conn.execute("SELECT key FROM projects")}
            if key is not None:
                key = validate_key(key)
                if key in taken:
                    raise ConflictError(f"Project key {key} is already in use")
            else:
                key = derive_key(name or info.name, taken)
            cur = self.conn.execute(
                "INSERT INTO projects (key, name, root_path, git_remote, created_at) VALUES (?,?,?,?,?)",
                (key, name or info.name, str(info.root), info.remote, self._now()),
            )
            project_id = cur.lastrowid
            assert project_id is not None
            self._emit("project_created", project_id, payload={"key": key})
            return self._project_by_id(project_id)

    def _project_by_id(self, project_id: int) -> Project:
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Project {project_id} not found")
        return _project(row)

    def get_project(self, key_or_id: str | int) -> Project:
        if isinstance(key_or_id, int) or str(key_or_id).isdigit():
            return self._project_by_id(int(key_or_id))
        row = self.conn.execute(
            "SELECT * FROM projects WHERE key = ?", (str(key_or_id).upper(),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {key_or_id} not found")
        return _project(row)

    def list_projects(
        self, *, states: Sequence[ProjectState] | None = None, query: str | None = None
    ) -> list[Project]:
        """List projects. Without ``states`` (None or empty), deleted projects are hidden.

        ``query`` matches name, root path, or key (case-insensitive substring).
        """
        where: list[str] = []
        params: list[Any] = []
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(s.value for s in states)
        else:
            where.append("state != 'deleted'")
        if query and query.strip():
            like = f"%{_escape_like(query.strip())}%"
            where.append(
                "(name LIKE ? ESCAPE '\\' OR root_path LIKE ? ESCAPE '\\' OR key LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like])
        sql = "SELECT * FROM projects WHERE " + " AND ".join(where) + " ORDER BY name"
        return [_project(r) for r in self.conn.execute(sql, params)]

    def get_setup(self, project_id: int) -> ProjectSetup | None:
        """The repo setup record, or None if the project has no record."""
        row = self.conn.execute(
            "SELECT setup_version, setup_options, setup_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {project_id} not found")
        if row["setup_version"] is None:
            return None
        try:
            options: object = json.loads(row["setup_options"] or "{}")
        except json.JSONDecodeError:
            options = {}  # A damaged record must not block `project info`; update rewrites it.
        return ProjectSetup(
            version=row["setup_version"],
            options=cast(dict[str, Any], options) if isinstance(options, dict) else {},
            updated_at=row["setup_at"],
        )

    def record_setup(
        self, project_id: int, version: str, options: dict[str, Any]
    ) -> ProjectSetup:
        """Store the version and options that last wrote the repo files.

        Like ``ensure_project``, this is setup bookkeeping, not a task write. It needs no
        identity and is allowed in every project state.
        """
        with transaction(self.conn):
            self._project_by_id(project_id)
            self.conn.execute(
                "UPDATE projects SET setup_version = ?, setup_options = ?, setup_at = ? WHERE id = ?",
                (version, json.dumps(options, sort_keys=True), self._now(), project_id),
            )
            self._emit("project_setup", project_id, payload={"version": version})
        setup = self.get_setup(project_id)
        assert setup is not None
        return setup

    def set_project_state(self, key_or_id: str | int, state: ProjectState | str) -> Project:
        """Change a project's lifecycle state. Humans only."""
        if not self.actor.is_human:
            raise ForbiddenError("Only a human can change a project's state (use --as human)")
        try:
            new_state = ProjectState(state)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid project state {state!r}; use one of "
                f"{', '.join(s.value for s in ProjectState)}"
            ) from exc
        with self._write():
            project = self.get_project(key_or_id)
            if project.state == new_state:
                return project
            self.conn.execute(
                "UPDATE projects SET state = ? WHERE id = ?", (new_state.value, project.id)
            )
            self._emit(
                "project_state_changed",
                project.id,
                payload={"from": project.state.value, "to": new_state.value},
            )
            return self._project_by_id(project.id)

    def _check_project_open(self, project_id: int) -> None:
        """Agents may change tasks only in in_progress projects. Humans are exempt."""
        if self.actor.is_human:
            return
        row = self.conn.execute(
            "SELECT key, state FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {project_id} not found")
        if row["state"] != ProjectState.IN_PROGRESS:
            raise ProjectClosedError(
                f"Project {row['key']} is {row['state']}; a human must set it to "
                "in_progress before agents can change its tasks"
            )

    # ------------------------------------------------------------------ agents

    def register_agent(
        self,
        kind: str,
        *,
        label: str | None = None,
        cwd: str | None = None,
        branch: str | None = None,
        project_id: int | None = None,
    ) -> Agent:
        """Create or refresh the agent row for the current actor."""
        if self.actor.is_human or self.actor.is_anonymous:
            raise ValidationError(f"The {self.actor.id} actor is not an agent")
        now = self._now()
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO agents (id, kind, label, cwd, branch, project_id, created_at, last_heartbeat)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, label = excluded.label,
                     cwd = excluded.cwd, branch = excluded.branch,
                     project_id = excluded.project_id, last_heartbeat = excluded.last_heartbeat""",
                (self.actor.id, kind, label, cwd, branch, project_id, now, now),
            )
            self._emit("agent_registered", project_id, payload={"kind": kind})
        row = self.conn.execute("SELECT * FROM agents WHERE id = ?", (self.actor.id,)).fetchone()
        return Agent.model_validate(dict(row))

    def heartbeat(self) -> None:
        with self._write():
            pass

    def list_agents(
        self, *, project_id: int | None = None, active_within_minutes: int | None = None
    ) -> list[Agent]:
        sql = "SELECT * FROM agents WHERE 1=1"
        params: list[Any] = []
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if active_within_minutes is not None:
            sql += " AND last_heartbeat >= ?"
            params.append(to_iso(plus_minutes(self._clock(), -active_within_minutes)))
        sql += " ORDER BY last_heartbeat DESC"
        return [Agent.model_validate(dict(r)) for r in self.conn.execute(sql, params)]

    # ------------------------------------------------------------------ task lookup

    def _row(self, ref: str | int, project_id: int | None) -> sqlite3.Row:
        """Resolve ``DLG-42`` (any project) or ``42`` (within ``project_id``)."""
        key, seq = parse_ref(ref)
        if key is not None:
            row = self.conn.execute(
                _TASK_SELECT + " WHERE p.key = ? AND t.seq = ?", (key, seq)
            ).fetchone()
        elif project_id is not None:
            row = self.conn.execute(
                _TASK_SELECT + " WHERE t.project_id = ? AND t.seq = ?", (project_id, seq)
            ).fetchone()
        else:
            raise ValidationError(f"Task reference {ref!r} needs a project key, e.g. DLG-{seq}")
        if row is None:
            raise NotFoundError(f"Task {ref} not found")
        return row

    def _row_by_id(self, task_id: int) -> sqlite3.Row:
        row = self.conn.execute(_TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task id {task_id} not found")
        return row

    def get_task(self, ref: str | int, project_id: int | None = None) -> Task:
        return _task(self._row(ref, project_id))

    def get_task_detail(self, ref: str | int, project_id: int | None = None) -> TaskDetail:
        self.reap_expired_leases()
        row = self._row(ref, project_id)
        tid = row["id"]
        task_ref = _ref(row)
        deps = self.conn.execute(
            _TASK_SELECT + " JOIN task_deps d ON d.depends_on_id = t.id WHERE d.task_id = ?"
            " ORDER BY t.seq",
            (tid,),
        ).fetchall()
        dependents = self.conn.execute(
            _TASK_SELECT + " JOIN task_deps d ON d.task_id = t.id WHERE d.depends_on_id = ?"
            " ORDER BY t.seq",
            (tid,),
        ).fetchall()
        children = self.conn.execute(
            _TASK_SELECT + " WHERE t.parent_id = ? ORDER BY t.seq", (tid,)
        ).fetchall()
        notes = self.conn.execute(
            "SELECT * FROM notes WHERE task_id = ? ORDER BY id", (tid,)
        ).fetchall()
        links = self.conn.execute(
            "SELECT * FROM links WHERE task_id = ? ORDER BY id", (tid,)
        ).fetchall()
        return TaskDetail(
            task=_task(row),
            ready=bool(row["ready"]),
            depends_on=[_summary(r) for r in deps],
            dependents=[_summary(r) for r in dependents],
            children=[_summary(r) for r in children],
            notes=[_note(r, task_ref) for r in notes],
            links=[_link(r, task_ref) for r in links],
        )

    def list_tasks(
        self,
        project_id: int,
        *,
        statuses: Sequence[Status] | None = None,
        assignee: str | None = None,
        parent: str | None = None,
        task_type: TaskType | str | None = None,
        ready_only: bool = False,
        query: str | None = None,
        include_closed: bool = False,
        limit: int = 200,
        order: Literal["priority", "recent"] = "priority",
    ) -> list[TaskSummary]:
        """List tasks. Closed tasks are hidden unless requested via ``statuses`` or ``include_closed``.

        ``order="recent"`` sorts by close time (else last update), newest first.
        """
        self.reap_expired_leases()
        where = ["t.project_id = ?"]
        params: list[Any] = [project_id]
        if statuses:
            where.append(f"t.status IN ({','.join('?' * len(statuses))})")
            params.extend(s.value for s in statuses)
        elif not include_closed:
            where.append(f"t.status NOT IN {_CLOSED_SQL}")
        if assignee is not None:
            where.append("t.assignee = ?")
            params.append(assignee)
        if parent is not None:
            where.append("t.parent_id = ?")
            params.append(self._row(parent, project_id)["id"])
        if task_type is not None:
            where.append("t.type = ?")
            params.append(_parse_type(task_type).value)
        if query and query.strip():
            clause, clause_params = _task_query_clause(query.strip())
            where.append(clause)
            params.extend(clause_params)
        sql = _TASK_SELECT + " WHERE " + " AND ".join(where)
        if ready_only:
            sql = f"SELECT * FROM ({sql}) WHERE ready"
        # Qualify columns unless wrapped: the parent join (pt) has the same column names.
        col = "" if ready_only else "t."
        if order == "recent":
            sql += f" ORDER BY COALESCE({col}closed_at, {col}updated_at) DESC, {col}seq DESC LIMIT ?"
        else:
            sql += f" ORDER BY {col}priority, {col}seq LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [_summary(r) for r in self.conn.execute(sql, params)]

    def project_status(self, project_id: int, *, ready_limit: int = 5) -> ProjectStatus:
        self.reap_expired_leases()
        project = self._project_by_id(project_id)
        counts = {s.value: 0 for s in Status}
        for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
            (project_id,),
        ):
            counts[r["status"]] = r["n"]
        ready = self.conn.execute(
            f"SELECT * FROM ({_TASK_SELECT} WHERE t.project_id = ? AND {_NO_OPEN_CHILDREN})"
            " WHERE ready ORDER BY priority, seq LIMIT ?",
            (project_id, ready_limit),
        ).fetchall()
        active = self.conn.execute(
            _TASK_SELECT + " WHERE t.project_id = ? AND t.status = 'in_progress' ORDER BY t.priority, t.seq",
            (project_id,),
        ).fetchall()
        blocked = self.conn.execute(
            _TASK_SELECT + " WHERE t.project_id = ? AND t.status = 'blocked' ORDER BY t.priority, t.seq",
            (project_id,),
        ).fetchall()
        blocked_items: list[BlockedItem] = []
        for r in blocked:
            note = self.conn.execute(
                "SELECT body FROM notes WHERE task_id = ? AND kind = 'blocker' ORDER BY id DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            blocked_items.append(BlockedItem(task=_summary(r), reason=note["body"] if note else None))
        return ProjectStatus(
            project=project,
            counts=counts,
            ready=[_summary(r) for r in ready],
            mine=[_task(r) for r in active if r["assignee"] == self.actor.id],
            in_progress=[_summary(r) for r in active if r["assignee"] != self.actor.id],
            blocked=blocked_items,
        )

    # ------------------------------------------------------------------ guards

    def _check_holder(self, row: sqlite3.Row) -> None:
        if self.actor.is_human:
            return
        if row["status"] == Status.IN_PROGRESS and row["assignee"] != self.actor.id:
            raise NotHolderError(f"{_ref(row)} is held by {row['assignee']}")

    @staticmethod
    def _check_version(row: sqlite3.Row, expected_version: int | None) -> None:
        if expected_version is not None and row["version"] != expected_version:
            raise ConflictError(
                f"{_ref(row)} changed (version {row['version']}, expected {expected_version}); "
                "re-read it and retry"
            )

    def _would_cycle_dep(self, task_id: int, depends_on_id: int) -> bool:
        """True if ``task_id`` is already reachable from ``depends_on_id``."""
        row = self.conn.execute(
            """WITH RECURSIVE reach(id) AS (
                 SELECT ? UNION
                 SELECT d.depends_on_id FROM task_deps d JOIN reach r ON d.task_id = r.id)
               SELECT 1 FROM reach WHERE id = ? LIMIT 1""",
            (depends_on_id, task_id),
        ).fetchone()
        return row is not None

    def _would_cycle_parent(self, task_id: int, parent_id: int) -> bool:
        """True if ``task_id`` is ``parent_id`` or one of its ancestors."""
        row = self.conn.execute(
            """WITH RECURSIVE up(id) AS (
                 SELECT ? UNION
                 SELECT t.parent_id FROM tasks t JOIN up ON t.id = up.id
                 WHERE t.parent_id IS NOT NULL)
               SELECT 1 FROM up WHERE id = ? LIMIT 1""",
            (parent_id, task_id),
        ).fetchone()
        return row is not None

    def _add_dep(self, task: sqlite3.Row, dep: sqlite3.Row) -> None:
        if dep["project_id"] != task["project_id"]:
            raise ValidationError(f"{_ref(task)} cannot depend on {_ref(dep)} in another project")
        if dep["id"] == task["id"] or self._would_cycle_dep(task["id"], dep["id"]):
            raise CycleError(f"{_ref(task)} -> {_ref(dep)} would create a dependency cycle")
        self.conn.execute(
            "INSERT OR IGNORE INTO task_deps (task_id, depends_on_id) VALUES (?,?)",
            (task["id"], dep["id"]),
        )

    # ------------------------------------------------------------------ writes

    def create_tasks(self, project_id: int, items: Sequence[NewTask]) -> list[Task]:
        """Create tasks atomically. Later items may reference earlier ``local_id`` values."""
        if not items:
            raise ValidationError("No tasks given")
        local: dict[str, int] = {}
        created: list[int] = []
        with self._write():
            self._project_by_id(project_id)
            self._check_project_open(project_id)
            for item in items:
                if item.local_id is not None:
                    if _looks_like_ref(item.local_id):
                        raise ValidationError(
                            f"local_id {item.local_id!r} looks like a task ref; use a word"
                        )
                    if item.local_id in local:
                        raise ValidationError(f"Duplicate local_id {item.local_id!r}")
                try:
                    priority = Priority.parse(item.priority)
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                task_type = _parse_type(item.type)

                parent_id: int | None = None
                if item.parent:
                    parent = self._resolve_in_batch(item.parent, project_id, local)
                    if parent["project_id"] != project_id:
                        raise ValidationError("Parent must be in the same project")
                    parent_id = parent["id"]

                seq: int = self.conn.execute(
                    "UPDATE projects SET next_seq = next_seq + 1 WHERE id = ? RETURNING next_seq - 1",
                    (project_id,),
                ).fetchone()[0]
                now = self._now()
                cur = self.conn.execute(
                    """INSERT INTO tasks (project_id, seq, parent_id, title, description, acceptance,
                         priority, type, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project_id,
                        seq,
                        parent_id,
                        item.title.strip(),
                        item.description,
                        item.acceptance,
                        int(priority),
                        task_type.value,
                        self.actor.id,
                        now,
                        now,
                    ),
                )
                task_id = cur.lastrowid
                assert task_id is not None
                task_row = self._row_by_id(task_id)
                for dep_ref in item.depends_on:
                    self._add_dep(task_row, self._resolve_in_batch(dep_ref, project_id, local))
                if item.local_id is not None:
                    local[item.local_id] = task_id
                created.append(task_id)
                self._emit("task_created", project_id, task_id, {"title": item.title})
        return [_task(self._row_by_id(i)) for i in created]

    def _resolve_in_batch(
        self, ref: str, project_id: int, local: dict[str, int]
    ) -> sqlite3.Row:
        if ref in local:
            return self._row_by_id(local[ref])
        return self._row(ref, project_id)

    def claim_task(
        self,
        project_id: int,
        ref: str | int | None = None,
        *,
        parent: str | None = None,
        max_priority: int | str | None = None,
    ) -> Task | None:
        """Claim ``ref``, or the highest-priority ready leaf task. Returns None if nothing is ready.

        Re-claiming a task you already hold just renews its lease.
        """
        if self.actor.is_human:
            raise ValidationError("Humans do not claim tasks; set status in the dashboard instead")
        with self._write():
            if ref is not None:
                row = self._row(ref, project_id)
                self._check_project_open(row["project_id"])
                if row["status"] == Status.IN_PROGRESS:
                    if row["assignee"] == self.actor.id:
                        return _task(self._row_by_id(row["id"]))  # lease renewed by _touch
                    raise NotHolderError(f"{_ref(row)} is already held by {row['assignee']}")
                if row["status"] != Status.TODO:
                    raise InvalidTransitionError(
                        f"{_ref(row)} is {row['status']}; only todo tasks can be claimed"
                    )
                if not row["ready"]:
                    open_deps = [
                        _ref(r)
                        for r in self.conn.execute(
                            _TASK_SELECT
                            + f" JOIN task_deps d ON d.depends_on_id = t.id"
                            f" WHERE d.task_id = ? AND t.status NOT IN {_CLOSED_SQL}",
                            (row["id"],),
                        )
                    ]
                    raise ValidationError(
                        f"{_ref(row)} is waiting on open dependencies: {', '.join(open_deps)}"
                    )
            else:
                self._check_project_open(project_id)
                where = ["t.project_id = ?", _NO_OPEN_CHILDREN]
                params: list[Any] = [project_id]
                if parent is not None:
                    where.append("t.parent_id = ?")
                    params.append(self._row(parent, project_id)["id"])
                if max_priority is not None:
                    try:
                        where.append("t.priority <= ?")
                        params.append(int(Priority.parse(max_priority)))
                    except ValueError as exc:
                        raise ValidationError(str(exc)) from exc
                row = self.conn.execute(
                    f"SELECT * FROM ({_TASK_SELECT} WHERE {' AND '.join(where)})"
                    " WHERE ready ORDER BY priority, seq LIMIT 1",
                    params,
                ).fetchone()
                if row is None:
                    return None

            # BEGIN IMMEDIATE holds the write lock, so the status guard below cannot
            # lose a race; it is kept as a defensive check.
            cur = self.conn.execute(
                """UPDATE tasks SET status = 'in_progress', assignee = ?, lease_expires_at = ?,
                     version = version + 1, updated_at = ?
                   WHERE id = ? AND status = 'todo'""",
                (self.actor.id, self._lease_deadline(), self._now(), row["id"]),
            )
            if cur.rowcount != 1:
                raise ConflictError(f"{_ref(row)} was claimed by someone else")
            # Remember where the agent works so dashboards can list it per project.
            self.conn.execute(
                "UPDATE agents SET project_id = ? WHERE id = ?", (row["project_id"], self.actor.id)
            )
            self._emit("task_claimed", row["project_id"], row["id"], {"from": row["status"]})
            return _task(self._row_by_id(row["id"]))

    def update_task(
        self,
        project_id: int | None,
        ref: str | int,
        update: TaskUpdate,
        *,
        expected_version: int | None = None,
    ) -> Task:
        with self._write():
            row = self._row(ref, project_id)
            self._check_project_open(row["project_id"])
            self._check_holder(row)
            self._check_version(row, expected_version)
            now = self._now()
            sets: dict[str, Any] = {}
            changed: list[str] = []

            for field in ("title", "description", "acceptance"):
                value: str | None = getattr(update, field)
                if value is not None and value != row[field]:
                    sets[field] = value.strip() if field == "title" else value
                    changed.append(field)
            if update.priority is not None:
                try:
                    priority = int(Priority.parse(update.priority))
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                if priority != row["priority"]:
                    sets["priority"] = priority
                    changed.append("priority")
            if update.type is not None:
                task_type = _parse_type(update.type)
                if task_type.value != row["type"]:
                    sets["type"] = task_type.value
                    changed.append("type")
            if update.clear_parent and update.parent is not None:
                raise ValidationError("Use parent or clear_parent, not both")
            if update.parent == "":
                # The old way to clear a parent. Refuse it so it is not read as a ref.
                raise ValidationError("parent must be a task ref; use clear_parent to remove it")
            if update.clear_parent or update.parent is not None:
                if update.parent is None:
                    new_parent: int | None = None
                else:
                    parent_row = self._row(update.parent, row["project_id"])
                    if parent_row["project_id"] != row["project_id"]:
                        raise ValidationError("Parent must be in the same project")
                    if self._would_cycle_parent(row["id"], parent_row["id"]):
                        raise CycleError(f"{_ref(parent_row)} cannot be a parent of {_ref(row)}")
                    new_parent = parent_row["id"]
                if new_parent != row["parent_id"]:
                    sets["parent_id"] = new_parent
                    changed.append("parent")

            transition: tuple[str, str] | None = None
            if update.status is not None and update.status != row["status"]:
                sets.update(self._transition_fields(row, update.status, now))
                transition = (row["status"], update.status.value)

            for dep_ref in update.add_deps:
                self._add_dep(row, self._row(dep_ref, row["project_id"]))
            for dep_ref in update.remove_deps:
                dep = self._row(dep_ref, row["project_id"])
                self.conn.execute(
                    "DELETE FROM task_deps WHERE task_id = ? AND depends_on_id = ?",
                    (row["id"], dep["id"]),
                )
            if update.add_deps or update.remove_deps:
                changed.append("deps")

            if not sets and not changed:
                return _task(row)
            sets["updated_at"] = now
            assignments = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(
                f"UPDATE tasks SET {assignments}, version = version + 1 WHERE id = ?",
                (*sets.values(), row["id"]),
            )
            if transition:
                self._emit(
                    "status_changed", row["project_id"], row["id"],
                    {"from": transition[0], "to": transition[1]},
                )
            if changed:
                self._emit("task_updated", row["project_id"], row["id"], {"fields": changed})
            return _task(self._row_by_id(row["id"]))

    def _transition_fields(self, row: sqlite3.Row, to: Status, now: str) -> dict[str, Any]:
        """Validate a status change and return the columns it sets."""
        current = Status(row["status"])
        if not self.actor.is_human:
            if to == Status.IN_PROGRESS:
                raise InvalidTransitionError("Use claim_task to start work on a task")
            if to not in AGENT_TRANSITIONS[current]:
                raise InvalidTransitionError(f"Cannot move {_ref(row)} from {current} to {to}")
            if to == Status.DONE:
                self._check_no_open_children(row)
        fields: dict[str, Any] = {"status": to.value}
        if to == Status.IN_PROGRESS:
            # Only humans reach here. Human work has no lease and never expires.
            fields.update(assignee=HUMAN, lease_expires_at=None)
        else:
            fields.update(assignee=None, lease_expires_at=None)
        if to.is_closed:
            fields["closed_at"] = now
        elif current.is_closed:
            fields["closed_at"] = None
        return fields

    def _check_no_open_children(self, row: sqlite3.Row) -> None:
        open_children = self.conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE parent_id = ? AND status NOT IN {_CLOSED_SQL}",
            (row["id"],),
        ).fetchone()[0]
        if open_children:
            raise InvalidTransitionError(f"{_ref(row)} has {open_children} open subtask(s)")

    def add_note(
        self, project_id: int | None, ref: str | int, kind: NoteKind | str, body: str
    ) -> Note:
        """Notes are open to everyone (not only the holder): they carry context between agents."""
        try:
            note_kind = NoteKind(kind)
        except ValueError as exc:
            raise ValidationError(
                f"Invalid note kind {kind!r}; use one of {', '.join(k.value for k in NoteKind)}"
            ) from exc
        if not body.strip():
            raise ValidationError("Note body is empty")
        with self._write():
            row = self._row(ref, project_id)
            self._check_project_open(row["project_id"])
            note_id = self._insert_note(row, note_kind, body)
            note = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            return _note(note, _ref(row))

    def _insert_note(self, row: sqlite3.Row, kind: NoteKind, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO notes (task_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
            (row["id"], self.actor.id, kind.value, body.strip(), self._now()),
        )
        assert cur.lastrowid is not None
        self._emit("note_added", row["project_id"], row["id"], {"kind": kind.value})
        return cur.lastrowid

    def add_links(
        self, project_id: int | None, ref: str | int, links: Iterable[tuple[LinkKind | str, str]]
    ) -> list[Link]:
        with self._write():
            row = self._row(ref, project_id)
            self._check_project_open(row["project_id"])
            self._insert_links(row, links)
            rows = self.conn.execute(
                "SELECT * FROM links WHERE task_id = ? ORDER BY id", (row["id"],)
            ).fetchall()
            return [_link(r, _ref(row)) for r in rows]

    def _insert_links(
        self, row: sqlite3.Row, links: Iterable[tuple[LinkKind | str, str]]
    ) -> None:
        for kind, value in links:
            try:
                link_kind = LinkKind(kind)
            except ValueError as exc:
                raise ValidationError(
                    f"Invalid link kind {kind!r}; use one of {', '.join(k.value for k in LinkKind)}"
                ) from exc
            if not value.strip():
                raise ValidationError("Link ref is empty")
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO links (task_id, kind, ref, created_at) VALUES (?,?,?,?)",
                (row["id"], link_kind.value, value.strip(), self._now()),
            )
            if cur.rowcount:
                self._emit(
                    "link_added", row["project_id"], row["id"],
                    {"kind": link_kind.value, "ref": value.strip()},
                )

    def complete_task(
        self,
        project_id: int | None,
        ref: str | int,
        summary: str,
        *,
        links: Iterable[tuple[LinkKind | str, str]] = (),
        to_review: bool = False,
        expected_version: int | None = None,
    ) -> Task:
        """Finish a held task: write a summary note, add links, move to done (or review)."""
        if not summary.strip():
            raise ValidationError("A completion summary is required")
        target = Status.REVIEW if to_review else Status.DONE
        with self._write():
            row = self._row(ref, project_id)
            self._check_project_open(row["project_id"])
            self._check_version(row, expected_version)
            if not self.actor.is_human:
                if row["status"] != Status.IN_PROGRESS or row["assignee"] != self.actor.id:
                    raise NotHolderError(
                        f"You do not hold {_ref(row)} (status {row['status']}); claim it first"
                    )
            if target == Status.DONE:
                self._check_no_open_children(row)
            now = self._now()
            self._insert_note(row, NoteKind.SUMMARY, summary)
            self._insert_links(row, links)
            closed_at = now if target == Status.DONE else None
            self.conn.execute(
                """UPDATE tasks SET status = ?, assignee = NULL, lease_expires_at = NULL,
                     closed_at = ?, version = version + 1, updated_at = ? WHERE id = ?""",
                (target.value, closed_at, now, row["id"]),
            )
            self._emit(
                "status_changed", row["project_id"], row["id"],
                {"from": row["status"], "to": target.value},
            )
            return _task(self._row_by_id(row["id"]))

    def release_task(self, project_id: int | None, ref: str | int, reason: str) -> Task:
        """Give a held task back to the queue with a handoff note."""
        if not reason.strip():
            raise ValidationError("A release reason is required (it becomes a handoff note)")
        with self._write():
            row = self._row(ref, project_id)
            self._check_project_open(row["project_id"])
            if row["status"] != Status.IN_PROGRESS:
                raise InvalidTransitionError(f"{_ref(row)} is {row['status']}, not in_progress")
            self._check_holder(row)
            self._insert_note(row, NoteKind.HANDOFF, reason)
            self.conn.execute(
                """UPDATE tasks SET status = 'todo', assignee = NULL, lease_expires_at = NULL,
                     version = version + 1, updated_at = ? WHERE id = ?""",
                (self._now(), row["id"]),
            )
            self._emit(
                "status_changed", row["project_id"], row["id"],
                {"from": "in_progress", "to": "todo", "released": True},
            )
            return _task(self._row_by_id(row["id"]))

    # ------------------------------------------------------------------ events

    def events_since(
        self, after_id: int = 0, *, project_id: int | None = None, limit: int = 100
    ) -> list[Event]:
        """Events with id > ``after_id``, oldest first (for streaming)."""
        return self._events(after_id, project_id, limit, newest_first=False)

    def events_recent(self, *, project_id: int | None = None, limit: int = 30) -> list[Event]:
        """The latest ``limit`` events, newest first (for activity feeds)."""
        return self._events(0, project_id, limit, newest_first=True)

    def _events(
        self, after_id: int, project_id: int | None, limit: int, *, newest_first: bool
    ) -> list[Event]:
        sql = """SELECT e.*, p.key AS pkey, t.seq AS task_seq FROM events e
                 LEFT JOIN tasks t ON t.id = e.task_id
                 LEFT JOIN projects p ON p.id = t.project_id
                 WHERE e.id > ?"""
        params: list[Any] = [after_id]
        if project_id is not None:
            sql += " AND e.project_id = ?"
            params.append(project_id)
        sql += f" ORDER BY e.id {'DESC' if newest_first else 'ASC'} LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [
            Event(
                id=r["id"],
                project_id=r["project_id"],
                task=format_ref(r["pkey"], r["task_seq"]) if r["task_seq"] is not None else None,
                agent=r["agent"],
                type=r["type"],
                payload=json.loads(r["payload"]),
                ts=r["ts"],
            )
            for r in self.conn.execute(sql, params)
        ]

    def latest_event_id(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])


class _WriteTxn:
    """Context manager: BEGIN IMMEDIATE, reap expired leases, record agent liveness."""

    def __init__(self, service: TaskService) -> None:
        self._service = service
        self._cm = transaction(service.conn)

    def __enter__(self) -> sqlite3.Connection:
        if self._service.actor.is_anonymous:
            raise IdentityRequiredError(
                "No identity: pass --as <tool>#<id> (e.g. --as codex#7f2a) or set DELEGATE_AGENT. "
                "People use --as human."
            )
        conn = self._cm.__enter__()
        try:
            self._service._reap()  # pyright: ignore[reportPrivateUsage]
            self._service._touch()  # pyright: ignore[reportPrivateUsage]
        except BaseException as exc:
            self._cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return conn

    def __exit__(self, *exc_info: Any) -> bool | None:
        return self._cm.__exit__(*exc_info)


# ---------------------------------------------------------------------- row mappers


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so user search text matches literally (use with ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _task_query_clause(query: str) -> tuple[str, list[Any]]:
    """SQL condition for ``list_tasks(query=...)`` on the ``t``/``p`` aliases of ``_TASK_SELECT``.

    Matches the text (case-insensitive for ASCII, as SQLite LIKE) in the title, description,
    acceptance criteria, note bodies and link refs. A query that is a task ref (``DLG-25``,
    ``25``, ``#25``) also matches that task; a ref with another project key matches no seq.
    """
    like = f"%{_escape_like(query)}%"
    parts = [
        "t.title LIKE ? ESCAPE '\\'",
        "t.description LIKE ? ESCAPE '\\'",
        "t.acceptance LIKE ? ESCAPE '\\'",
        "EXISTS (SELECT 1 FROM notes n WHERE n.task_id = t.id AND n.body LIKE ? ESCAPE '\\')",
        "EXISTS (SELECT 1 FROM links l WHERE l.task_id = t.id AND l.ref LIKE ? ESCAPE '\\')",
    ]
    params: list[Any] = [like] * len(parts)
    if _looks_like_ref(query):
        key, seq = parse_ref(query)
        if key is None:
            parts.append("t.seq = ?")
            params.append(seq)
        else:
            parts.append("(p.key = ? AND t.seq = ?)")
            params.extend([key, seq])
    return "(" + " OR ".join(parts) + ")", params


def _looks_like_ref(value: str) -> bool:
    try:
        parse_ref(value)
    except ValidationError:
        return False
    return True


def _ref(row: sqlite3.Row) -> str:
    return format_ref(row["pkey"], row["seq"])


def _parent_ref(row: sqlite3.Row) -> str | None:
    return format_ref(row["pkey"], row["parent_seq"]) if row["parent_seq"] is not None else None


def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        root_path=row["root_path"],
        git_remote=row["git_remote"],
        state=ProjectState(row["state"]),
        created_at=row["created_at"],
    )


def _parse_type(value: TaskType | str) -> TaskType:
    """Parse a task type (any case). Raises ValidationError with the allowed values."""
    try:
        return TaskType(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(t.value for t in TaskType)
        raise ValidationError(f"Invalid task type {value!r}; use one of {allowed}") from exc


def _summary(row: sqlite3.Row) -> TaskSummary:
    return TaskSummary(
        ref=_ref(row),
        title=row["title"],
        type=TaskType(row["type"]),
        status=Status(row["status"]),
        priority=Priority(row["priority"]),
        assignee=row["assignee"],
        parent=_parent_ref(row),
        ready=bool(row["ready"]),
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        ref=_ref(row),
        project_id=row["project_id"],
        seq=row["seq"],
        parent=_parent_ref(row),
        title=row["title"],
        description=row["description"],
        acceptance=row["acceptance"],
        type=TaskType(row["type"]),
        status=Status(row["status"]),
        priority=Priority(row["priority"]),
        assignee=row["assignee"],
        lease_expires_at=row["lease_expires_at"],
        version=row["version"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
    )


def _note(row: sqlite3.Row, task_ref: str) -> Note:
    return Note(
        id=row["id"],
        task=task_ref,
        author=row["author"],
        kind=NoteKind(row["kind"]),
        body=row["body"],
        created_at=row["created_at"],
    )


def _link(row: sqlite3.Row, task_ref: str) -> Link:
    return Link(
        id=row["id"],
        task=task_ref,
        kind=LinkKind(row["kind"]),
        ref=row["ref"],
        created_at=row["created_at"],
    )


__all__ = ["CLOSED_STATUSES", "NewTask", "TaskService", "TaskUpdate", "new_agent_id"]
