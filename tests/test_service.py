"""TaskService behaviour: creation, readiness, claims, leases, transitions, notes, events."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from delegate.core import (
    ANONYMOUS,
    ConflictError,
    CycleError,
    ForbiddenError,
    IdentityRequiredError,
    InvalidTransitionError,
    NewTask,
    NoteKind,
    NotHolderError,
    Project,
    ProjectClosedError,
    ProjectState,
    Status,
    TaskType,
    TaskUpdate,
    ValidationError,
)
from delegate.core.ids import derive_key, parse_ref

from .conftest import FakeClock, ServiceFactory


def test_project_key_and_idempotent_ensure(make_service: ServiceFactory, project: Project) -> None:
    assert project.key == "DLG"
    again = make_service("human").ensure_project(Path(project.root_path))
    assert again.id == project.id


def test_worktrees_share_project(make_service: ServiceFactory, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init")
    git("worktree", "add", str(tmp_path / "wt"), "-b", "feature")
    svc = make_service("human")
    assert svc.ensure_project(repo).id == svc.ensure_project(tmp_path / "wt").id


def test_relink_moved_project(make_service: ServiceFactory, tmp_path: Path) -> None:
    human = make_service("human")
    old, new, busy = tmp_path / "old", tmp_path / "new", tmp_path / "busy"
    for d in (old, new, busy):
        d.mkdir()
    project = human.ensure_project(old, key="MOV")
    human.create_tasks(project.id, [NewTask(title="keep me")])
    # A hook in the moved folder registered it as a new, empty project.
    placeholder = human.ensure_project(new)
    with pytest.raises(ForbiddenError):
        make_service("claude#aaaa").relink_project("MOV", new)
    moved = human.relink_project("mov", new)
    assert moved.root_path == str(new.resolve()) and moved.id == project.id
    assert human.ensure_project(new).id == project.id
    assert placeholder.key not in {p.key for p in human.list_projects(states=list(ProjectState))}
    assert human.get_task("MOV-1").title == "keep me"
    assert human.events_since(0, project_id=project.id)[-1].type == "project_relinked"
    # A project with tasks at the target path is never removed.
    other = human.ensure_project(busy, key="BSY")
    human.create_tasks(other.id, [NewTask(title="mine")])
    with pytest.raises(ConflictError, match="BSY"):
        human.relink_project("MOV", busy)
    assert human.relink_project("MOV", new).root_path == moved.root_path  # no-op


def test_derive_key_and_parse_ref() -> None:
    none: set[str] = set()
    assert derive_key("delegate-ai", none) == "DLG"
    assert derive_key("delegate-ai", {"DLG"}) == "DLG2"
    assert derive_key("io", none) == "IO"
    assert parse_ref("dlg-42") == ("DLG", 42)
    assert parse_ref("#7") == (None, 7)
    with pytest.raises(ValidationError):
        parse_ref("nope")


def test_create_batch_with_local_ids(make_service: ServiceFactory, project: Project) -> None:
    svc = make_service("claude#0001")
    tasks = svc.create_tasks(
        project.id,
        [
            NewTask(title="Epic", local_id="epic"),
            NewTask(title="Schema", parent="epic", priority="P0", local_id="schema"),
            NewTask(title="Service", parent="epic", depends_on=["schema"]),
        ],
    )
    assert [t.ref for t in tasks] == ["DLG-1", "DLG-2", "DLG-3"]
    assert tasks[1].parent == "DLG-1"
    detail = svc.get_task_detail("DLG-3")
    assert [d.ref for d in detail.depends_on] == ["DLG-2"]
    assert detail.ready is False


def test_create_batch_is_atomic(make_service: ServiceFactory, project: Project) -> None:
    svc = make_service("human")
    with pytest.raises(Exception):
        svc.create_tasks(project.id, [NewTask(title="ok"), NewTask(title="bad", depends_on=["DLG-99"])])
    assert svc.list_tasks(project.id) == []


def test_claim_next_order_and_epics_skipped(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(
        project.id,
        [
            NewTask(title="Epic", priority=0, local_id="e"),
            NewTask(title="Low", priority=3),
            NewTask(title="High child", priority=1, parent="e"),
        ],
    )
    agent = make_service("claude#0001")
    first = agent.claim_task(project.id)
    assert first is not None and first.title == "High child"
    second = agent.claim_task(project.id)
    assert second is not None and second.title == "Low"  # epic has an open child
    assert agent.claim_task(project.id) is None


def test_claim_requires_ready(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A", local_id="a"), NewTask(title="B", depends_on=["a"])])
    agent = make_service("claude#0001")
    with pytest.raises(ValidationError, match="DLG-1"):
        agent.claim_task(project.id, "DLG-2")
    agent.claim_task(project.id, "DLG-1")
    agent.complete_task(project.id, "DLG-1", "done")
    assert agent.claim_task(project.id, "DLG-2") is not None


def test_cancelled_dependency_counts_as_resolved(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A", local_id="a"), NewTask(title="B", depends_on=["a"])])
    human.update_task(project.id, "DLG-1", TaskUpdate(status=Status.CANCELLED))
    assert human.get_task_detail("DLG-2").ready


def test_holder_rules(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    a, b = make_service("claude#aaaa"), make_service("codex#bbbb")
    a.claim_task(project.id, "DLG-1")
    with pytest.raises(NotHolderError):
        b.claim_task(project.id, "DLG-1")
    with pytest.raises(NotHolderError):
        b.update_task(project.id, "DLG-1", TaskUpdate(title="hijack"))
    with pytest.raises(NotHolderError):
        b.complete_task(project.id, "DLG-1", "nope")
    b.add_note(project.id, "DLG-1", NoteKind.PROGRESS, "anyone can note")
    # Humans override holder checks.
    make_service("human").update_task(project.id, "DLG-1", TaskUpdate(priority="P0"))


def test_lease_expiry_and_renewal(
    make_service: ServiceFactory, project: Project, clock: FakeClock
) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A"), NewTask(title="B")])
    a = make_service("claude#aaaa")
    a.claim_task(project.id, "DLG-1")
    clock.advance(10)
    a.add_note(project.id, "DLG-1", "progress", "still working")  # renews lease
    clock.advance(10)
    assert a.get_task("DLG-1").status == Status.IN_PROGRESS
    clock.advance(6)  # 16 min since last write: expired
    b = make_service("codex#bbbb")
    status = b.project_status(project.id)
    # The status stays; only the claim goes.
    assert [t.ref for t in status.unclaimed] == ["DLG-1"]
    task = a.get_task("DLG-1")
    assert task.status == Status.IN_PROGRESS and task.assignee is None
    types = [e.type for e in a.events_since(0)]
    assert "lease_expired" in types
    # Claim-next still takes only ready todo tasks; the unclaimed task needs a ref.
    nxt = b.claim_task(project.id)
    assert nxt is not None and nxt.ref == "DLG-2"
    again = b.claim_task(project.id, "DLG-1")
    assert again is not None and again.assignee == "codex#bbbb"


def test_agent_transitions(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    a = make_service("claude#aaaa")
    with pytest.raises(InvalidTransitionError):
        a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.IN_PROGRESS))
    with pytest.raises(InvalidTransitionError):
        a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.DONE))
    a.claim_task(project.id, "DLG-1")
    # A status change keeps the claim and its lease.
    t = a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.BLOCKED))
    assert t.assignee == "claude#aaaa" and t.lease_expires_at is not None
    t = a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.TODO))
    assert t.assignee == "claude#aaaa" and not a.get_task_detail("DLG-1").ready
    # The holder can move its own task back to in_progress.
    a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.IN_PROGRESS))
    # Closing a task ends the claim.
    t = a.update_task(project.id, "DLG-1", TaskUpdate(status=Status.CANCELLED))
    assert t.assignee is None and t.lease_expires_at is None


def test_claim_in_any_status(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A"), NewTask(title="B")])
    human.update_task(project.id, "DLG-1", TaskUpdate(status=Status.REVIEW))
    human.update_task(project.id, "DLG-2", TaskUpdate(status=Status.DONE))
    a, b = make_service("claude#aaaa"), make_service("codex#bbbb")
    t = a.claim_task(project.id, "DLG-1")
    assert t is not None and t.status == Status.REVIEW and t.assignee == "claude#aaaa"
    with pytest.raises(NotHolderError):
        b.update_task(project.id, "DLG-1", TaskUpdate(status=Status.DONE))
    done = a.complete_task(project.id, "DLG-1", "reviewed")
    assert done.status == Status.DONE and done.assignee is None
    t = a.claim_task(project.id, "DLG-2")
    assert t is not None and t.status == Status.DONE
    with pytest.raises(InvalidTransitionError, match="already done"):
        a.complete_task(project.id, "DLG-2", "again")
    a.update_task(project.id, "DLG-2", TaskUpdate(status=Status.TODO))  # reopen, still held
    assert a.get_task("DLG-2").assignee == "claude#aaaa"


def test_human_claim_ends_when_leaving_in_progress(
    make_service: ServiceFactory, project: Project
) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A")])
    t = human.update_task(project.id, "DLG-1", TaskUpdate(status=Status.IN_PROGRESS))
    assert t.assignee == "human"
    with pytest.raises(NotHolderError):
        make_service("claude#aaaa").claim_task(project.id, "DLG-1")
    t = human.update_task(project.id, "DLG-1", TaskUpdate(status=Status.REVIEW))
    assert t.assignee is None


def test_complete_to_review_then_done(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    a = make_service("claude#aaaa")
    a.claim_task(project.id, "DLG-1")
    t = a.complete_task(project.id, "DLG-1", "built it", links=[("commit", "abc123")], to_review=True)
    assert t.status == Status.REVIEW and t.closed_at is None
    reviewer = make_service("codex#bbbb")
    done = reviewer.update_task(project.id, "DLG-1", TaskUpdate(status=Status.DONE))
    assert done.closed_at is not None
    detail = reviewer.get_task_detail("DLG-1")
    assert [n.kind for n in detail.notes] == [NoteKind.SUMMARY]
    assert detail.links[0].ref == "abc123"


def test_cannot_complete_with_open_children(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(
        project.id, [NewTask(title="Epic", local_id="e"), NewTask(title="Child", parent="e")]
    )
    a = make_service("claude#aaaa")
    a.claim_task(project.id, "DLG-1")
    with pytest.raises(InvalidTransitionError, match="open subtask"):
        a.complete_task(project.id, "DLG-1", "done")


def test_release_keeps_status(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    a, b = make_service("claude#aaaa"), make_service("codex#bbbb")
    a.claim_task(project.id, "DLG-1")
    with pytest.raises(NotHolderError):
        b.release_task(project.id, "DLG-1", "not mine")
    t = a.release_task(project.id, "DLG-1", "out of context; next step is X")
    assert t.status == Status.IN_PROGRESS and t.assignee is None and t.lease_expires_at is None
    assert a.get_task_detail("DLG-1").notes[-1].kind == NoteKind.HANDOFF
    assert a.events_since(0)[-1].type == "task_released"
    with pytest.raises(InvalidTransitionError, match="not claimed"):
        a.release_task(project.id, "DLG-1", "again")
    # Unclaimed tasks are open to every agent; another agent can pick it up.
    b.update_task(project.id, "DLG-1", TaskUpdate(priority="P0"))
    t = b.claim_task(project.id, "DLG-1")
    assert t is not None and t.status == Status.IN_PROGRESS and t.assignee == "codex#bbbb"
    b.complete_task(project.id, "DLG-1", "built it", to_review=True)
    b.claim_task(project.id, "DLG-1")
    t = b.release_task(project.id, "DLG-1", "review later")
    assert t.status == Status.REVIEW and t.assignee is None


def test_human_releases_any_claim(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    make_service("claude#aaaa").claim_task(project.id, "DLG-1")
    t = make_service("human").release_task(project.id, "DLG-1", "stuck agent")
    assert t.status == Status.IN_PROGRESS and t.assignee is None


def test_cycles_rejected(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(
        project.id,
        [NewTask(title="A", local_id="a"), NewTask(title="B", depends_on=["a"], local_id="b"),
         NewTask(title="C", depends_on=["b"])],
    )
    with pytest.raises(CycleError):
        human.update_task(project.id, "DLG-1", TaskUpdate(add_deps=["DLG-3"]))
    with pytest.raises(CycleError):
        human.update_task(project.id, "DLG-1", TaskUpdate(add_deps=["DLG-1"]))
    human.update_task(project.id, "DLG-2", TaskUpdate(parent="DLG-1"))
    with pytest.raises(CycleError):
        human.update_task(project.id, "DLG-1", TaskUpdate(parent="DLG-2"))


def test_task_types(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    plain, bug = human.create_tasks(
        project.id, [NewTask(title="Plain"), NewTask(title="Crash", type="Bug")]
    )
    assert plain.type is TaskType.UNKNOWN and bug.type is TaskType.BUG
    with pytest.raises(ValidationError, match="Invalid task type"):
        human.create_tasks(project.id, [NewTask(title="X", type="epic")])
    updated = human.update_task(project.id, plain.ref, TaskUpdate(type=TaskType.DOCS))
    assert updated.type is TaskType.DOCS and updated.version == plain.version + 1
    # Same type again is a no-op: no new version.
    assert human.update_task(project.id, plain.ref, TaskUpdate(type="docs")).version == updated.version
    with pytest.raises(ValidationError):
        human.update_task(project.id, plain.ref, TaskUpdate(type="nope"))
    assert [t.ref for t in human.list_tasks(project.id, task_type="bug")] == [bug.ref]
    assert human.list_tasks(project.id, task_type=TaskType.DESIGN) == []


def test_clear_parent(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A", local_id="a"), NewTask(title="B", parent="a")])
    with pytest.raises(ValidationError):
        human.update_task(project.id, "DLG-2", TaskUpdate(parent=""))
    with pytest.raises(ValidationError):
        human.update_task(project.id, "DLG-2", TaskUpdate(parent="DLG-1", clear_parent=True))
    assert human.update_task(project.id, "DLG-2", TaskUpdate(clear_parent=True)).parent is None
    # Clearing an absent parent is a no-op, not an error.
    assert human.update_task(project.id, "DLG-2", TaskUpdate(clear_parent=True)).parent is None


def test_optimistic_version(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    (t,) = human.create_tasks(project.id, [NewTask(title="A")])
    human.update_task(project.id, t.ref, TaskUpdate(title="B"), expected_version=t.version)
    with pytest.raises(ConflictError):
        human.update_task(project.id, t.ref, TaskUpdate(title="C"), expected_version=t.version)


def test_project_status(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(
        project.id, [NewTask(title="A"), NewTask(title="B"), NewTask(title="C")]
    )
    a, b = make_service("claude#aaaa"), make_service("codex#bbbb")
    a.claim_task(project.id, "DLG-1")
    b.claim_task(project.id, "DLG-2")
    b.update_task(project.id, "DLG-2", TaskUpdate(status=Status.BLOCKED))
    b.add_note(project.id, "DLG-2", NoteKind.BLOCKER, "need API key")
    s = a.project_status(project.id)
    assert [t.ref for t in s.mine] == ["DLG-1"]
    assert [t.ref for t in s.ready] == ["DLG-3"]
    assert s.blocked[0].reason == "need API key"
    assert s.counts["todo"] == 1


def test_list_recent_order_and_events_recent(
    make_service: ServiceFactory, project: Project, clock: FakeClock
) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="A", local_id="a"), NewTask(title="B", parent="a")])
    human.update_task(project.id, "DLG-2", TaskUpdate(status=Status.DONE))
    clock.advance(1)
    human.update_task(project.id, "DLG-1", TaskUpdate(status=Status.DONE))
    recent = human.list_tasks(project.id, statuses=[Status.DONE], order="recent")
    assert [t.ref for t in recent] == ["DLG-1", "DLG-2"]
    assert recent[0].closed_at is not None
    newest = human.events_recent(project_id=project.id, limit=2)
    assert newest[0].id > newest[1].id and newest[0].task == "DLG-1"


def test_claim_records_agent_project(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    agent = make_service("claude#aaaa")
    agent.claim_task(project.id)
    assert [a.id for a in agent.list_agents(project_id=project.id)] == ["claude#aaaa"]


def test_anonymous_can_read_but_not_write(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A")])
    anon = make_service(ANONYMOUS)
    assert anon.project_status(project.id).counts["todo"] == 1
    with pytest.raises(IdentityRequiredError):
        anon.add_note(project.id, "DLG-1", "progress", "hi")
    with pytest.raises(IdentityRequiredError):
        anon.claim_task(project.id)
    with pytest.raises(ForbiddenError):
        anon.set_project_state(project.id, ProjectState.COMPLETE)


def test_new_projects_start_not_started_and_block_agents(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    human = make_service("human")
    fresh = human.ensure_project(root)
    assert fresh.state == ProjectState.NOT_STARTED
    human.create_tasks(fresh.id, [NewTask(title="Planned by a human")])  # humans exempt
    agent = make_service("claude#aaaa")
    with pytest.raises(ProjectClosedError, match="not_started"):
        agent.claim_task(fresh.id)
    with pytest.raises(ProjectClosedError):
        agent.create_tasks(fresh.id, [NewTask(title="x")])
    with pytest.raises(ProjectClosedError):
        agent.add_note(fresh.id, "1", "progress", "x")
    human.set_project_state(fresh.key, "in_progress")
    assert agent.claim_task(fresh.id) is not None


def test_closing_project_freezes_agent_writes(make_service: ServiceFactory, project: Project) -> None:
    make_service("human").create_tasks(project.id, [NewTask(title="A"), NewTask(title="B")])
    agent = make_service("claude#aaaa")
    agent.claim_task(project.id, "DLG-1")
    human = make_service("human")
    human.set_project_state(project.id, ProjectState.COMPLETE)
    for call in (
        lambda: agent.complete_task(project.id, "DLG-1", "done"),
        lambda: agent.release_task(project.id, "DLG-1", "bye"),
        lambda: agent.update_task(project.id, "DLG-1", TaskUpdate(title="x")),
        lambda: agent.add_links(project.id, "DLG-1", [("url", "http://x")]),
        lambda: agent.claim_task(project.id, "DLG-2"),
    ):
        with pytest.raises(ProjectClosedError):
            call()
    human.update_task(project.id, "DLG-2", TaskUpdate(status=Status.CANCELLED))  # humans exempt
    events = [e for e in human.events_since(0) if e.type == "project_state_changed"]
    assert events[-1].payload == {"from": "in_progress", "to": "complete"}


def test_only_humans_change_project_state(make_service: ServiceFactory, project: Project) -> None:
    with pytest.raises(ForbiddenError):
        make_service("claude#aaaa").set_project_state(project.id, ProjectState.INACTIVE)
    with pytest.raises(ValidationError):
        make_service("human").set_project_state(project.id, "archived")


def test_list_projects_filter_and_search(make_service: ServiceFactory, tmp_path: Path) -> None:
    human = make_service("human")
    keys: dict[str, str] = {}
    for name in ("alpha-web", "beta-api", "gamma-ml"):
        (tmp_path / name).mkdir()
        keys[name] = human.ensure_project(tmp_path / name).key
    human.set_project_state(keys["beta-api"], ProjectState.IN_PROGRESS)
    human.set_project_state(keys["gamma-ml"], ProjectState.DELETED)

    def names(ps: list[Project]) -> list[str]:
        return [p.name for p in ps]

    assert names(human.list_projects()) == ["alpha-web", "beta-api"]  # deleted hidden
    assert names(human.list_projects(states=[ProjectState.DELETED])) == ["gamma-ml"]
    assert names(human.list_projects(query="API")) == ["beta-api"]
    assert names(human.list_projects(query=keys["alpha-web"].lower())) == ["alpha-web"]
    assert names(human.list_projects(query=str(tmp_path / "alpha"))) == ["alpha-web"]
    assert names(
        human.list_projects(states=[ProjectState.NOT_STARTED, ProjectState.IN_PROGRESS], query="a")
    ) == ["alpha-web", "beta-api"]
    # LIKE wildcards in user input match literally.
    assert names(human.list_projects(query="alpha_web")) == []
    assert names(human.list_projects(query="%")) == []


def test_task_query_escapes_wildcards(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(project.id, [NewTask(title="100% coverage"), NewTask(title="1000 items")])
    assert [t.title for t in human.list_tasks(project.id, query="100%")] == ["100% coverage"]


def test_task_query_matches_all_fields_and_refs(make_service: ServiceFactory, project: Project) -> None:
    human = make_service("human")
    human.create_tasks(
        project.id,
        [
            NewTask(title="plain one"),
            NewTask(title="plain two", acceptance="must handle Zebra input"),
            NewTask(title="plain three"),
            NewTask(title="plain four"),
            NewTask(title="plain five"),
        ],
    )
    human.add_note(project.id, "DLG-3", "decision", "picked the walrus approach")
    human.add_links(project.id, "DLG-4", [("file", "src/narwhal.py")])
    human.update_task(project.id, "DLG-5", TaskUpdate(status=Status.DONE))

    def refs(query: str, include_closed: bool = False) -> list[str]:
        tasks = human.list_tasks(project.id, query=query, include_closed=include_closed)
        return [t.ref for t in tasks]

    assert refs("zebra") == ["DLG-2"]  # acceptance, case-insensitive
    assert refs("walrus") == ["DLG-3"]  # note body
    assert refs("narwhal") == ["DLG-4"]  # link ref
    assert refs("dlg-1") == ["DLG-1"] and refs("#1") == ["DLG-1"] and refs("1") == ["DLG-1"]
    assert refs("OTHER-1") == []
    assert refs("DLG-5") == [] and refs("DLG-5", include_closed=True) == ["DLG-5"]
    assert len(refs("   ")) == 4  # blank query: no filter


def test_events_since(make_service: ServiceFactory, project: Project) -> None:
    svc = make_service("human")
    start = svc.latest_event_id()
    svc.create_tasks(project.id, [NewTask(title="A")])
    events = svc.events_since(start, project_id=project.id)
    assert [e.type for e in events] == ["task_created"]
    assert events[0].task == "DLG-1"
