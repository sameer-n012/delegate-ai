"""Numbered schema migrations. Append new entries; never edit an applied one.

Timestamps are ISO-8601 UTC strings with millisecond precision (see ``clock.now_iso``),
so lexical comparison in SQL matches chronological order.
"""

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        key         TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL,
        root_path   TEXT NOT NULL UNIQUE,
        git_remote  TEXT,
        next_seq    INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    );
    CREATE UNIQUE INDEX projects_git_remote ON projects(git_remote) WHERE git_remote IS NOT NULL;

    CREATE TABLE agents (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        label           TEXT,
        cwd             TEXT,
        branch          TEXT,
        project_id      INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        created_at      TEXT NOT NULL,
        last_heartbeat  TEXT NOT NULL
    );

    CREATE TABLE tasks (
        id                INTEGER PRIMARY KEY,
        project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        seq               INTEGER NOT NULL,
        parent_id         INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        title             TEXT NOT NULL,
        description       TEXT NOT NULL DEFAULT '',
        acceptance        TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'todo'
                          CHECK (status IN ('todo','in_progress','review','blocked','done','cancelled')),
        priority          INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 3),
        assignee          TEXT,
        lease_expires_at  TEXT,
        version           INTEGER NOT NULL DEFAULT 1,
        created_by        TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        closed_at         TEXT,
        UNIQUE (project_id, seq)
    );
    CREATE INDEX tasks_queue ON tasks(project_id, status, priority, seq);
    CREATE INDEX tasks_parent ON tasks(parent_id);
    CREATE INDEX tasks_lease ON tasks(lease_expires_at) WHERE lease_expires_at IS NOT NULL;

    CREATE TABLE task_deps (
        task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        depends_on_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        PRIMARY KEY (task_id, depends_on_id),
        CHECK (task_id != depends_on_id)
    );
    CREATE INDEX task_deps_reverse ON task_deps(depends_on_id);

    CREATE TABLE notes (
        id          INTEGER PRIMARY KEY,
        task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        author      TEXT NOT NULL,
        kind        TEXT NOT NULL
                    CHECK (kind IN ('progress','decision','blocker','handoff','summary')),
        body        TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX notes_task ON notes(task_id, id);

    CREATE TABLE links (
        id          INTEGER PRIMARY KEY,
        task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('file','commit','pr','url')),
        ref         TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (task_id, kind, ref)
    );

    CREATE TABLE events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        task_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        agent       TEXT,
        type        TEXT NOT NULL,
        payload     TEXT NOT NULL DEFAULT '{}',
        ts          TEXT NOT NULL
    );
    CREATE INDEX events_project ON events(project_id, id);
    """,
]
