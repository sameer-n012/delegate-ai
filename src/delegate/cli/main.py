"""Typer CLI: a thin adapter over ``TaskService``. No business rules live here."""

import functools
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from delegate.cli import render
from delegate.integrations.commands import hook_app, init_command
from delegate.core import (
    DelegateError,
    NewTask,
    Project,
    ProjectState,
    Status,
    TaskType,
    TaskUpdate,
    ValidationError,
)
from delegate.runtime import Runtime, open_runtime, resolve_actor

GLOBAL_FLAGS = frozenset({"--json"})
GLOBAL_VALUE_OPTIONS = frozenset({"--as", "--project"})


def hoist_global_options(args: list[str]) -> list[str]:
    """Move global options found after the subcommand to the front of ``args``.

    Click only accepts the callback's options before the subcommand, so ``delegate status
    --json`` would fail. Agents often write it that way. Handles ``--as X`` and ``--as=X``.
    Stops at ``--``, so later tokens stay positional. A value option with no value is left
    in place for Click to report.
    """
    front: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            rest.extend(args[i:])
            break
        name = arg.split("=", 1)[0]
        if arg in GLOBAL_FLAGS:
            front.append(arg)
        elif name in GLOBAL_VALUE_OPTIONS and "=" in arg:
            front.append(arg)
        elif arg in GLOBAL_VALUE_OPTIONS and i + 1 < len(args):
            front.extend(args[i : i + 2])
            i += 1
        else:
            rest.append(arg)
        i += 1
    return front + rest


class _App(typer.Typer):
    """Typer app that accepts global options anywhere on the command line."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # The console script calls app() with no arguments, so Click reads sys.argv.
        if not args and "args" not in kwargs:
            kwargs["args"] = hoist_global_options(sys.argv[1:])
        return super().__call__(*args, **kwargs)


app = _App(help="Task tracker for coding agents.", no_args_is_help=True, add_completion=False)
# Integrations (Phase 3) live in delegate.integrations; they manage their own runtime
# and error handling because hooks must always exit 0.
app.command("init")(init_command)
app.add_typer(hook_app, name="hook")


class ConfigError(DelegateError):
    """Settings or database could not be loaded."""

    code = "config"


@dataclass
class State:
    """Per-invocation state from the global options. The runtime opens on first use."""

    actor: str | None
    project_key: str | None
    as_json: bool
    ctx: typer.Context
    _rt: Runtime | None = field(default=None, repr=False)

    def rt(self) -> Runtime:
        if self._rt is None:
            try:
                self._rt = open_runtime(self.actor)
            except ValueError as exc:  # malformed config.json or bad setting value
                raise ConfigError(str(exc)) from exc
            self.ctx.call_on_close(self._rt.close)
        return self._rt

    def project(self) -> Project:
        return self.rt().project_for(key=self.project_key)


_current: State | None = None
"""Set by the callback for each invocation. Typer does not keep the click context active."""


def _state() -> State:
    assert _current is not None, "global options callback did not run"
    return _current


# ---------------------------------------------------------------------- output / errors


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _emit(value: Any, text: Callable[[], str]) -> None:
    """Print JSON in --json mode, else the lazily rendered text."""
    if _state().as_json:
        typer.echo(json.dumps(_dump(value)))
    else:
        typer.echo(text())


def _fail(code: str, message: str, exit_code: int) -> typer.Exit:
    if _state().as_json:
        typer.echo(json.dumps({"error": code, "message": message}))
    else:
        typer.echo(f"error[{code}]: {message}", err=True)
    return typer.Exit(exit_code)


def _guarded[**P](fn: Callable[P, None]) -> Callable[P, None]:
    """Map expected errors to messages and exit codes (2 = invalid input, 1 = other)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except ValidationError as exc:
            raise _fail(exc.code, str(exc), 2) from exc
        except DelegateError as exc:
            raise _fail(exc.code, str(exc), 1) from exc
        except PydanticValidationError as exc:
            raise _fail("invalid", _pydantic_message(exc), 2) from exc

    return wrapper


def _pydantic_message(exc: PydanticValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
    )


def _read_text(source: str) -> str:
    """Read ``-`` from stdin, otherwise a file path."""
    if source == "-":
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"Cannot read {source}: {exc.strerror or exc}") from exc


# ---------------------------------------------------------------------- callback


@app.callback()
def main(
    ctx: typer.Context,
    as_: Annotated[
        str | None,
        typer.Option(
            "--as",
            help="Actor id (default: $DELEGATE_AGENT). Without one, writes are refused. "
            "People use --as human.",
        ),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="Project key (default: $DELEGATE_PROJECT, else cwd).")
    ] = None,
    json_: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    global _current
    _current = State(actor=as_, project_key=project, as_json=json_, ctx=ctx)


# ---------------------------------------------------------------------- read commands


@app.command()
@_guarded
def status() -> None:
    """Project overview: counts, ready, yours, others, blocked."""
    st = _state()
    result = st.rt().service.project_status(st.project().id)
    _emit(result, lambda: render.status_text(result))


