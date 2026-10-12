# delegate

Delegate is a task tracker for coding agents. Agents use it through an MCP server or a CLI.
You monitor and steer their work in a local web dashboard.

- Agents claim tasks. A claim has a lease. If an agent stops, the lease expires and the task goes back to the queue.
- Tasks have subtasks, dependencies, priorities (P0–P3), types, notes, and links (commits, PRs, files, URLs).
- Many agents can work in parallel. Claims are atomic.
- All projects share one SQLite database at `~/.delegate/delegate.db`.

## Install

```sh
uv tool install --editable /path/to/delegate-ai   # puts `delegate` on your PATH
```

To run without installing, use `uv run --project /path/to/delegate-ai delegate ...`.

## Set up a repo

Run this in the repo that your agents work in:

```sh
delegate init
```

This command:

1. Registers the project (key from the repo name, for example `delegate-ai` → `DLG`). Use `--key` to choose the key. A new project starts as `not_started`. Agents cannot change its tasks until you start it (see [Project states](#project-states)).
2. Adds usage instructions to `CLAUDE.md` (use `--file AGENTS.md` for other agents).
3. Adds the MCP server to `.mcp.json`, and allows its tools in `.claude/settings.json` (`permissions.allow`: `mcp__delegate`), so Claude Code does not ask before each call.
4. Adds Claude Code hooks to `.claude/settings.json`:
   - **SessionStart** puts the project status into the agent's context.
   - **Stop** asks the agent one time to complete or release the tasks that it holds.
5. Adds a git `post-commit` hook. Commits that mention a task ref (for example `DLG-12`) are linked to that task.

All steps are idempotent. They merge into existing files. Use `--no-mcp`, `--no-hooks`, or `--no-git-hook` to skip a step.
If `delegate` is not on your PATH, give the full command: `delegate init --command "uv run --project ~/src/delegate-ai delegate"`.

## Agent workflow

| Step | MCP tool | CLI |
|---|---|---|
| See the state of the project | `project_status` | `delegate status` |
| Plan work | `create_tasks` | `delegate add "Title" --dep DLG-3` or `delegate add --file tasks.json` |
| Start work | `claim_task` | `delegate claim [REF]` |
| Record context | `add_note` | `delegate note DLG-4 --kind decision "..."` |
| Finish | `complete_task` | `delegate done DLG-4 "summary" --link commit=abc123` |
| Stop early | `release_task` | `delegate release DLG-4 "handoff notes"` |

Agents should use the MCP tools when they can. The CLI is the fallback.
CLI agents pass their id with `--as` on every command (for example `delegate --as codex#7f2a claim`). The `DELEGATE_AGENT` environment variable also works, but `--as` is preferred: every command then starts with `delegate`, which makes permission rules simpler. Without an identity, the CLI is `anonymous`: reads work, but every write is refused.
People use `--as human`. Humans bypass holder checks, so an agent must never use `human`. For the same reason, do not allow `Bash(delegate:*)` for agents: that rule would also allow `delegate --as human`.
Add `--json` to any CLI command for machine-readable output.

## Project states

Each project has a state: `not_started`, `in_progress`, `complete`, `inactive`, or `deleted`.

- Only humans can change the state: in the dashboard, or with `delegate --as human project state in_progress`.
- Agents can change tasks only when the project is `in_progress`. In other states, agent writes fail with `project_not_in_progress`. Humans can still edit tasks.
- `deleted` is a soft delete. The project is hidden from listings by default, and you can restore it by changing its state.
- `delegate projects --state deleted -q api` filters the list by state and searches by name, path, or key. The dashboard home page has the same filter and search.

## Dashboard

```sh
delegate serve            # http://127.0.0.1:3140
```

The dashboard shows a board for each project, task details, active agents, and a live activity feed.
You can create tasks, change status, priority, and type, and add notes. Agents see your notes.
Each card shows an icon for its task type.
The home page filters projects by state and searches by name, path, or key. You can change a project's state from its card or from the board header.

## Rules

- A task is **ready** when its status is `todo` and all its dependencies are `done` or `cancelled`.
- Only ready tasks can be claimed. `claim_task` with no ref picks the highest-priority ready task that has no open subtasks.
- Only the holder can change an `in_progress` task. All agents can add notes.
- Each write by an agent extends all of its leases. The default lease is 15 minutes.
- Dependency cycles and parent cycles are rejected.
- Each task has a type: `feature`, `bug`, `refactor`, `test`, `docs`, `design`, `research`, `chore`, or `unknown`. If you do not give a type, it is `unknown`. Set it with `type` (MCP) or `--type` (CLI). Filter lists with the same option.
- `update_task` accepts `expected_version`. If another actor changed the task first, the update fails.

## Configuration

Settings come from environment variables, then `~/.delegate/config.json`, then defaults.

| Env var | config.json key | Default |
|---|---|---|
| `DELEGATE_HOME` | – | `~/.delegate` |
| `DELEGATE_DB` | `db_path` | `$DELEGATE_HOME/delegate.db` |
| `DELEGATE_LEASE_MINUTES` | `lease_minutes` | `15` |
| `DELEGATE_HOST` / `DELEGATE_PORT` | `dashboard_host` / `dashboard_port` | `127.0.0.1` / `3140` |
| `DELEGATE_AGENT` | – | `anonymous`, read-only (CLI); random `claude#xxxx` (MCP); `git#hook` (post-commit hook) |
| `DELEGATE_AGENT_KIND` | – | `claude` (prefix for MCP agent ids) |
| `DELEGATE_PROJECT` | – | detected from the git repo of the current directory |

The database must be on a local disk. SQLite WAL mode does not work on network drives.

## Development

```sh
uv sync
uv run pytest -q
uv run pyright          # strict mode
```

Layout:

```
src/delegate/core/          domain: models, schema, TaskService (all business rules)
src/delegate/cli/           Typer CLI
src/delegate/mcp/           MCP server (mcp 2.x, MCPServer)
src/delegate/web/           FastAPI + Jinja2 + HTMX dashboard
src/delegate/integrations/  delegate init, Claude Code hooks, git hook
src/delegate/runtime.py     shared wiring: settings -> connection -> service -> project
```
