"""Test helpers: ergonomic temp-git-repo factory.

Each test module does its own `sys.path` bootstrap (see `bootstrap_paths()`
below) before `from _helpers import …`, since the smart-test hook invokes
test files via path-import and the `tests` package isn't on sys.path."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


def bootstrap_paths() -> None:
    """Make the package root and the tests directory both importable."""
    here = Path(__file__).resolve().parent
    pkg = here.parent
    for p in (str(pkg), str(here)):
        if p not in sys.path:
            sys.path.insert(0, p)


bootstrap_paths()


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and capture output. We force a deterministic git
    identity + main branch so init/commit don't depend on the host's
    .gitconfig. Local file:// submodule URLs are explicitly allowed for
    `git submodule add` (recent git versions block them by default)."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "idlegit-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@idlegit.local")
    env.setdefault("GIT_COMMITTER_NAME", "idlegit-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@idlegit.local")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    cmd = list(args)
    if cmd and cmd[0] == "git":
        cmd = ["git", "-c", "protocol.file.allow=always", *cmd[1:]]
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        check=check, env=env,
    )


def make_repo(parent: Path, name: str,
              with_initial_commit: bool = True,
              branch: str = "main") -> Path:
    """Create `parent/name/` as a fresh git repo. Optionally make one
    initial commit on `branch` so HEAD exists. Returns the repo path."""
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "git", "init", "-q", "-b", branch)
    if with_initial_commit:
        readme = repo / "README.md"
        readme.write_text(f"# {name}\n")
        _run(repo, "git", "add", "README.md")
        _run(repo, "git", "commit", "-q", "-m", "init")
    return repo


def add_origin(repo: Path, url: str) -> None:
    _run(repo, "git", "remote", "add", "origin", url)


def write_file(repo: Path, relpath: str, contents: str = "x\n") -> Path:
    """Write a file inside `repo`, creating parent directories."""
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contents)
    return p


def stage_and_commit(repo: Path, message: str,
                     paths: Optional[Iterable[str]] = None) -> None:
    if paths is None:
        _run(repo, "git", "add", "-A")
    else:
        _run(repo, "git", "add", *list(paths))
    _run(repo, "git", "commit", "-q", "-m", message)


def make_repo_model(rel: str = "r", **kwargs):
    """Phantom Repo dataclass instance — no filesystem touched. The
    on-disk counterpart is `make_repo()` above."""
    from core.models import Repo
    return Repo(rel=rel, path=Path(f"/tmp/{rel}"), **kwargs)


def make_state(*repos, **kwargs):
    """State factory for unit tests. Defaults workspace_name='ws' so
    callers don't have to repeat it; any kwarg the caller passes wins."""
    from core.models import State
    kwargs.setdefault("workspace_name", "ws")
    return State(repos=list(repos), **kwargs)
