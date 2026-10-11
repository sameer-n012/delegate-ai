"""Detect the project a working directory belongs to, using git when available."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class RepoInfo:
    root: Path
    """Main worktree root. All worktrees of one repo share it."""
    name: str
    remote: str | None
    branch: str | None


def _git(cwd: Path, *args: str) -> str | None:
    """Run git and return stripped stdout, or None if git fails or is missing."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def normalize_remote(url: str) -> str:
    """Make ``git@host:a/b.git`` and ``https://host/a/b`` compare equal."""
    url = url.strip().removesuffix(".git").removesuffix("/")
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
        url = f"{host}/{path}"
    for prefix in ("https://", "http://", "ssh://git@", "ssh://", "git://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
            break
    return url.lower()


def detect(cwd: Path) -> RepoInfo:
    """Return repo info for ``cwd``. Outside git, the directory itself is the root."""
    cwd = cwd.resolve()
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return RepoInfo(root=cwd, name=cwd.name, remote=None, branch=None)
    common_path = Path(common)
    # For a normal repo or a linked worktree, the common dir is <main-root>/.git.
    # A bare repo has no .git suffix; fall back to the current toplevel then.
    if common_path.name == ".git":
        root = common_path.parent
    else:
        toplevel = _git(cwd, "rev-parse", "--show-toplevel")
        root = Path(toplevel) if toplevel else cwd
    remote = _git(cwd, "config", "--get", "remote.origin.url")
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return RepoInfo(
        root=root.resolve(),
        name=root.name,
        remote=normalize_remote(remote) if remote else None,
        branch=branch if branch != "HEAD" else None,
    )
