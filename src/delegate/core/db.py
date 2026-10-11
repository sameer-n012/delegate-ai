"""SQLite connection setup, migrations, and transactions.

Connections run in autocommit mode (``isolation_level=None``); writes go through
``transaction()``, which issues ``BEGIN IMMEDIATE``. That takes the write lock up
front, so two processes cannot both read "task is free" and then both claim it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from delegate.core.migrations import MIGRATIONS

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the DB, enable WAL and foreign keys, and apply pending migrations.

    ``":memory:"`` is accepted for tests (WAL is skipped there).
    """
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the web app hands connections to a threadpool. Each
    # connection is still used by one thread at a time.
    conn = sqlite3.connect(
        str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run a block in a ``BEGIN IMMEDIATE`` transaction. Nested use joins the outer one."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    """Apply migrations newer than ``PRAGMA user_version``. Safe to run concurrently."""
    # Fast path without the write lock: most connections find the schema current.
    if conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS):
        return
    with transaction(conn):
        # Re-read under the lock: another process may have migrated meanwhile.
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"DB schema version {current} is newer than this delegate build "
                f"({len(MIGRATIONS)}). Upgrade delegate."
            )
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            # executescript() would COMMIT our transaction, so run statements one by one.
            for statement in _split_sql(script):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements. Migrations must not use ';' inside literals."""
    return [s.strip() for s in script.split(";") if s.strip()]
