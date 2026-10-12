"""`delegate init` and `delegate project update`: wire a repo up for agents.

The delegate version and options that last wrote a repo's files are stored on the project
(``TaskService.record_setup``). Repos set up before that was tracked have their options
inferred from the files (``detect_options``).

Each step is idempotent and merges into existing files instead of replacing them:

* instructions block in CLAUDE.md / AGENTS.md (between marker comments)
* `.mcp.json` server entry (Claude Code project-scoped MCP config)
* `.claude/settings.json` allow rule for the MCP tools, so Claude Code does not prompt
* `.claude/settings.json` SessionStart hook (and removal of the Stop hook of older versions)
* git `post-commit` hook that links commits mentioning task refs
"""

from __future__ import annotations

import json
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from delegate import __version__
from delegate.core import Project, ProjectSetup, TaskService
from delegate.core.gitinfo import detect, run_git

BEGIN_MARK = "<!-- delegate:begin -->"
END_MARK = "<!-- delegate:end -->"
HOOK_BEGIN = "# delegate:begin"
HOOK_END = "# delegate:end"
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
"""Files searched for the instructions block when inferring options of an old setup."""


class InitOptions(BaseModel):
    """The options of ``delegate init``. Stored per project so ``project update`` can reuse them."""

    command: list[str] = Field(default_factory=lambda: ["delegate"], min_length=1)
    instructions_file: str = "CLAUDE.md"
    mcp: bool = True
    hooks: bool = True
    git_hook: bool = True

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> InitOptions | None:
        """Parse stored options. Returns None if the record is damaged."""
        try:
            return cls.model_validate(data)
        except PydanticValidationError:
            return None


def detect_options(root: Path) -> InitOptions | None:
    """Infer init options from the files that ``delegate init`` wrote.

    For repos set up before setup records existed. Returns None when no delegate file
    is found, which means the repo was never set up.
    """
    instructions = next(
        (
            name
            for name in INSTRUCTION_FILES
            if (root / name).is_file() and BEGIN_MARK in (root / name).read_text(encoding="utf-8")
        ),
        None,
    )
    mcp_command = _mcp_command(root)
    hook_command = _hook_command(root)
    git_hook = _git_hook_installed(root)
    if instructions is None and mcp_command is None and hook_command is None and not git_hook:
        return None
    return InitOptions(
        command=mcp_command or hook_command or ["delegate"],
        instructions_file=instructions or "CLAUDE.md",
        mcp=mcp_command is not None,
        hooks=hook_command is not None,
        git_hook=git_hook,
    )


def _mcp_command(root: Path) -> list[str] | None:
    """The delegate command from `.mcp.json` (its args end with ``mcp``), if present."""
    try:
        servers: object = _load_json_object(root / ".mcp.json").get("mcpServers", {})
    except ValueError:
        return None
    entry: object = cast(dict[str, Any], servers).get("delegate") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return None
    entry = cast(dict[str, Any], entry)
    command: object = entry.get("command")
    args: object = entry.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    parts = [command, *map(str, cast(list[object], args))]
    return parts[:-1] if parts[-1] == "mcp" and len(parts) > 1 else parts


def _hook_command(root: Path) -> list[str] | None:
    """The delegate command from the SessionStart hook in `.claude/settings.json`, if present."""
    try:
        hooks: object = _load_json_object(root / ".claude" / "settings.json").get("hooks", {})
    except ValueError:
        return None
    if not isinstance(hooks, dict):
        return None
    for group in cast(list[object], cast(dict[str, Any], hooks).get("SessionStart", [])):
        if not isinstance(group, dict):
            continue
        for hook in cast(list[object], cast(dict[str, Any], group).get("hooks", [])):
            text = str(cast(dict[str, Any], hook).get("command", "")) if isinstance(hook, dict) else ""
            suffix = " hook session-start"
            if text.endswith(suffix):
                return shlex.split(text.removesuffix(suffix)) or None
    return None


