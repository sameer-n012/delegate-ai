"""CLI tests: run the Typer app in-process against a temp DB."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from delegate.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DB and a project directory (no git: the path is the identity)."""
    monkeypatch.setenv("DELEGATE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DELEGATE_DB", str(tmp_path / "delegate.db"))
    # Tests act as an explicit human by default (there is no implicit human any more)
    # and start the project, since new projects are not_started.
    monkeypatch.setenv("DELEGATE_AGENT", "human")
    monkeypatch.delenv("DELEGATE_PROJECT", raising=False)
    proj = tmp_path / "demo-app"
    proj.mkdir()
    monkeypatch.chdir(proj)
    run("project", "state", "in_progress")
    return proj


def run(*args: str, input: str | None = None, code: int = 0) -> Any:
    result = runner.invoke(app, list(args), input=input)
    assert result.exit_code == code, (result.output, result.exception)
    return result


def run_json(*args: str, as_: str | None = None) -> Any:
    pre = ["--json"] + (["--as", as_] if as_ else [])
    return json.loads(run(*pre, *args).stdout)


def test_add_list_show() -> None:
    out = run("add", "First task", "--desc", "details", "-p", "P1", "--acceptance", "works").stdout
    ref = out.split()[0]
    assert "P1" in out and "todo" in out
    run("add", "Second", "--dep", ref, "--parent", ref)
    listing = run("list").stdout
    assert "[ready]" in listing and "(parent" in listing
    assert len(listing.strip().splitlines()) == 2
    shown = run("show", ref).stdout
    assert "details" in shown and "Acceptance:" in shown and "Dependents:" in shown
    assert run("list", "-q", "Second").stdout.count("\n") == 1
    assert "No tasks." in run("list", "--status", "done").stdout


def test_batch_add_from_file_and_stdin(env: Path) -> None:
    items = [
        {"title": "Epic", "local_id": "e", "priority": "P1"},
        {"title": "Child", "parent": "e", "depends_on": ["e"]},
    ]
    f = env / "batch.json"
    f.write_text(json.dumps(items))
    created = run_json("add", "--file", str(f))
    assert [t["title"] for t in created] == ["Epic", "Child"]
    assert created[1]["parent"] == created[0]["ref"]
    out = run("add", "--file", "-", input=json.dumps([{"title": "Via stdin"}])).stdout
    assert "Via stdin" in out


def test_add_argument_errors(env: Path) -> None:
    run("add", code=2)
    run("add", "x", "--file", "-", input="[]", code=2)
    run("add", "--file", "-", input="not json", code=2)
    result = runner.invoke(app, ["add", "--file", "-"], input=json.dumps([{"title": ""}]))
    assert result.exit_code == 2 and "error[invalid]" in result.output
    run("add", "--file", str(env / "missing.json"), code=2)


def test_claim_done_with_links() -> None:
    ref = run_json("add", "Work")[0]["ref"]
    claimed = run_json("claim", as_="claude#aaa1")
    assert claimed["ref"] == ref and claimed["status"] == "in_progress"
    status = run("--as", "claude#aaa1", "status").stdout
    assert "Yours:" in status and "lease until" in status
    assert "Others:" in run("status").stdout
    done = run_json(
        "done", ref, "All good", "--link", "commit=abc123", "--link", "url=http://x?a=b",
        as_="claude#aaa1",
    )
    assert done["status"] == "done"
    detail = run_json("show", ref)
    assert {(link["kind"], link["ref"]) for link in detail["links"]} == {
        ("commit", "abc123"),
        ("url", "http://x?a=b"),
    }
    assert detail["notes"][0]["kind"] == "summary"


def test_claim_nothing_ready() -> None:
    run("add", "x")  # created but then claimed away
    run("--as", "a#1", "claim")
    text = run("--as", "a#2", "claim").stdout
    assert "Nothing ready" in text
    assert run_json("claim", as_="a#2") is None


def test_done_to_review_and_bad_link() -> None:
    ref = run_json("add", "R")[0]["ref"]
    run("--as", "a#1", "claim", ref)
    run("--as", "a#1", "done", ref, "s", "--link", "nokind", code=2)
    out = run("--as", "a#1", "done", ref, "ready for review", "--review").stdout
    assert "review" in out


def test_update_status_and_deps() -> None:
    a = run_json("add", "A")[0]["ref"]
    b = run_json("add", "B")[0]["ref"]
    out = run("update", b, "--add-dep", a, "--priority", "P0", "--title", "B2").stdout
    assert "P0" in out and "B2" in out
    assert run_json("update", b, "--rm-dep", a, "--status", "blocked")["status"] == "blocked"
    version = run_json("update", b, "--desc", "d")["version"]
    run("update", b, "--title", "stale", "--expect-version", str(version - 1), code=1)
    assert run_json("update", b, "--parent", a)["parent"] == a
    run("update", b, "--parent", "", code=2)
    run("update", b, "--parent", a, "--clear-parent", code=2)
    assert run_json("update", b, "--clear-parent")["parent"] is None


