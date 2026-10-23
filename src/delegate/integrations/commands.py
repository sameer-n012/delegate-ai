"""Typer commands for `delegate init` and `delegate hook ...`.

Hooks run inside other tools (Claude Code, git). They must never break those tools:
every hook catches its own errors, reports them on stderr, and exits 0.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from delegate import __version__
from delegate.integrations import hooks as hook_logic
from delegate.integrations.setup import InitOptions, InitResult, apply_setup, setup_info
from delegate.core import ANONYMOUS, DelegateError, Project, ProjectState
from delegate.core.gitinfo import detect
from delegate.runtime import open_runtime, resolve_actor

GIT_HOOK_ACTOR = "git#hook"

hook_app = typer.Typer(help="Hook entry points (called by Claude Code and git, not by hand).")


def init_command(
    ctx: typer.Context,
    command: Annotated[
        str,
        typer.Option(
            "--command",
            help='How hooks and .mcp.json invoke delegate, e.g. "uv run --project ~/src/delegate delegate".',
        ),
    ] = "delegate",
    instructions_file: Annotated[
        str, typer.Option("--file", help="Instructions file to update (CLAUDE.md or AGENTS.md).")
    ] = "CLAUDE.md",
    key: Annotated[
        str | None, typer.Option("--key", help="Project key to use when registering a new project.")
    ] = None,
    no_mcp: Annotated[
        bool, typer.Option("--no-mcp", help="Skip .mcp.json and the MCP tool allow rule.")
    ] = False,
    no_hooks: Annotated[bool, typer.Option("--no-hooks", help="Skip Claude Code hooks.")] = False,
    no_git_hook: Annotated[bool, typer.Option("--no-git-hook", help="Skip the git post-commit hook.")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Run again on a repo that is already set up, with these options.")
    ] = False,
    relink: Annotated[
        str | None,
        typer.Option(
            "--relink",
            metavar="KEY",
            help="Move an existing project (e.g. DLG) to this repo after its folder moved. "
            "Humans only: delegate --as human init --relink DLG.",
        ),
    ] = None,
) -> None:
    """Set up this repo for agents: instructions, MCP config, Claude Code hooks, git hook.

    Does nothing on a repo that is already set up (by any version). Use `delegate project
    update` to refresh it, or `--force` to set it up again with new options.

    With `--relink KEY`, first point project KEY at this repo (see
    `TaskService.relink_project`). An empty project registered for this folder is removed.
    """
    cmd = shlex.split(command)
    if not cmd:
        raise typer.BadParameter("--command must not be empty")
    if relink is not None and key is not None:
        raise typer.BadParameter("--key names a new project; --relink uses an existing one")
    options = InitOptions(
        command=cmd,
        instructions_file=instructions_file,
        mcp=not no_mcp,
        hooks=not no_hooks,
        git_hook=not no_git_hook,
    )
    # The global --as option is a parameter of the root (app callback) context.
    actor = cast("str | None", ctx.find_root().params.get("as_"))
    rt = open_runtime(actor)
    try:
        cwd = Path.cwd()
        if relink is not None:
            try:
                project = rt.service.relink_project(relink, cwd)
            except DelegateError as exc:
                typer.echo(f"error[{exc.code}]: {exc}", err=True)
                raise typer.Exit(1) from exc
            typer.echo(f"Project {project.name} ({project.key}) now points at {project.root_path}")
        else:
            project = rt.service.ensure_project(cwd, key=key)
        info = setup_info(rt.service, project, detect(cwd).root)
        if info.is_set_up and not force:
            version = info.recorded.version if info.recorded else "an older version"
            typer.echo(
                f"Project {project.name} ({project.key}) is already set up (delegate {version}). "
                "Nothing changed."
            )
            typer.echo(
                "Run `delegate project update` to refresh it, or `delegate init --force` "
                "to set it up again with new options."
            )
            return
        result = apply_setup(rt.service, cwd, project, options)
    finally:
        rt.close()
    print_setup_result(project, result)


def print_setup_result(project: Project, result: InitResult) -> None:
    """Print which files a setup run changed, and how to start a project that is not started."""
    typer.echo(
        f"Project {project.name} ({project.key}) at {result.root} [{project.state.value}], "
        f"delegate {__version__}"
    )
    for name in result.changed:
        typer.echo(f"  updated   {name}")
    for name in result.unchanged:
        typer.echo(f"  unchanged {name}")
    if project.state != ProjectState.IN_PROGRESS:
        typer.echo(
            "Agents cannot change tasks until the project is in progress. Start it from the "
            "dashboard or run: delegate --as human project state in_progress"
        )


def _read_hook_input() -> dict[str, Any]:
    """Parse the hook's stdin JSON. Empty or non-JSON input yields an empty dict."""
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        typer.echo("delegate hook: ignoring non-JSON stdin", err=True)
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _hook_cwd(data: dict[str, Any]) -> Path:
    return Path(str(data.get("cwd") or Path.cwd()))


@hook_app.command("session-start")
def session_start() -> None:
    """Claude Code SessionStart: print a project summary into the agent's context."""
    data = _read_hook_input()
    try:
        rt = open_runtime()
        try:
            project = rt.project_for(_hook_cwd(data))
            typer.echo(hook_logic.session_start_context(rt.service, project))
        finally:
            rt.close()
    except Exception as exc:  # hooks must not break the host tool; report and exit 0
        typer.echo(f"delegate session-start hook failed: {exc}", err=True)


@hook_app.command("stop", hidden=True)
def stop() -> None:
    """Removed in 0.2.1; does nothing.

    Kept for repos whose `.claude/settings.json` still calls it (until `delegate project
    update` removes the hook). An unknown subcommand exits 2, which Claude Code treats as
    a blocking Stop hook error.
    """


@hook_app.command("post-commit")
def post_commit() -> None:
    """git post-commit: link HEAD to tasks mentioned in its message."""
    cwd = Path.cwd()
    try:
        head = hook_logic.head_commit(cwd)
        if head is None:
            return
        # The hook needs a write identity. Use the committing agent's id when it is set,
        # else a dedicated hook identity (never an implicit human).
        actor = resolve_actor()
        rt = open_runtime(GIT_HOOK_ACTOR if actor == ANONYMOUS else actor)
        try:
            project = rt.project_for(cwd)
            linked = hook_logic.link_commit(rt.service, project, *head)
        finally:
            rt.close()
    except Exception as exc:  # never fail a commit because of the tracker
        typer.echo(f"delegate post-commit hook failed: {exc}", err=True)
        return
    if linked:
        typer.echo(f"delegate: linked {head[0][:8]} to {', '.join(linked)}")
