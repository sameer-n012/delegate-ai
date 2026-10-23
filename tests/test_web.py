"""Tests for the web dashboard: pages, actions, JSON API, error mapping, and SSE."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from delegate.core import NewTask, Project, Settings, TaskService, connect
from delegate.web.app import create_app, event_stream
from delegate.web.views import ago

HX = {"HX-Request": "true"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class Env:
    def __init__(self, settings: Settings, project: Project) -> None:
        self.settings = settings
        self.project = project

    def service(self, actor: str) -> TaskService:
        return TaskService(connect(self.settings.db_path), actor)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    settings = Settings(db_path=tmp_path / "d.db")
    root = tmp_path / "webproj"
    root.mkdir()
    svc = TaskService(connect(settings.db_path), "human")
    project = svc.set_project_state(svc.ensure_project(root, key="WEB").id, "in_progress")
    svc.create_tasks(
        project.id,
        [
            NewTask(title="Design <b>schema</b>", priority="P0", local_id="a"),
            NewTask(title="Build API", depends_on=["a"], local_id="b"),
            NewTask(title="Write docs", priority="P3"),
        ],
    )
    agent = TaskService(connect(settings.db_path), "claude#a1")
    agent.claim_task(project.id, "WEB-1")
    agent.add_note(project.id, "WEB-1", "progress", "Schema drafted & <script>alert(1)</script>")
    return Env(settings, project)


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    with TestClient(create_app(env.settings)) as c:
        yield c


def test_overview_lists_project(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "WEB" in r.text and "webproj" in r.text
    assert "1 active agent" in r.text


def test_board_columns_and_markers(client: TestClient) -> None:
    html = client.get("/p/web").text
    in_progress = html.split('aria-label="In progress"')[1].split("</section>")[0]
    todo = html.split('aria-label="To do"')[1].split("</section>")[0]
    assert "WEB-1" in in_progress and "claude#a1" in in_progress
    assert "WEB-2" in todo and "waiting on deps" in todo
    assert "WEB-3" in todo and "ready" in todo
    assert "Design &lt;b&gt;schema&lt;/b&gt;" in html  # user content is escaped
    assert "claude#a1" in html  # agents panel


def test_closed_columns_hidden_by_default_and_done_capped(env: Env, client: TestClient) -> None:
    human = env.service("human")
    items = [NewTask(title=f"bulk {i}") for i in range(25)]
    for t in human.create_tasks(env.project.id, items):
        human.update_task(env.project.id, t.ref, _status("done"))
    human.update_task(env.project.id, "WEB-3", _status("cancelled"))
    html = client.get("/p/WEB/board").text
    assert 'aria-label="Done"' not in html and 'aria-label="Cancelled"' not in html
    html = client.get("/p/WEB/board?show_done=1").text
    done = html.split('aria-label="Done"')[1].split("</section>")[0]
    assert done.count('class="card ') == 20
    assert 'aria-label="Cancelled"' not in html
    both = client.get("/p/WEB/board?show_done=1&show_cancelled=1").text
    assert 'aria-label="Done"' in both and 'aria-label="Cancelled"' in both
    page = client.get("/p/WEB?show_done=1").text  # full page: toggle state is rendered
    assert 'name="show_done" value="1" checked' in page
    assert 'name="show_cancelled" value="1">' in page


def test_search_filters_board_columns(env: Env, client: TestClient) -> None:
    human = env.service("human")
    human.add_note(env.project.id, "WEB-3", "decision", "Use mkdocs")
    html = client.get("/p/WEB/board?q=mkdocs").text
    assert 'data-ref="WEB-3"' in html
    assert 'data-ref="WEB-1"' not in html and 'data-ref="WEB-2"' not in html
    assert "1 task match “mkdocs” in the shown columns" in html
    assert 'data-ref="WEB-2"' in client.get("/p/WEB/board?q=web-2").text  # by ref
    # A filter on a hidden closed column does not show it.
    human.update_task(env.project.id, "WEB-3", _status("done"))
    assert 'data-ref="WEB-3"' not in client.get("/p/WEB/board?q=mkdocs").text
    assert 'data-ref="WEB-3"' in client.get("/p/WEB/board?q=mkdocs&show_done=1").text
    # The full page keeps the filter text in the box.
    page = client.get("/p/WEB?q=%3Cx%3E").text
    assert 'value="&lt;x&gt;"' in page and "/p/WEB/search" not in page


def _status(value: str):  # noqa: ANN202
    from delegate.core import Status, TaskUpdate

    return TaskUpdate(status=Status(value))


def test_drawer_renders_notes_escaped(client: TestClient) -> None:
    r = client.get("/p/WEB/t/WEB-1", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text  # fragment only
    assert "claude#a1" in r.text and "progress" in r.text
    assert "&lt;script&gt;" in r.text and "<script>alert" not in r.text
    assert "Blocking" in r.text  # WEB-2 depends on WEB-1


def test_drawer_full_page_on_direct_navigation(client: TestClient) -> None:
    r = client.get("/p/WEB/t/WEB-1")
    assert "<html" in r.text and 'data-open="true"' in r.text and "Schema drafted" in r.text


def test_status_change_updates_db(env: Env, client: TestClient) -> None:
    r = client.post("/p/WEB/t/WEB-3/status", content="status=review", headers={**FORM, **HX})
    assert r.status_code == 200
    assert r.headers["HX-Trigger"] == "boardChanged"
    assert env.service("human").get_task("WEB-3").status.value == "review"


def test_release_from_drawer(env: Env, client: TestClient) -> None:
    drawer = client.get("/p/WEB/t/WEB-1", headers=HX).text
    assert "Release claim" in drawer
    r = client.post("/p/WEB/t/WEB-1/release", content="reason=", headers={**FORM, **HX})
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "boardChanged"
    assert "Release claim" not in r.text
    detail = env.service("human").get_task_detail("WEB-1")
    assert detail.task.assignee is None and detail.task.status.value == "in_progress"
    assert detail.notes[-1].kind.value == "handoff" and detail.notes[-1].author == "human"
    # Nothing to release now.
    r = client.post("/p/WEB/t/WEB-1/release", content="reason=again", headers={**FORM, **HX})
    assert r.status_code == 400
    assert "Release claim" not in client.get("/p/WEB/t/WEB-3", headers=HX).text


def test_priority_edit_note_link(env: Env, client: TestClient) -> None:
    base = "/p/WEB/t/WEB-3"
    client.post(f"{base}/priority", content="priority=P1", headers=FORM)
    body = "title=New+title&description=line+1%0Aline+2&acceptance=ok"
    assert client.post(f"{base}/edit", content=body, headers=FORM).status_code == 200
    client.post(f"{base}/note", content="kind=decision&body=Use+SQLite", headers=FORM)
    client.post(f"{base}/link", content="kind=commit&ref=abc123", headers=FORM)
    detail = env.service("human").get_task_detail("WEB-3")
    assert int(detail.task.priority) == 1 and detail.task.title == "New title"
    assert detail.task.description == "line 1\nline 2"
    assert detail.notes[0].kind.value == "decision" and detail.notes[0].author == "human"
    assert detail.links[0].ref == "abc123"


def test_create_task(env: Env, client: TestClient) -> None:
    body = "title=Ship+it&description=&priority=P1&parent=WEB-3&depends_on=WEB-1%2C+WEB-2"
    r = client.post("/p/WEB/tasks", content=body, headers={**FORM, **HX})
    assert r.status_code == 200 and "Created WEB-4" in r.text
    detail = env.service("human").get_task_detail("WEB-4")
    assert detail.task.parent == "WEB-3"
    assert [d.ref for d in detail.depends_on] == ["WEB-1", "WEB-2"]


def test_task_type_icon_create_and_change(env: Env, client: TestClient) -> None:
    board = client.get("/p/WEB/board", headers=HX).text
    assert 'class="ttype tt-unknown"' in board and "<svg" in board
    r = client.post("/p/WEB/tasks", content="title=Fix+it&type=bug", headers={**FORM, **HX})
    assert r.status_code == 200 and "Created WEB-4" in r.text
    assert env.service("human").get_task_detail("WEB-4").task.type.value == "bug"
    drawer = client.post("/p/WEB/t/WEB-3/type", content="type=docs", headers={**FORM, **HX}).text
    assert 'aria-label="Type: Docs"' in drawer
    assert env.service("human").get_task_detail("WEB-3").task.type.value == "docs"
    assert client.post("/p/WEB/t/WEB-3/type", content="type=epic", headers={**FORM, **HX}).status_code == 400
    assert [t["ref"] for t in client.get("/api/p/WEB/tasks?type=bug").json()] == ["WEB-4"]


def test_json_api(client: TestClient) -> None:
    assert [p["key"] for p in client.get("/api/projects").json()] == ["WEB"]
    status = client.get("/api/p/WEB/status").json()
    assert status["counts"]["in_progress"] == 1
    tasks = client.get("/api/p/WEB/tasks", params={"status": ["todo"]}).json()
    assert {t["ref"] for t in tasks} == {"WEB-2", "WEB-3"}
    assert [t["ref"] for t in client.get("/api/p/WEB/tasks?ready_only=true").json()] == ["WEB-3"]
    detail = client.get("/api/p/WEB/tasks/WEB-1").json()
    assert detail["task"]["assignee"] == "claude#a1" and len(detail["notes"]) == 1
    events = client.get("/api/events", params={"project": "WEB"}).json()
    assert any(e["type"] == "task_claimed" for e in events)
    assert client.get("/api/events", params={"after": events[-1]["id"]}).json() == []


def test_error_mapping(client: TestClient) -> None:
    r = client.get("/api/p/WEB/tasks/WEB-99")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert client.get("/api/p/NOPE/status").status_code == 404
    assert client.get("/p/WEB/t/WEB-99").status_code == 404  # HTML error page

    r = client.post("/p/WEB/t/WEB-3/priority", content="priority=P9", headers={**FORM, **HX})
    assert r.status_code == 400
    assert r.headers["HX-Retarget"] == "#toasts" and r.headers["HX-Reswap"] == "beforeend"
    assert 'class="toast' in r.text and "Invalid priority" in r.text

    # dependency cycle: WEB-2 already depends on WEB-1
    r = client.post("/p/WEB/tasks", content="title=x&depends_on=WEB-99", headers=FORM)
    assert r.status_code == 404
    r = client.post("/p/WEB/t/WEB-1/status", content="status=bogus", headers=FORM)
    assert r.status_code == 400


def test_conflict_maps_to_409(env: Env, client: TestClient) -> None:
    r = client.post(
        "/p/WEB/t/WEB-3/edit", content="title=x&version=99", headers={**FORM, **HX}
    )
    assert r.status_code == 409 and "toast" in r.text


def test_ago() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert ago("2026-01-01T11:58:00.000Z", now) == "2m ago"
    assert ago("2026-01-01T11:59:58.000Z", now) == "just now"
    assert ago("2026-01-01T09:00:00.000Z", now) == "3h ago"
    assert ago("2025-12-30T12:00:00.000Z", now) == "2d ago"


def test_event_stream_emits_and_stops(env: Env) -> None:
    human = env.service("human")
    calls = 0

    async def disconnected() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:  # between polls, add an event the stream has not seen
            human.create_tasks(env.project.id, [NewTask(title="late")])
        return calls > 3

    async def collect() -> list[str]:
        return [
            frame
            async for frame in event_stream(
                env.settings, env.project.id, 0, disconnected, poll_seconds=0, ping_seconds=0
            )
        ]

    frames = asyncio.run(collect())
    data = [f for f in frames if f.startswith("event: delegate\ndata: ")]
    ids = [json.loads(f.split("data: ", 1)[1])["id"] for f in data]
    assert ids == sorted(set(ids)) and len(ids) >= 4  # history replayed once, in order
    assert json.loads(data[-1].split("data: ", 1)[1])["type"] == "task_created"
    assert ": ping\n\n" in frames and frames[0] == ": connected\n\n"


def test_event_stream_route_unknown_project(client: TestClient) -> None:
    assert client.get("/p/NOPE/events/stream").status_code == 404


# ---------------------------------------------------------------------- project state


def _add_project(env: Env, tmp_path: Path, name: str, key: str, state: str) -> None:
    root = tmp_path / name
    root.mkdir()
    svc = env.service("human")
    project = svc.ensure_project(root, key=key)
    svc.set_project_state(project.id, state)


@pytest.fixture
def many(env: Env, tmp_path: Path) -> Env:
    _add_project(env, tmp_path, "payments-api", "PAY", "complete")
    _add_project(env, tmp_path, "old-site", "OLD", "deleted")
    return env


def _cards(html: str) -> set[str]:
    return {k for k in ("WEB", "PAY", "OLD") if f'id="pcard-{k}"' in html}


def test_overview_hides_deleted_and_shows_state_badges(many: Env, client: TestClient) -> None:
    html = client.get("/").text
    assert _cards(html) == {"WEB", "PAY"}
    assert "2 projects" in html
    assert "sbadge st-complete" in html and "sbadge st-in_progress" in html


def test_search_by_name_path_and_key_case_insensitive(many: Env, client: TestClient) -> None:
    assert _cards(client.get("/?q=PAYMENTS").text) == {"PAY"}  # name
    assert _cards(client.get("/?q=webproj").text) == {"WEB"}  # path fragment
    assert _cards(client.get("/?q=pay").text) == {"PAY"}  # key and name
    assert _cards(client.get("/?q=wEb").text) == {"WEB"}  # key
    html = client.get("/?q=zzz").text
    assert _cards(html) == set() and "0 projects" in html and "No projects match" in html


def test_state_filter_and_explicit_deleted(many: Env, client: TestClient) -> None:
    assert _cards(client.get("/?state=complete").text) == {"PAY"}
    assert _cards(client.get("/?state=complete&state=in_progress").text) == {"WEB", "PAY"}
    html = client.get("/?state=deleted").text
    assert _cards(html) == {"OLD"} and "1 project<" in html
    assert 'value="deleted" checked' in html  # the chip stays selected
    assert _cards(client.get("/?q=old-site&state=in_progress").text) == set()


def test_htmx_overview_returns_partial_only(many: Env, client: TestClient) -> None:
    r = client.get("/?q=pay", headers=HX)
    assert "<html" not in r.text and 'id="filters"' not in r.text
    assert _cards(r.text) == {"PAY"} and "1 project" in r.text
    assert r.headers["Vary"] == "HX-Request"
    # history restore needs the whole page
    full = client.get("/?q=pay", headers={**HX, "HX-History-Restore-Request": "true"})
    assert "<html" in full.text


def test_overview_invalid_state_is_400(client: TestClient) -> None:
    assert client.get("/?state=bogus").status_code == 400


def test_set_state_from_card(env: Env, client: TestClient) -> None:
    r = client.post("/p/WEB/state", content="state=complete&view=card", headers={**FORM, **HX})
    assert r.status_code == 200 and r.headers["HX-Trigger"] == "boardChanged"
    assert 'id="pcard-WEB"' in r.text and "sbadge st-complete" in r.text
    svc = env.service("human")
    assert svc.get_project("WEB").state.value == "complete"
    events = [e for e in svc.events_recent(project_id=env.project.id, limit=10)]
    changed = [e for e in events if e.type == "project_state_changed"]
    assert changed[0].payload == {"from": "in_progress", "to": "complete"}
    assert changed[0].agent == "human"


def test_set_state_from_board_and_restore_deleted(env: Env, client: TestClient) -> None:
    r = client.post("/p/WEB/state", content="state=deleted&view=board", headers={**FORM, **HX})
    assert 'id="pstate"' in r.text and "is deleted" in r.text
    assert "WEB" not in [p["key"] for p in client.get("/api/projects").json()]
    assert client.get("/p/WEB").status_code == 200  # still reachable
    r = client.post("/p/WEB/state", content="state=in_progress&view=board", headers={**FORM, **HX})
    assert 'class="banner"' not in r.text
    assert "WEB" in [p["key"] for p in client.get("/api/projects").json()]


def test_set_state_without_htmx_redirects(env: Env, client: TestClient) -> None:
    r = client.post("/p/WEB/state", content="state=inactive&view=board", headers=FORM,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/WEB"
    r = client.post("/p/WEB/state", content="state=in_progress", headers=FORM,
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_set_state_invalid_is_400_toast(client: TestClient) -> None:
    r = client.post("/p/WEB/state", content="state=nope", headers={**FORM, **HX})
    assert r.status_code == 400 and "toast" in r.text
    assert client.post("/p/NOPE/state", content="state=complete", headers=FORM).status_code == 404


def test_banner_only_when_not_in_progress(env: Env, client: TestClient) -> None:
    assert 'class="banner"' not in client.get("/p/WEB").text
    env.service("human").set_project_state("WEB", "complete")
    html = client.get("/p/WEB").text
    assert 'class="banner"' in html and "This project is complete" in html
    assert "Set in progress" in html
    assert "Design &lt;b&gt;schema&lt;/b&gt;" in html  # the board still renders tasks


def test_project_state_event_text(env: Env, client: TestClient) -> None:
    env.service("human").set_project_state("WEB", "complete")
    assert "project → complete" in client.get("/p/WEB/side").text


def test_api_projects_filters(many: Env, client: TestClient) -> None:
    def keys(params: dict[str, str | list[str]]) -> set[str]:
        return {p["key"] for p in client.get("/api/projects", params=params).json()}

    assert keys({}) == {"WEB", "PAY"}
    assert keys({"q": "pay"}) == {"PAY"}
    assert keys({"state": "deleted"}) == {"OLD"}
    assert keys({"state": ["deleted", "complete"]}) == {"OLD", "PAY"}
    assert keys({"q": "web", "state": "complete"}) == set()
    r = client.get("/api/projects", params={"state": "bogus"})
    assert r.status_code == 400 and r.json()["error"] == "invalid"


def test_agent_blocked_error_maps_to_409(env: Env, client: TestClient) -> None:
    from delegate.core import ProjectClosedError

    env.service("human").set_project_state("WEB", "complete")
    agent = env.service("claude#a1")
    with pytest.raises(ProjectClosedError):
        agent.add_note(env.project.id, "WEB-1", "progress", "x")
    # the dashboard acts as human, so its own edits still work
    r = client.post("/p/WEB/t/WEB-3/status", content="status=review", headers={**FORM, **HX})
    assert r.status_code == 200