@app.command("list")
@_guarded
def list_(
    status: Annotated[list[Status] | None, typer.Option("--status", help="Repeatable.")] = None,
    assignee: Annotated[str | None, typer.Option()] = None,
    parent: Annotated[str | None, typer.Option(help="Only children of this task.")] = None,
    type_: Annotated[TaskType | None, typer.Option("--type", "-t", help="Only this task type.")] = None,
    ready: Annotated[bool, typer.Option("--ready", help="Only ready tasks.")] = False,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Match title/description.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Include done and cancelled.")] = False,
    limit: Annotated[int, typer.Option()] = 200,
) -> None:
    """List tasks, one line each."""
    st = _state()
    tasks = st.rt().service.list_tasks(
        st.project().id,
        statuses=status,
        assignee=assignee,
        parent=parent,
        task_type=type_,
        ready_only=ready,
        query=query,
        include_closed=all_,
        limit=limit,
    )
    _emit(tasks, lambda: "\n".join(render.summary_line(t) for t in tasks) or "No tasks.")


@app.command()
@_guarded
def show(ref: str) -> None:
    """Show one task in full."""
    st = _state()
    detail = st.rt().service.get_task_detail(ref, st.project().id)
    _emit(detail, lambda: render.detail_text(detail))


@app.command()
@_guarded
def projects(
    state: Annotated[
        list[ProjectState] | None,
        typer.Option("--state", help="Repeatable. Default: every state except deleted."),
    ] = None,
    query: Annotated[
        str | None, typer.Option("--query", "-q", help="Match name, path, or key.")
    ] = None,
) -> None:
    """List projects."""
    items = _state().rt().service.list_projects(states=state, query=query)
    _emit(items, lambda: "\n".join(render.project_line(p) for p in items) or "No projects.")


project_app = typer.Typer(help="Project lifecycle (humans only).", no_args_is_help=True)
app.add_typer(project_app, name="project")


@project_app.command("state")
@_guarded
def project_state(
    new_state: Annotated[
        ProjectState | None,
        typer.Argument(help="New state. Omit to show the current state."),
    ] = None,
) -> None:
    """Show or set the project state. Agents can only change tasks while in_progress."""
    st = _state()
    project = st.project()
    if new_state is not None:
        project = st.rt().service.set_project_state(project.id, new_state)
    _emit(project, lambda: f"{project.key} ({project.name}): {project.state.value}")


@app.command()
@_guarded
def agents(
    active: Annotated[int | None, typer.Option(help="Only agents seen in the last N minutes.")] = None,
) -> None:
    """List known agents."""
    items = _state().rt().service.list_agents(active_within_minutes=active)
    _emit(items, lambda: "\n".join(render.agent_line(a) for a in items) or "No agents.")


@app.command()
@_guarded
def events(
    since: Annotated[int | None, typer.Option(help="Only events with id greater than this.")] = None,
    limit: Annotated[int, typer.Option()] = 100,
    follow: Annotated[bool, typer.Option("--follow", help="Poll every second until Ctrl-C.")] = False,
) -> None:
    """Show project events. With --follow and no --since, start from now."""
    st = _state()
    svc = st.rt().service
    project_id = st.project().id
    last = since if since is not None else (svc.latest_event_id() if follow else 0)
    try:
        while True:
            batch = svc.events_since(last, project_id=project_id, limit=limit)
            for e in batch:
                typer.echo(json.dumps(e.model_dump(mode="json")) if st.as_json else render.event_line(e))
            if batch:
                last = batch[-1].id
            if not follow:
                if not batch and not st.as_json:
                    typer.echo("No events.")
                return
            if len(batch) < limit:
                time.sleep(1)
    except KeyboardInterrupt:
        return


@app.command()
@_guarded
def whoami() -> None:
    """Show the actor id and the resolved project."""
    st = _state()
    actor = resolve_actor(st.actor)
    project = st.project()
    _emit(
        {"actor": actor, "project": project.key},
        lambda: f"{actor} @ {project.key} ({project.name})",
    )


# ---------------------------------------------------------------------- write commands


@app.command()
@_guarded
def add(
    title: Annotated[str | None, typer.Argument(help="Task title (omit with --file).")] = None,
    desc: Annotated[str, typer.Option(help="Description.")] = "",
    acceptance: Annotated[str, typer.Option(help="Acceptance criteria.")] = "",
    priority: Annotated[str, typer.Option("--priority", "-p", help="P0-P3.")] = "P2",
    type_: Annotated[TaskType, typer.Option("--type", "-t", help="Task type.")] = TaskType.UNKNOWN,
    parent: Annotated[str | None, typer.Option()] = None,
    dep: Annotated[list[str] | None, typer.Option(help="Dependency ref. Repeatable.")] = None,
    file: Annotated[
        str | None, typer.Option("--file", help="JSON list of tasks to create; '-' reads stdin.")
    ] = None,
) -> None:
    """Create one task, or a batch with --file."""
    if (title is None) == (file is None):
        raise ValidationError("Give either a TITLE or --file, not both or neither")
    if file is not None:
        try:
            raw: object = json.loads(_read_text(file))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON in {file}: {exc}") from exc
        items = TypeAdapter(list[NewTask]).validate_python(raw)
    else:
        assert title is not None
        items = [
            NewTask(
                title=title,
                description=desc,
                acceptance=acceptance,
                priority=priority,
                type=type_,
                parent=parent,
                depends_on=dep or [],
            )
        ]
    st = _state()
    created = st.rt().service.create_tasks(st.project().id, items)
    _emit(created, lambda: "\n".join(render.task_line(t) for t in created))