def _git_hook_installed(root: Path) -> bool:
    hooks_dir = run_git(root, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if hooks_dir is None:
        return False
    hook = Path(hooks_dir) / "post-commit"
    return hook.is_file() and HOOK_BEGIN in hook.read_text(encoding="utf-8")


def parse_version(value: str) -> tuple[int, ...] | None:
    """``"0.2.0"`` -> ``(0, 2, 0)``. Returns None for anything that is not dotted integers."""
    try:
        return tuple(int(part) for part in value.strip().split("."))
    except ValueError:
        return None


def is_newer(installed: str, recorded: str | None) -> bool:
    """True if ``installed`` is newer than ``recorded``. An unknown record counts as older."""
    if recorded is None:
        return True
    a, b = parse_version(installed), parse_version(recorded)
    if a is None or b is None:
        # Cannot order them; any difference means the repo files may be stale.
        return installed.strip() != recorded.strip()
    # Pad so "0.2" and "0.2.0" compare equal.
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def instructions_block(project: Project) -> str:
    return f"""{BEGIN_MARK}
## Task tracking (delegate)

This repo tracks work in **delegate** (project key `{project.key}`; tasks look like `{project.key}-42`).
Use the `delegate` MCP tools. They are the preferred method: they know your agent id and need no
permission prompts. Use the CLI only if the MCP tools are not available: pass your id with `--as`
on every command and add `--json`, e.g. `delegate --as codex#7f2a --json claim` (these options may
also go after the command). Pick one id
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


def _remove_stop_hook(hooks: dict[str, Any]) -> bool:
    """Remove the Stop hook that delegate < 0.2.1 wrote. Returns True if something was removed.

    Other Stop hooks stay. Groups and the ``Stop`` key are removed only when they become empty.
    """

    def is_delegate_stop(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        return str(cast(dict[str, Any], entry).get("command", "")).endswith(" hook stop")

    raw_groups: object = hooks.get("Stop")
    if not isinstance(raw_groups, list):
        return False
    changed = False
    kept_groups: list[object] = []
    for group in cast(list[object], raw_groups):
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        group = cast(dict[str, Any], group)
        raw_entries: object = group.get("hooks")
        if not isinstance(raw_entries, list):
            kept_groups.append(group)
            continue
        entries = cast(list[object], raw_entries)
        kept = [h for h in entries if not is_delegate_stop(h)]
        if len(kept) == len(entries):
            kept_groups.append(group)
            continue
        changed = True
        if kept:
            group["hooks"] = kept
            kept_groups.append(group)
    if not changed:
        return False
    if kept_groups:
        hooks["Stop"] = kept_groups
    else:
        del hooks["Stop"]
    return True


def write_claude_hooks(root: Path, command: list[str]) -> bool:
    """Add the SessionStart hook to `.claude/settings.json` without duplicating it.

    Also removes the delegate Stop hook of older versions. Returns True if the file changed.
    """
    path = root / ".claude" / "settings.json"
    data = _load_json_object(path)
    hooks = cast(dict[str, Any], data.setdefault("hooks", {}))
    changed = _remove_stop_hook(hooks)
    groups = cast(list[dict[str, Any]], hooks.setdefault("SessionStart", []))
    already = any(
        "hook session-start" in str(h.get("command", ""))
        for g in groups
        for h in cast(list[dict[str, Any]], g.get("hooks", []))
    )
    if not already:
        groups.append(_hook_entry(command, "session-start"))
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


@dataclass(slots=True, frozen=True)
class SetupInfo:
    """What `delegate project info` shows. ``options`` are stored, else inferred from files."""

    project: Project
    root: Path
    installed_version: str
    recorded: ProjectSetup | None
    options: InitOptions | None
    inferred: bool
    """True when ``options`` came from the repo files because there is no valid record."""

    @property
    def is_set_up(self) -> bool:
        return self.options is not None

    @property
    def update_available(self) -> bool:
        """True if the installed delegate is newer than the one that wrote the repo files."""
        return self.is_set_up and is_newer(
            self.installed_version, self.recorded.version if self.recorded else None
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "project": self.project.key,
            "root": str(self.root),
            "installed_version": self.installed_version,
            "setup_version": self.recorded.version if self.recorded else None,
            "setup_at": self.recorded.updated_at if self.recorded else None,
            "options": self.options.model_dump() if self.options else None,
            "options_inferred": self.inferred,
            "set_up": self.is_set_up,
            "update_available": self.update_available,
        }


def setup_info(service: TaskService, project: Project, root: Path) -> SetupInfo:
    recorded = service.get_setup(project.id)
    options = InitOptions.from_record(recorded.options) if recorded else None
    inferred = options is None
    if options is None:
        options = detect_options(root)
    return SetupInfo(
        project=project,
        root=root,
        installed_version=__version__,
        recorded=recorded,
        options=options,
        inferred=inferred and options is not None,
    )


def apply_setup(
    service: TaskService, cwd: Path, project: Project, options: InitOptions
) -> InitResult:
    """Write the repo files with ``options`` and record them with the installed version."""
    result = init_repo(cwd, project, options)
    service.record_setup(project.id, __version__, options.model_dump())
    return result


@dataclass(slots=True)
class InitResult:
    root: Path
    changed: list[str] = field(default_factory=list[str])
    unchanged: list[str] = field(default_factory=list[str])


def init_repo(cwd: Path, project: Project, options: InitOptions) -> InitResult:
    """Run all setup steps for the repo containing ``cwd``. Each step is idempotent."""
    root = detect(cwd).root
    result = InitResult(root=root)
    command = options.command

    def note(name: str, changed: bool) -> None:
        (result.changed if changed else result.unchanged).append(name)

    doc = root / options.instructions_file
    old = doc.read_text(encoding="utf-8") if doc.exists() else ""
    new = upsert_marked_block(old, instructions_block(project), BEGIN_MARK, END_MARK)
    if new != old:
        doc.write_text(new, encoding="utf-8")
    note(options.instructions_file, new != old)

    if options.mcp:
        note(".mcp.json", write_mcp_config(root, command))
        note(".claude/settings.json (permissions)", allow_mcp_tools(root))
    if options.hooks:
        note(".claude/settings.json (hooks)", write_claude_hooks(root, command))
    if options.git_hook:
        installed = install_git_hook(cwd, command)
        if installed is not None:
            note(str(installed[0]), installed[1])
    return result
