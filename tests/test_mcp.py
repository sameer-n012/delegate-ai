"""MCP adapter tests, run through the real protocol with the SDK's in-memory client."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from delegate.core import Project, Settings, TaskService
from delegate.mcp.server import build_server
from delegate.runtime import Runtime

T = TypeVar("T")

EXPECTED_TOOLS = {
    "project_status",
    "list_tasks",
    "get_task",
    "create_tasks",
    "claim_task",
    "update_task",
    "add_note",
    "complete_task",
    "release_task",
}


@pytest.fixture
def project(make_service: Callable[[str], TaskService], tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    human = make_service("human")
    return human.set_project_state(human.ensure_project(root, key="DLG").id, "in_progress")


def make_server(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project, agent: str
) -> MCPServer:
    settings = Settings(db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    runtime = Runtime(settings=settings, conn=conn, service=make_service(agent))
    return build_server(runtime, project, agent)


async def call(server: MCPServer, name: str, **args: Any) -> tuple[bool, str]:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
    text = "\n".join(getattr(block, "text", "") for block in result.content)
    return result.is_error, text


def run(fn: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(fn)


def test_lists_nine_tools(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project
) -> None:
    server = make_server(make_service, conn, project, "claude#aaaa")

    async def go() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert run(go) == EXPECTED_TOOLS
    assert server.instructions


def test_batch_claim_note_complete_flow(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project
) -> None:
    server = make_server(make_service, conn, project, "claude#aaaa")

    async def go() -> list[tuple[bool, str]]:
        out: list[tuple[bool, str]] = []
        out.append(
            await call(
                server,
                "create_tasks",
                tasks=[
                    {"title": "Epic", "local_id": "epic", "priority": "P1"},
                    {"title": "First", "parent": "epic", "local_id": "a"},
                    {"title": "Second", "parent": "epic", "depends_on": ["a"]},
                ],
            )
        )
        out.append(await call(server, "claim_task"))
        out.append(await call(server, "add_note", ref="DLG-2", kind="decision", body="use sqlite"))
        out.append(await call(server, "project_status"))
        out.append(
            await call(
                server,
                "complete_task",
                ref="DLG-2",
                summary="done it",
                links=[{"kind": "commit", "ref": "abc123"}],
            )
        )
        out.append(await call(server, "get_task", ref="DLG-2"))
        out.append(await call(server, "list_tasks", ready_only=True))
        return out

    created, claimed, note, status, done, detail, ready = run(go)
    assert not created[0] and "DLG-1 Epic" in created[1] and "DLG-3 Second" in created[1]
    assert not claimed[0] and "Claimed." in claimed[1] and "DLG-2" in claimed[1]
    assert not note[0]
    assert "Yours" in status[1] and "DLG-2" in status[1] and "claude#aaaa" in status[1]
    assert "Ready" not in status[1]  # DLG-3 waits on DLG-2, epic is not a leaf
    assert not done[0] and "done" in done[1]
    assert "use sqlite" in detail[1] and "commit:abc123" in detail[1] and "version=" in detail[1]
    assert "DLG-3" in ready[1]


def test_claim_held_by_other_is_tool_error(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project
) -> None:
    a = make_server(make_service, conn, project, "claude#aaaa")
    b = make_server(make_service, conn, project, "claude#bbbb")

    async def go() -> tuple[tuple[bool, str], tuple[bool, str], tuple[bool, str]]:
        await call(a, "create_tasks", tasks=[{"title": "One"}])
        await call(a, "claim_task", ref="DLG-1")
        return (
            await call(b, "claim_task", ref="DLG-1"),
            await call(b, "complete_task", ref="DLG-1", summary="x"),
            await call(b, "get_task", ref="DLG-99"),
        )

    claim, complete, missing = run(go)
    assert claim[0] and "not_holder" in claim[1] and "claude#aaaa" in claim[1]
    assert complete[0] and "not_holder" in complete[1]
    assert missing[0] and "not_found" in missing[1]


def test_no_ready_tasks_and_bad_input(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project
) -> None:
    server = make_server(make_service, conn, project, "claude#aaaa")

    async def go() -> tuple[tuple[bool, str], tuple[bool, str], tuple[bool, str]]:
        return (
            await call(server, "claim_task"),
            await call(server, "list_tasks", status=["bogus"]),
            await call(server, "project_status", project="NOPE"),
        )

    empty, bad_status, bad_project = run(go)
    assert not empty[0] and "No ready tasks" in empty[1]
    assert bad_status[0] and "invalid" in bad_status[1]
    assert bad_project[0] and "not_found" in bad_project[1]


def test_update_task_and_version_conflict(
    make_service: Callable[[str], TaskService], conn: sqlite3.Connection, project: Project
) -> None:
    server = make_server(make_service, conn, project, "claude#aaaa")

    async def go() -> list[tuple[bool, str]]:
        await call(server, "create_tasks", tasks=[{"title": "One"}, {"title": "Two", "type": "bug"}])
        return [
            await call(
                server, "update_task", ref="DLG-1", title="Uno", priority="P0", type="feature",
                add_deps=["DLG-2"],
            ),
            await call(server, "update_task", ref="DLG-1", title="X", expected_version=99),
            await call(server, "update_task", ref="DLG-1", status="in_progress"),
            await call(server, "list_tasks"),
            await call(server, "get_task", ref="DLG-1"),
        ]

    ok, conflict, transition, listing, detail = run(go)
    assert not ok[0] and "Uno" in ok[1] and "P0" in ok[1]
    # Typed tasks show their type in lists; unknown is left out to save context.
    assert "DLG-2 [P2 todo bug] Two" in listing[1] and "[P0 todo feature]" in listing[1]
    assert "type=feature" in detail[1]
    assert conflict[0] and "conflict" in conflict[1]
    assert transition[0] and "invalid_transition" in transition[1]
