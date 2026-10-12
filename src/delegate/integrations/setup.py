"""`delegate init`: wire a repo up for agents.

Each step is idempotent and merges into existing files instead of replacing them:

* instructions block in CLAUDE.md / AGENTS.md (between marker comments)
* `.mcp.json` server entry (Claude Code project-scoped MCP config)
* `.claude/settings.json` allow rule for the MCP tools, so Claude Code does not prompt
* `.claude/settings.json` SessionStart and Stop hooks
* git `post-commit` hook that links commits mentioning task refs
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from delegate.core import Project
from delegate.core.gitinfo import detect, run_git

BEGIN_MARK = "<!-- delegate:begin -->"
END_MARK = "<!-- delegate:end -->"
HOOK_BEGIN = "# delegate:begin"
HOOK_END = "# delegate:end"


def instructions_block(project: Project) -> str:
    return f"""{BEGIN_MARK}
## Task tracking (delegate)

This repo tracks work in **delegate** (project key `{project.key}`; tasks look like `{project.key}-42`).
Use the `delegate` MCP tools. They are the preferred method: they know your agent id and need no
permission prompts. Use the CLI only if the MCP tools are not available: pass your id with `--as`
on every command and add `--json`, e.g. `delegate --as codex#7f2a --json claim`. Pick one id
(`<tool>#<4 hex>`) per session and keep it. Without `--as`, every write is refused; never use `human`.
Agents can change tasks only while the project is `in_progress`; if it is not, ask the user.

1. Start with `project_status` to see ready work, your claims, and blockers.
2. `claim_task` before you work on anything (no ref = next ready task). Claims expire if you go quiet.
3. Record `decision` and `progress` notes with `add_note` as you go; use `blocker` notes and status `blocked` when stuck.
4. Break large work into subtasks with `create_tasks` (use `parent` and `depends_on`).
5. Finish with `complete_task` (summary + commit/PR links), or `release_task` with a handoff note if you stop early.
6. Mention task refs (e.g. `{project.key}-42`) in commit messages; they are linked automatically.
{END_MARK}
"""


def upsert_marked_block(text: str, block: str, begin: str, end: str) -> str:
    """Replace the text between markers, or append the block if the markers are absent."""
    start = text.find(begin)
    stop = text.find(end, start + 1) if start != -1 else -1
    if start != -1 and stop != -1:
        # Keep everything after the end marker line.
        after = text[stop + len(end) :].lstrip("\n")
        return text[:start] + block.rstrip("\n") + "\n" + (("\n" + after) if after else "")
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix it or skip this step") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], data)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_mcp_config(root: Path, command: list[str]) -> bool:
    """Add/refresh the `delegate` server in `.mcp.json`. Returns True if the file changed."""
    path = root / ".mcp.json"
    data = _load_json_object(path)
    servers = cast(dict[str, Any], data.setdefault("mcpServers", {}))
    entry = {"type": "stdio", "command": command[0], "args": [*command[1:], "mcp"]}
    if servers.get("delegate") == entry:
        return False
    servers["delegate"] = entry
    _write_json(path, data)
    return True


def _hook_entry(command: list[str], sub: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": " ".join([*command, "hook", sub])}]}


def write_claude_hooks(root: Path, command: list[str]) -> bool:
    """Add SessionStart and Stop hooks to `.claude/settings.json` without duplicating them."""
    path = root / ".claude" / "settings.json"
    data = _load_json_object(path)
    hooks = cast(dict[str, Any], data.setdefault("hooks", {}))
    changed = False
    for event, sub in (("SessionStart", "session-start"), ("Stop", "stop")):
        groups = cast(list[dict[str, Any]], hooks.setdefault(event, []))
        marker = f"hook {sub}"
        already = any(
            marker in str(h.get("command", ""))
            for g in groups
            for h in cast(list[dict[str, Any]], g.get("hooks", []))
        )
        if not already:
            groups.append(_hook_entry(command, sub))
            changed = True
    if changed:
        _write_json(path, data)
    return changed


MCP_ALLOW_RULE = "mcp__delegate"
"""Claude Code permission rule that allows every tool of the `delegate` MCP server."""


def allow_mcp_tools(root: Path) -> bool:
    """Add the delegate MCP tools to `permissions.allow` in `.claude/settings.json`.

    This stops Claude Code from asking before each tracker call. Returns True if the file changed.
    """
    path = root / ".claude" / "settings.json"
    data = _load_json_object(path)
    permissions = cast(dict[str, Any], data.setdefault("permissions", {}))
    allow = cast(list[str], permissions.setdefault("allow", []))
    if MCP_ALLOW_RULE in allow:
        return False
    allow.append(MCP_ALLOW_RULE)
    _write_json(path, data)
    return True


def install_git_hook(cwd: Path, command: list[str]) -> tuple[Path, bool] | None:
    """Add a post-commit hook (shared by all worktrees).

    Returns ``(path, changed)``, or None outside git.
    """
    hooks_dir_raw = run_git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if hooks_dir_raw is None:
        return None
    hook = Path(hooks_dir_raw) / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    # `|| true`: a tracker failure must never break a commit.
    body = f"{HOOK_BEGIN}\n{' '.join(command)} hook post-commit >/dev/null 2>&1 || true\n{HOOK_END}\n"
    old = hook.read_text(encoding="utf-8") if hook.exists() else None
    new = upsert_marked_block(old or "#!/bin/sh\n", body, HOOK_BEGIN, HOOK_END)
    if new != old:
        hook.write_text(new, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook, new != old


@dataclass(slots=True)
class InitResult:
    root: Path
    changed: list[str] = field(default_factory=list[str])
    unchanged: list[str] = field(default_factory=list[str])


def init_repo(
    cwd: Path,
    project: Project,
    *,
    command: list[str],
    instructions_file: str = "CLAUDE.md",
    mcp: bool = True,
    hooks: bool = True,
    git_hook: bool = True,
) -> InitResult:
    """Run all setup steps for the repo containing ``cwd``."""
    root = detect(cwd).root
    result = InitResult(root=root)

    def note(name: str, changed: bool) -> None:
        (result.changed if changed else result.unchanged).append(name)

    doc = root / instructions_file
    old = doc.read_text(encoding="utf-8") if doc.exists() else ""
    new = upsert_marked_block(old, instructions_block(project), BEGIN_MARK, END_MARK)
    if new != old:
        doc.write_text(new, encoding="utf-8")
    note(instructions_file, new != old)

    if mcp:
        note(".mcp.json", write_mcp_config(root, command))
        note(".claude/settings.json (permissions)", allow_mcp_tools(root))
    if hooks:
        note(".claude/settings.json (hooks)", write_claude_hooks(root, command))
    if git_hook:
        installed = install_git_hook(cwd, command)
        if installed is not None:
            note(str(installed[0]), installed[1])
    return result
