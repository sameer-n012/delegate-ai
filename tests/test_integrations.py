"""Repo setup (init), Claude Code hook logic, and commit linking."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from delegate.core import NewTask, Project, TaskService, connect
from delegate.integrations import hooks
from delegate.integrations.setup import (
    BEGIN_MARK,
    END_MARK,
    InitOptions,
    apply_setup,
    detect_options,
    init_repo,
    is_newer,
    setup_info,
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
    existing = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "permissions": {"allow": ["Bash(ls:*)"]},
        "x": 1,
    }
    (repo / ".claude" / "settings.json").write_text(json.dumps(existing))
    project = make_service("human").ensure_project(repo)

    first = init_repo(repo, project, InitOptions())
    second = init_repo(repo, project, InitOptions())

    claude_md = (repo / "CLAUDE.md").read_text()
    assert claude_md.startswith("# Existing rules") and claude_md.count(BEGIN_MARK) == 1
    assert f"{project.key}-42" in claude_md
    mcp = json.loads((repo / ".mcp.json").read_text())
    assert mcp["mcpServers"]["delegate"] == {"type": "stdio", "command": "delegate", "args": ["mcp"]}
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["x"] == 1
    assert settings["permissions"]["allow"] == ["Bash(ls:*)", "mcp__delegate"]
    assert "--as" in claude_md and "preferred" in claude_md
    assert ".claude/settings.json (permissions)" in second.unchanged
    stop_cmds = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds == ["echo hi"]
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "delegate hook session-start"
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.read_text().count("hook post-commit") == 1
    assert ".mcp.json" in first.changed and ".mcp.json" in second.unchanged
    assert str(hook) in first.changed and str(hook) in second.unchanged


def test_session_start_context(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="Write docs", priority=1)])
    text = hooks.session_start_context(make_service("human"), project)
    assert "DLG-1 P1 Write docs" in text and "claim_task" in text


def test_init_repo_removes_old_stop_hook(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".claude").mkdir()
    old = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "uv run delegate hook stop"}]},
                {
                    "hooks": [
                        {"type": "command", "command": "echo hi"},
                        {"type": "command", "command": "delegate hook stop"},
                    ]
                },
            ]
        }
    }
    (repo / ".claude" / "settings.json").write_text(json.dumps(old))
    project = make_service("human").ensure_project(repo)

    result = init_repo(repo, project, InitOptions(mcp=False, git_hook=False))

    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "echo hi"}]}]
    assert ".claude/settings.json (hooks)" in result.changed

    # When only delegate's Stop hook was present, the Stop key goes away.
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "delegate hook stop"}]}]}})
    )
    init_repo(repo, project, InitOptions(mcp=False, git_hook=False))
    assert "Stop" not in json.loads((repo / ".claude" / "settings.json").read_text())["hooks"]


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


def test_post_commit_hook_uses_hook_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from delegate.cli.main import app

    repo = tmp_path / "hookrepo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    monkeypatch.setenv("DELEGATE_DB", str(tmp_path / "hook.db"))
    monkeypatch.setenv("DELEGATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DELEGATE_AGENT", raising=False)
    monkeypatch.chdir(repo)
    human = TaskService(connect(tmp_path / "hook.db"), "human")
    project = human.set_project_state(human.ensure_project(repo).id, "in_progress")
    human.create_tasks(project.id, [NewTask(title="A")])
    _git(repo, "commit", "--allow-empty", "-m", f"Work on {project.key}-1")

    result = CliRunner().invoke(app, ["hook", "post-commit"])
    assert result.exit_code == 0, result.output
    assert human.get_task_detail(f"{project.key}-1").links[0].kind == "commit"
    assert human.events_recent(project_id=project.id, limit=1)[0].agent == "git#hook"


# ---------------------------------------------------------------- setup versioning


def test_version_matches_pyproject() -> None:
    """``delegate.__version__`` is the runtime source of truth; it must match pyproject.toml."""
    import tomllib

    from delegate import __version__

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["version"] == __version__


@pytest.mark.parametrize(
    ("installed", "recorded", "newer"),
    [
        ("0.2.0", None, True),
        ("0.2.0", "0.1.0", True),
        ("0.2.0", "0.2.0", False),
        ("0.2", "0.2.0", False),
        ("0.10.0", "0.9.0", True),
        ("0.1.0", "0.2.0", False),
        ("0.2.0-dev", "0.2.0", True),  # not orderable: any difference counts
    ],
)
def test_is_newer(installed: str, recorded: str | None, newer: bool) -> None:
    assert is_newer(installed, recorded) is newer


def _repo(tmp_path: Path, name: str = "proj") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    return repo


def test_detect_options_from_old_setup(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert detect_options(repo) is None
    project = make_service("human").ensure_project(repo)
    custom = InitOptions(
        command=["uv", "run", "--project", "/src/d", "delegate"], instructions_file="AGENTS.md", hooks=False
    )
    init_repo(repo, project, custom)
    assert detect_options(repo) == custom


def test_setup_record_and_update_available(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    svc = make_service("human")
    project = svc.ensure_project(repo)
    assert not setup_info(svc, project, repo).is_set_up
    # An old setup: files exist but no record.
    init_repo(repo, project, InitOptions(git_hook=False))
    old = setup_info(svc, project, repo)
    assert old.is_set_up and old.inferred and old.recorded is None and old.update_available

    apply_setup(svc, repo, project, InitOptions(git_hook=False))
    info = setup_info(svc, project, repo)
    assert info.recorded is not None and not info.inferred and not info.update_available
    assert info.options == InitOptions(git_hook=False)
    svc.record_setup(project.id, "0.0.1", InitOptions(git_hook=False).model_dump())
    assert setup_info(svc, project, repo).update_available
    assert svc.events_recent(project_id=project.id, limit=1)[0].type == "project_setup"


def test_init_noop_update_and_info_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from delegate import __version__
    from delegate.cli.main import app

    repo = _repo(tmp_path, "clirepo")
    monkeypatch.setenv("DELEGATE_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DELEGATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DELEGATE_AGENT", raising=False)
    monkeypatch.delenv("DELEGATE_PROJECT", raising=False)
    monkeypatch.chdir(repo)
    runner = CliRunner()

    def run(*args: str, code: int = 0) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == code, result.output
        return result.output

    assert json.loads(run("--json", "project", "info"))["set_up"] is False
    run("project", "update", code=2)  # not set up yet
    assert "updated   CLAUDE.md" in run("init", "--command", "uv run delegate", "--no-git-hook")

    # Second init is a no-op, even with different options.
    (repo / "CLAUDE.md").write_text("edited by hand\n")
    assert "already set up" in run("init", "--file", "AGENTS.md")
    assert (repo / "CLAUDE.md").read_text() == "edited by hand\n" and not (repo / "AGENTS.md").exists()

    info = json.loads(run("--json", "project", "info"))
    assert info["setup_version"] == __version__ and info["update_available"] is False
    assert info["options"]["command"] == ["uv", "run", "delegate"] and info["options"]["git_hook"] is False

    # update reuses the stored options and restores the block.
    out = json.loads(run("--json", "project", "update"))
    assert out["to_version"] == __version__ and "CLAUDE.md" in out["changed"]
    assert BEGIN_MARK in (repo / "CLAUDE.md").read_text()
    assert json.loads((repo / ".mcp.json").read_text())["mcpServers"]["delegate"]["command"] == "uv"

    # --force re-runs with the new options and records them.
    run("init", "--force", "--file", "AGENTS.md", "--no-git-hook")
    assert BEGIN_MARK in (repo / "AGENTS.md").read_text()
    assert "file=AGENTS.md" in run("project", "info")
