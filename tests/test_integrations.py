"""Repo setup (init), Claude Code hook logic, and commit linking."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from delegate.core import NewTask, Project, TaskService
from delegate.integrations import hooks
from delegate.integrations.setup import (
    BEGIN_MARK,
    END_MARK,
    init_repo,
    upsert_marked_block,
)

from .conftest import ServiceFactory


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return out.stdout


def test_upsert_marked_block_replaces_in_place() -> None:
    text = "# Title\n\nintro\n"
    once = upsert_marked_block(text, f"{BEGIN_MARK}\nv1\n{END_MARK}\n", BEGIN_MARK, END_MARK)
    assert once.startswith("# Title\n\nintro\n\n<!--")
    twice = upsert_marked_block(once + "tail\n", f"{BEGIN_MARK}\nv2\n{END_MARK}\n", BEGIN_MARK, END_MARK)
    assert "v1" not in twice and "v2" in twice and twice.endswith("tail\n")


def test_init_repo_is_idempotent_and_merges(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "CLAUDE.md").write_text("# Existing rules\n")
    (repo / ".claude").mkdir()
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}, "x": 1}
    (repo / ".claude" / "settings.json").write_text(json.dumps(existing))
    project = make_service("human").ensure_project(repo)

    first = init_repo(repo, project, command=["delegate"])
    second = init_repo(repo, project, command=["delegate"])

    claude_md = (repo / "CLAUDE.md").read_text()
    assert claude_md.startswith("# Existing rules") and claude_md.count(BEGIN_MARK) == 1
    assert f"{project.key}-42" in claude_md
    mcp = json.loads((repo / ".mcp.json").read_text())
    assert mcp["mcpServers"]["delegate"] == {"type": "stdio", "command": "delegate", "args": ["mcp"]}
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["x"] == 1
    stop_cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds == ["echo hi", "delegate hook stop"]
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "delegate hook session-start"
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.read_text().count("hook post-commit") == 1
    assert ".mcp.json" in first.changed and ".mcp.json" in second.unchanged


def test_session_start_context(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="Write docs", priority=1)])
    text = hooks.session_start_context(make_service("human"), project)
    assert "DLG-1 P1 Write docs" in text and "claim_task" in text


def test_stop_decision_blocks_once_for_tasks_held_in_cwd(
    make_service: ServiceFactory, project: Project
) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    agent = make_service("claude#aaaa")
    agent.register_agent("claude", cwd="/work/a", project_id=project.id)
    agent.claim_task(project.id)
    viewer = make_service("human")

    decision = hooks.stop_decision(viewer, project, {"cwd": "/work/a"})
    assert decision is not None and decision["decision"] == "block" and "DLG-1" in decision["reason"]
    assert hooks.stop_decision(viewer, project, {"cwd": "/work/a", "stop_hook_active": True}) is None
    assert hooks.stop_decision(viewer, project, {"cwd": "/elsewhere"}) is None


def test_refs_in_message() -> None:
    msg = "Fix lease bug (DLG-3, DLG-12)\n\nAlso DLG-3 and OTHER-4, dlg-5"
    assert hooks.refs_in_message(msg, "DLG") == ["DLG-3", "DLG-12"]


def test_link_commit_from_head(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    svc: TaskService = make_service("human")
    project = svc.ensure_project(repo)
    svc.create_tasks(project.id, [NewTask(title="A")])
    _git(repo, "commit", "--allow-empty", "-m", f"Implement {project.key}-1 and {project.key}-99")

    head = hooks.head_commit(repo)
    assert head is not None
    linked = hooks.link_commit(svc, project, *head)
    assert linked == [f"{project.key}-1"]  # unknown ref skipped, not an error
    links = svc.get_task_detail(f"{project.key}-1").links
    assert links[0].kind == "commit" and links[0].ref == head[0]