@app.command()
@_guarded
def claim(
    ref: Annotated[str | None, typer.Argument(help="Task to claim; omit for the next ready task.")] = None,
    parent: Annotated[str | None, typer.Option(help="Only pick from this parent's children.")] = None,
    max_priority: Annotated[str | None, typer.Option(help="Lowest priority to accept, e.g. P1.")] = None,
) -> None:
    """Claim a task (or the next ready one) and start a lease."""
    st = _state()
    task = st.rt().service.claim_task(
        st.project().id, ref, parent=parent, max_priority=max_priority
    )
    _emit(task, lambda: "Nothing ready to claim." if task is None else render.task_line(task))


@app.command()
@_guarded
def update(
    ref: str,
    title: Annotated[str | None, typer.Option()] = None,
    desc: Annotated[str | None, typer.Option()] = None,
    acceptance: Annotated[str | None, typer.Option()] = None,
    priority: Annotated[str | None, typer.Option()] = None,
    status: Annotated[Status | None, typer.Option()] = None,
    type_: Annotated[TaskType | None, typer.Option("--type", "-t", help="Task type.")] = None,
    parent: Annotated[str | None, typer.Option(help="New parent task.")] = None,
    clear_parent: Annotated[bool, typer.Option("--clear-parent", help="Remove the parent.")] = False,
    add_dep: Annotated[list[str] | None, typer.Option()] = None,
    rm_dep: Annotated[list[str] | None, typer.Option()] = None,
    expect_version: Annotated[int | None, typer.Option(help="Fail if the task version differs.")] = None,
) -> None:
    """Change task fields, status, or dependencies."""
    st = _state()
    change = TaskUpdate(
        title=title,
        description=desc,
        acceptance=acceptance,
        priority=priority,
        status=status,
        type=type_,
        parent=parent,
        clear_parent=clear_parent,
        add_deps=add_dep or [],
        remove_deps=rm_dep or [],
    )
    task = st.rt().service.update_task(
        st.project().id, ref, change, expected_version=expect_version
    )
    _emit(task, lambda: render.task_line(task))


@app.command()
@_guarded
def note(
    ref: str,
    body: Annotated[str, typer.Argument(help="Note text; '-' reads stdin.")],
    kind: Annotated[str, typer.Option(help="progress, decision, blocker, or handoff.")] = "progress",
) -> None:
    """Add a note to a task."""
    st = _state()
    text = sys.stdin.read() if body == "-" else body
    result = st.rt().service.add_note(st.project().id, ref, kind, text)
    _emit(result, lambda: f"{result.task} note #{result.id} ({result.kind.value}) added")


@app.command()
@_guarded
def link(ref: str, kind: str, value: str) -> None:
    """Attach a link (file, commit, pr, url) to a task."""
    st = _state()
    links = st.rt().service.add_links(st.project().id, ref, [(kind, value)])
    _emit(links, lambda: render.links_text(ref, links))


def _parse_link(spec: str) -> tuple[str, str]:
    kind, sep, value = spec.partition("=")
    if not sep:
        raise ValidationError(f"Invalid --link {spec!r}; use KIND=VALUE")
    return kind, value


@app.command()
@_guarded
def done(
    ref: str,
    summary: str,
    review: Annotated[bool, typer.Option("--review", help="Move to review instead of done.")] = False,
    link: Annotated[list[str] | None, typer.Option("--link", help="KIND=VALUE. Repeatable.")] = None,
    expect_version: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Finish a task you hold, with a summary."""
    st = _state()
    task = st.rt().service.complete_task(
        st.project().id,
        ref,
        summary,
        links=[_parse_link(s) for s in link or []],
        to_review=review,
        expected_version=expect_version,
    )
    _emit(task, lambda: render.task_line(task))


@app.command()
@_guarded
def release(ref: str, reason: str) -> None:
    """Give a held task back to the queue with a handoff note."""
    st = _state()
    task = st.rt().service.release_task(st.project().id, ref, reason)
    _emit(task, lambda: render.task_line(task))


# ---------------------------------------------------------------------- servers


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio."""
    from delegate.mcp.server import run

    run()


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Default from settings.")] = None,
    port: Annotated[int | None, typer.Option(help="Default from settings.")] = None,
) -> None:
    """Run the web dashboard."""
    from delegate.web.app import run

    run(host=host, port=port)


if __name__ == "__main__":
    app()
