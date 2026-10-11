"""Shared fixtures: a temp DB, a fake clock, and a project rooted in a temp dir."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from delegate.core import Project, TaskService, connect


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "delegate.db"


@pytest.fixture
def conn(db_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


ServiceFactory = Callable[[str], TaskService]


@pytest.fixture
def make_service(conn: sqlite3.Connection, clock: FakeClock) -> ServiceFactory:
    """Build a service for a given actor on the shared connection and fake clock."""

    def factory(actor: str) -> TaskService:
        return TaskService(conn, actor, lease_minutes=15, clock=clock)

    return factory


@pytest.fixture
def project(make_service: ServiceFactory, tmp_path: Path) -> Project:
    root = tmp_path / "delegate-ai"
    root.mkdir()
    return make_service("human").ensure_project(root)