def test_note_and_stdin_note() -> None:
    ref = run_json("add", "N")[0]["ref"]
    run("note", ref, "first", "--kind", "decision")
    run("note", ref, "-", input="from stdin\n")
    notes = run_json("show", ref)["notes"]
    assert [(n["kind"], n["body"]) for n in notes] == [
        ("decision", "first"),
        ("progress", "from stdin"),
    ]
    run("note", ref, "x", "--kind", "bogus", code=2)
    run("link", ref, "pr", "http://pr/1")


def test_release() -> None:
    ref = run_json("add", "Rel")[0]["ref"]
    run("--as", "a#1", "claim", ref)
    run("--as", "a#2", "release", ref, "mine now", code=1)
    out = run("--as", "a#1", "release", ref, "handing off").stdout
    assert "todo" in out
    assert run_json("show", ref)["notes"][0]["kind"] == "handoff"


def test_error_exit_codes() -> None:
    ref = run_json("add", "Held")[0]["ref"]
    run("--as", "a#1", "claim", ref)
    result = runner.invoke(app, ["--as", "a#2", "claim", ref])
    assert result.exit_code == 1
    assert "error[not_holder]" in result.output
    err = json.loads(run("--json", "--as", "a#2", "claim", ref, code=1).stdout)
    assert err["error"] == "not_holder"
    run("add", "bad", "-p", "P9", code=2)
    run("show", "999", code=1)
    run("list", "--status", "nonsense", code=2)


def test_status_output() -> None:
    a = run_json("add", "Ready one", "-p", "P1")[0]["ref"]
    b = run_json("add", "Blocked one")[0]["ref"]
    run("update", b, "--status", "blocked")
    run("note", b, "waiting on human", "--kind", "blocker")
    lines = run("status").stdout.splitlines()
    assert lines[0].startswith("demo-app (") and "1 todo" in lines[0] and "1 blocked" in lines[0]
    assert any(line.startswith("Ready:") and a in line for line in lines)
    assert any(line.startswith("Blocked:") and "waiting on human" in line for line in lines)
    assert run_json("status")["counts"]["todo"] == 1


def test_projects_agents_events_whoami() -> None:
    run("add", "x")
    assert "demo-app" in run("projects").stdout
    run("--as", "codex#9", "claim")
    assert "codex#9" in run("agents", "--active", "5").stdout
    assert len(run_json("agents")) == 1
    events = run("events").stdout
    assert "task_created" in events and "task_claimed" in events
    last = max(e["id"] for e in map(json.loads, run("--json", "events").stdout.splitlines()))
    assert "No events." in run("events", "--since", str(last)).stdout
    who = run_json("whoami")
    assert who["actor"] == "human"
    assert run_json("whoami", as_="x#1")["actor"] == "x#1"


def test_project_option_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = run_json("whoami")["project"]
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    run("--project", key, "add", "In first project")
    assert "In first project" in run("--project", key, "list").stdout
    monkeypatch.setenv("DELEGATE_PROJECT", key)
    assert "In first project" in run("list").stdout
    run("--project", "NOPE", "list", code=1)


def test_no_identity_reads_ok_writes_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    run("add", "x")
    monkeypatch.delenv("DELEGATE_AGENT")
    assert "x" in run("list").stdout
    assert run_json("whoami")["actor"] == "anonymous"
    out = run("--json", "note", "1", "hi", code=1).stdout
    assert json.loads(out)["error"] == "no_identity"
    run("--json", "claim", code=1)


def test_project_state_and_projects_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run("add", "x")
    assert run_json("project", "state")["state"] == "in_progress"
    # Agents cannot change project state, and cannot work once it is closed.
    assert json.loads(run("--json", "--as", "codex#1", "project", "state", "complete", code=1).stdout)["error"] == "forbidden"
    run("project", "state", "complete")
    err = json.loads(run("--json", "--as", "codex#1", "claim", code=1).stdout)
    assert err["error"] == "project_not_in_progress"
    assert "Project is complete" in run("status").stdout
    # Second project for filtering; deleted projects are hidden unless asked for.
    other = tmp_path / "other-svc"
    other.mkdir()
    monkeypatch.chdir(other)
    run("project", "state", "deleted")
    assert [p["name"] for p in run_json("projects")] == ["demo-app"]
    assert [p["name"] for p in run_json("projects", "--state", "deleted")] == ["other-svc"]
    assert [p["name"] for p in run_json("projects", "-q", "OTHER", "--state", "deleted")] == ["other-svc"]
    run("project", "state", "archived", code=2)
