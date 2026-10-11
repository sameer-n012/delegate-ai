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

from delegate.integrations import hooks as hook_logic
from delegate.integrations.setup import init_repo
from delegate.runtime import open_runtime

hook_app = typer.Typer(help="Hook entry points (called by Claude Code and git, not by hand).")


def init_command(
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
    no_mcp: Annotated[bool, typer.Option("--no-mcp", help="Skip .mcp.json.")] = False,
    no_hooks: Annotated[bool, typer.Option("--no-hooks", help="Skip Claude Code hooks.")] = False,
    no_git_hook: Annotated[bool, typer.Option("--no-git-hook", help="Skip the git post-commit hook.")] = False,
) -> None:
    """Set up this repo for agents: instructions, MCP config, Claude Code hooks, git hook."""
    cmd = shlex.split(command)
    if not cmd:
        raise typer.BadParameter("--command must not be empty")
    rt = open_runtime()
    try:
        cwd = Path.cwd()
        project = rt.service.ensure_project(cwd, key=key)
        result = init_repo(
            cwd,
            project,
            command=cmd,
            instructions_file=instructions_file,
            mcp=not no_mcp,
            hooks=not no_hooks,
            git_hook=not no_git_hook,
        )
    finally:
        rt.close()
    typer.echo(f"Project {project.name} ({project.key}) at {result.root}")
    for name in result.changed:
        typer.echo(f"  updated   {name}")
    for name in result.unchanged:
        typer.echo(f"  unchanged {name}")


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


@hook_app.command("stop")
def stop() -> None:
    """Claude Code Stop: ask the agent once to complete or release tasks it still holds."""
    data = _read_hook_input()
    try:
        rt = open_runtime()
        try:
            project = rt.project_for(_hook_cwd(data))
            decision = hook_logic.stop_decision(rt.service, project, data)
        finally:
            rt.close()
    except Exception as exc:  # hooks must not break the host tool; report and exit 0
        typer.echo(f"delegate stop hook failed: {exc}", err=True)
        return
    if decision is not None:
        typer.echo(json.dumps(decision))


@hook_app.command("post-commit")
def post_commit() -> None:
    """git post-commit: link HEAD to tasks mentioned in its message."""
    cwd = Path.cwd()
    try:
        head = hook_logic.head_commit(cwd)
        if head is None:
            return
        rt = open_runtime()
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
