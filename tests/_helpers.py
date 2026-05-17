"""Test helpers: ergonomic temp-git-repo factory.

Each test module does its own `sys.path` bootstrap (see `bootstrap_paths()`
below) before `from _helpers import …`, since the smart-test hook invokes
test files via path-import and the `tests` package isn't on sys.path."""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional


def bootstrap_paths() -> None:
    """Make the package root and the tests directory both importable."""
    here = Path(__file__).resolve().parent
    pkg = here.parent
    for p in (str(pkg), str(here)):
        if p not in sys.path:
            sys.path.insert(0, p)


def bootstrap_git_config_global() -> None:
    """Inject `protocol.file.allow=always` into the test process's
    `GIT_CONFIG_GLOBAL` so idlegit's own subprocess calls — which
    inherit `os.environ` and don't have the `-c` injection that
    `_run` applies — can fetch / pull / push against `file://` remote
    paths created inside the test fixtures.

    Git 2.38 (Oct 2022) hardened the default to refuse `transport
    'file'` unless `protocol.file.allow` is set, which broke the CI
    runs of integration tests that exercise the pull / propagate
    pipelines through idlegit's `git()` helper. The dev machine
    happened to have an older git or a permissive user config and
    didn't notice.

    Implementation: write a tiny throwaway gitconfig that contains
    only the `[protocol "file"] allow = always` directive, point
    `GIT_CONFIG_GLOBAL` at it for the rest of the process lifetime,
    and clean up the file on interpreter shutdown. We touch only
    `os.environ` — the user's real `~/.gitconfig` is unaffected.

    `_run` already calls `env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)`
    on its env copy; once we set `os.environ["GIT_CONFIG_GLOBAL"]`
    here, that setdefault becomes a no-op so `_run` also reads the
    permissive file. `_run`'s explicit `-c protocol.file.allow=always`
    is then redundant but harmless."""
    fd, path = tempfile.mkstemp(prefix="idlegit-test-gitconfig-")
    with os.fdopen(fd, "w") as f:
        # `user.name` / `user.email` are required for any git operation
        # that creates a commit (merge fallback, propagate-bump, etc.)
        # On CI there's no ~/.gitconfig and `_run`'s GIT_AUTHOR_NAME /
        # GIT_COMMITTER_NAME env vars only apply to subprocesses _run
        # itself spawns — idlegit's `git()` helper (with its own env)
        # would otherwise fail with "Please tell me who you are."
        f.write(
            "[user]\n"
            "\tname = idlegit-test\n"
            "\temail = test@idlegit.local\n"
            "[protocol \"file\"]\n"
            "\tallow = always\n"
        )
    os.environ["GIT_CONFIG_GLOBAL"] = path

    def _cleanup() -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
    atexit.register(_cleanup)


bootstrap_paths()
bootstrap_git_config_global()


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
    env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    env.setdefault("GIT_CONFIG_SYSTEM", os.devnull)
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
