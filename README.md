# delegate

Delegate is a task tracker for coding agents. Agents use it through an MCP server or a CLI.
You monitor and steer their work in a local web dashboard.

- Agents claim tasks. A claim has a lease. If an agent stops, the lease expires and another agent can claim the task.
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
4. Adds a Claude Code **SessionStart** hook to `.claude/settings.json`. It puts the project status into the agent's context. (Versions before 0.2.1 also added a Stop hook. `delegate project update` removes it.)
5. Adds a git `post-commit` hook. Commits that mention a task ref (for example `DLG-12`) are linked to that task.

All steps are idempotent. They merge into existing files. Use `--no-mcp`, `--no-hooks`, or `--no-git-hook` to skip a step.
If `delegate` is not on your PATH, give the full command: `delegate init --command "uv run --project ~/src/delegate-ai delegate"`.

Delegate stores the version and options that set up each repo. If the repo is already set up (by any version), `delegate init` does nothing. To set it up again with different options, use `delegate init --force`.

## Upgrade delegate

1. Get the new code: `git pull` for an editable install, or `uv tool upgrade delegate`.
2. The database upgrades itself when a new `delegate` process opens it.
3. In each repo, run `delegate project info` to see the repo's setup version, the stored options, and whether an update is available.
4. Run `delegate project update` to rewrite the repo files with the stored options. Repos set up before 0.2.0 have no stored options. For them, delegate reads the options from the existing files.
5. Restart `delegate serve`, and run `/mcp` in open Claude Code sessions.

Both commands take `--project KEY` to work on another project's repo, and `--json`.

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
The global options `--as`, `--json`, and `--project` work before or after the command name (`delegate status --json` is the same as `delegate --json status`).

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
The **Columns** menu next to the filter box chooses which status columns the board shows. By default it shows To do, In progress, Review, and Done. The Done and Cancelled columns show the 20 most recently closed tasks.
The filter box above the board shows only the cards that match its text. It matches a task ref (`DLG-12`), the title, description, acceptance criteria, notes, and links. The same search is available to agents as `query` in `list_tasks` (MCP) and as `-q` in the CLI.

## Rules

- A task is **ready** when its status is `todo`, nobody holds it, and all its dependencies are `done` or `cancelled`.
- A claim does not depend on the status. An agent can claim any task that nobody holds. Claiming a `todo` task starts it (`in_progress`) and needs its dependencies done. In other statuses, the status stays. `claim_task` with no ref picks the highest-priority ready task that has no open subtasks.
- Only the holder can change a claimed task. Any agent can change an unclaimed task. All agents can add notes.
- `release_task` and an expired lease remove the claim and keep the status. `project_status` lists unclaimed `in_progress` tasks so that another agent can claim them by ref. Closing a task (`done`, `cancelled`) also removes the claim.
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
