"""Everything that talks to git: subprocess wrappers, discovery,
synchronization, suggestion, LFS, working-tree state queries. No curses
imports here — these functions all run on background threads (or
synchronously at startup) and never touch the screen."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.state.action_target import TargetState
from .state.action_menu import CommitEntry, FileEntry
from .state.review import FileChange, LFSCandidate
from .state.repos import ChildRef, Repo, WorkflowInfo, WorkflowInput
from .state.safe_merge import ConflictFile, ConflictHunk, MergeSide
from .state.store import ChildStatusSnapshot, RepoStatusSnapshot
from .state.workspaces import SubtreeSpec


def _find_embedded_gitlinks(base: Path,
                            repo_path: Path) -> List[str]:
    """Walk `base` looking for directories that contain a `.git` (dir
    or file — submodule gitfiles count). Returns each finding's path
    relative to `repo_path`, with forward slashes and no trailing
    slash. Does NOT recurse into found nested repos.

    Used by `safe_stage_all` to find embedded gitlinks under untracked
    directories (where `git status --porcelain` reports the *parent*
    untracked dir, not the deeper path containing the `.git`)."""
    found: List[str] = []
    if not base.is_dir():
        return found
    for root, dirs, files in os.walk(base):
        if ".git" in dirs or ".git" in files:
            rel = Path(root).relative_to(repo_path).as_posix()
            found.append(rel)
            # Don't recurse into the embedded repo's contents.
            dirs[:] = [d for d in dirs if d != ".git"]
            continue
    return found

# git status XY codes that indicate an unmerged path.
CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _iter_porcelain_z_entries(out: str):
    """Yield `(xy, path)` tuples from the output of
    `git status --porcelain=v1 -z`, correctly skipping the
    source-path follow-up token that `R` (rename) and `C` (copy)
    entries emit.

    `-z` mode encodes a rename as TWO NUL-separated tokens —
    `R<flag> destpath` followed by `srcpath` — and a naive
    `out.split("\\x00")` loop reads `srcpath` as a third row. That
    row has no `XY ` prefix, so a downstream `entry[3:]` slice would
    chop the first three characters of the source path (`idlegit.conf`
    → `egit.conf`) and pass the corrupt path to `git add`, producing
    `fatal: pathspec 'egit.conf' did not match any files`. Centralising
    the parser here keeps every status reader consistent — only one
    place to update if the porcelain format ever changes again."""
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path = entry[3:]
        # R/C entries carry an extra source-path token under -z;
        # consume it here so callers don't have to.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i < len(parts):
                i += 1
        yield xy, path


def _status_xy_from_v2(raw: str) -> str:
    return raw.replace(".", " ")


def _parse_porcelain_v2_z(repo: Repo, out: str) -> None:
    """Populate branch/upstream/ahead/behind/status fields from v2 status."""
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        if entry.startswith("# "):
            tokens = entry.split()
            if len(tokens) >= 3 and tokens[1] == "branch.oid":
                repo.head = "" if tokens[2] == "(initial)" else tokens[2]
            elif len(tokens) >= 3 and tokens[1] == "branch.head":
                repo.branch = tokens[2] or "(detached)"
            elif len(tokens) >= 3 and tokens[1] == "branch.upstream":
                repo.upstream = tokens[2]
            elif len(tokens) >= 4 and tokens[1] == "branch.ab":
                try:
                    repo.ahead = int(tokens[2].lstrip("+"))
                    repo.behind = int(tokens[3].lstrip("-"))
                except ValueError:
                    pass
            continue
        kind = entry[0]
        if kind == "?":
            path = entry[2:] if len(entry) > 2 else ""
            if path:
                repo.untracked.append(path)
            continue
        if kind == "!":
            continue
        if kind == "1":
            fields = entry.split(" ", 8)
        elif kind == "2":
            fields = entry.split(" ", 9)
        elif kind == "u":
            fields = entry.split(" ", 10)
        else:
            fields = entry.split()
        if len(fields) < 2:
            continue
        xy = _status_xy_from_v2(fields[1])
        path = fields[-1]
        if kind == "2" and i < len(parts):
            # Renames/copies carry an extra original-path token under -z.
            i += 1
        if xy in CONFLICT_CODES or kind == "u":
            repo.merging = True
            repo.conflict_paths.append(path)
            continue
        x, y = xy[0], xy[1]
        if x != " ":
            repo.staged.append((x, path))
        if y != " ":
            repo.unstaged.append((y, path))


def _read_gitmodules_submodules(repo_path: Path) -> List[Tuple[str, Path]]:
    if not (repo_path / ".gitmodules").exists():
        return []
    rc, out, _ = git(repo_path, [
        "config", "-f", ".gitmodules",
        "--get-regexp", r"submodule\..+\.(path|url)",
    ])
    if rc != 0:
        return []
    by_name: Dict[str, Dict[str, str]] = {}
    for line in out.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        key, value = parts
        if not key.startswith("submodule."):
            continue
        if key.endswith(".path"):
            name = key[len("submodule."):-len(".path")]
            by_name.setdefault(name, {})["path"] = value.strip()
        elif key.endswith(".url"):
            name = key[len("submodule."):-len(".url")]
            by_name.setdefault(name, {})["url"] = value.strip()
    nested: List[Tuple[str, Path]] = []
    for item in by_name.values():
        path_str = item.get("path", "")
        url = item.get("url", "")
        if not path_str or not url:
            continue
        nested.append((canonicalize_url(url), (repo_path / path_str).resolve()))
    return nested

# .git/<marker> files/dirs that mean a merge-like operation is in progress.
MERGE_MARKER_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
MERGE_MARKER_DIRS = ("rebase-merge", "rebase-apply")

DEFAULT_GIT_TIMEOUT_SECONDS = 120
DEFAULT_GH_TIMEOUT_SECONDS = 60
MAX_PARALLEL_GIT_JOBS = max(4, min(16, (os.cpu_count() or 4) * 2))


# ---------- Subprocess + small helpers -------------------------------------


def _git_env() -> dict:
    env = os.environ.copy()
    # Background workers cannot answer credential prompts. Failing fast
    # keeps the TUI from hanging behind a hidden git prompt.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def git(path: Path, args: List[str],
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    """Run git with `cwd=path`. Resilient to a missing/inaccessible cwd
    (returns rc=1 with the OSError message instead of raising) so the
    caller can treat it as any other git failure — important when a repo
    folder is removed under us between refreshes. Calls are bounded and
    non-interactive so background workers can't hang forever behind a
    credential prompt, hook, or stalled remote."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except OSError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"git timed out after {timeout:g}s"
    return p.returncode, p.stdout, p.stderr


def _signal_process_group(proc: subprocess.Popen, sig: int) -> bool:
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, sig)
            return True
        except OSError:
            return False
    return False


def _terminate_cancellable_process(proc: subprocess.Popen) -> None:
    if not _signal_process_group(proc, signal.SIGTERM):
        proc.terminate()


def _kill_cancellable_process(proc: subprocess.Popen) -> None:
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    if not _signal_process_group(proc, kill_signal):
        proc.kill()


def git_cancellable(
        path: Path, args: List[str],
        cancel_event: "Optional[object]" = None,
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        poll_interval: float = 0.25) -> Tuple[int, str, str]:
    """Like `git()` but polls `cancel_event` (a `threading.Event`) while
    waiting on the subprocess so a user-initiated Cancel can terminate
    a long-running git operation (push, pull, fetch). Returns the same
    `(rc, stdout, stderr)` tuple; on cancel, `rc == 130` (the standard
    "terminated by signal" code) and stderr names the action.

    Uses `Popen` directly so we hold the handle for termination. Falls
    back to plain `git()` behaviour when `cancel_event` is None — no
    subprocess overhead for paths that don't need cancellation."""
    if cancel_event is None:
        return git(path, args, timeout=timeout)
    try:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_git_env(),
            start_new_session=True,
        )
    except OSError as e:
        return 1, "", str(e)
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=poll_interval)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            pass
        if cancel_event.is_set():
            # Terminate first (SIGTERM), then kill if it doesn't yield.
            err = ""
            try:
                _terminate_cancellable_process(proc)
                _, err = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    _kill_cancellable_process(proc)
                except OSError:
                    pass
                try:
                    _, err = proc.communicate(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
            return 130, "", err or "cancelled"
        if time.monotonic() > deadline:
            try:
                _kill_cancellable_process(proc)
            except OSError:
                pass
            try:
                _, err = proc.communicate(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                err = ""
            return 124, "", err or f"git timed out after {timeout:g}s"


def git_bounded_output(path: Path, args: List[str],
                       max_bytes: int,
                       timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS
                       ) -> Tuple[int, str, str, bool]:
    """Run git and capture at most `max_bytes` of stdout/stderr combined.

    Used by viewer-style code where the full command output can be very
    large. Returns (rc, out, err, truncated)."""
    try:
        p = subprocess.Popen(
            ["git", *args],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
        )
        try:
            out_b, err_b = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out_b, err_b = p.communicate()
            return 124, "", f"git timed out after {timeout:g}s", False
    except OSError as e:
        return 1, "", str(e), False

    combined_len = len(out_b) + len(err_b)
    truncated = combined_len > max_bytes
    if truncated:
        remaining = max_bytes
        out_b = out_b[:remaining]
        remaining -= len(out_b)
        err_b = err_b[:max(0, remaining)]
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    return p.returncode, out, err, truncated


def is_safe_ref_arg(ref: str) -> bool:
    """True when `ref` is safe to pass as a git/gh positional ref.

    argv-list subprocesses avoid shell injection, but git/gh can still
    parse leading-dash refs as options in many positions. Refuse those
    instead of trying to disambiguate every command's grammar."""
    return bool(ref) and not ref.startswith("-")


def first_line(text: str) -> str:
    """First non-blank line of `text`, stripped. Returns the sentinel
    `"(no output)"` when input is empty or whitespace-only — task-row
    messages prefer that to a blank cell when surfacing git's stderr."""
    if not text:
        return "(no output)"
    for ln in text.strip().splitlines():
        if ln.strip():
            return ln.strip()
    return "(no output)"


def canonicalize_url(url: str) -> str:
    """Normalize a git remote URL so HTTPS / SSH / trailing-slash variants match."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url[4:].replace(":", "/", 1)
    elif "://" in url:
        url = url.split("://", 1)[1]
        if "@" in url.split("/", 1)[0]:
            host_and_path = url.split("/", 1)
            host = host_and_path[0].rsplit("@", 1)[-1]
            url = host + "/" + host_and_path[1] if len(host_and_path) > 1 else host
    return url.lower()


# ---------- Repo refresh (populates a Repo from git state) -----------------


@dataclass(frozen=True)
class RepoRefreshSnapshot:
    """Typed repo refresh result before temporary projection fields are updated."""

    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    remote_url: Optional[str] = None
    remote_url_raw: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    staged: List[Tuple[str, str]] = field(default_factory=list)
    unstaged: List[Tuple[str, str]] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    nested_subs: List[Tuple[str, Path]] = field(default_factory=list)
    error: str = ""
    merging: bool = False
    conflict_paths: List[str] = field(default_factory=list)
    workflows: List[WorkflowInfo] = field(default_factory=list)
    workflow_states_hydrated: bool = False
    message: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked)

    def status_snapshot(self) -> RepoStatusSnapshot:
        """Return the store-owned status subset for this refresh."""
        return RepoStatusSnapshot(
            branch=self.branch,
            head=self.head,
            upstream=self.upstream,
            ahead=self.ahead,
            behind=self.behind,
            dirty=self.dirty,
            message=self.message,
            error=self.error,
            merging=self.merging,
        )


@dataclass(frozen=True)
class ChildRefreshSnapshot:
    """Typed nested-row refresh result before projection fields are updated."""

    kind: str = ""
    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    message: str = ""
    error: str = ""
    merging: bool = False
    in_sync: bool = True

    def status_snapshot(self) -> ChildStatusSnapshot:
        """Return the store-owned status subset for this refresh."""
        return ChildStatusSnapshot(
            kind=self.kind,
            branch=self.branch,
            head=self.head,
            upstream=self.upstream,
            ahead=self.ahead,
            behind=self.behind,
            dirty=self.dirty,
            message=self.message,
            error=self.error,
            merging=self.merging,
            in_sync=self.in_sync,
        )


def read_repo_refresh_snapshot(repo: Repo, message: str = "") -> RepoRefreshSnapshot:
    """Query one repo and return a typed refresh snapshot.

    This intentionally mutates only a temporary projection object. The caller
    publishes ``status_snapshot()`` to the store, then applies the full snapshot
    to the live ``Repo`` only while projection fields still exist.
    """
    previous_workflow_state = {
        wf.path: wf.state for wf in repo.workflows if wf.state
    }
    previous_workflow_paths = {wf.path for wf in repo.workflows}
    previous_workflow_states_hydrated = repo.workflow_states_hydrated
    probe = Repo(rel=repo.rel, path=repo.path, message=message)
    probe.workflows = discover_workflows_local(repo.path)
    current_workflow_paths = {wf.path for wf in probe.workflows}
    for wf in probe.workflows:
        wf.state = previous_workflow_state.get(wf.path, "")
    probe.workflow_states_hydrated = (
        previous_workflow_states_hydrated
        and current_workflow_paths == previous_workflow_paths
    )

    rc, out, err = git(repo.path, [
        "status", "--porcelain=v2", "--branch", "-z",
    ])
    if rc != 0:
        probe.error = (err or "git status failed").strip().splitlines()[0]
        return _repo_refresh_snapshot_from_probe(probe)
    _parse_porcelain_v2_z(probe, out)

    rc, out, _ = git(repo.path, ["remote", "get-url", "origin"])
    if rc == 0 and out.strip():
        probe.remote_url_raw = out.strip()
        probe.remote_url = canonicalize_url(out.strip())

    rc, out, _ = git(repo.path, ["rev-parse", "--git-dir"])
    if rc == 0 and out.strip():
        git_dir = Path(out.strip())
        if not git_dir.is_absolute():
            git_dir = (repo.path / git_dir).resolve()
        if not probe.merging:
            for marker in MERGE_MARKER_FILES:
                if (git_dir / marker).exists():
                    probe.merging = True
                    break
        if not probe.merging:
            for marker in MERGE_MARKER_DIRS:
                if (git_dir / marker).is_dir():
                    probe.merging = True
                    break

    probe.nested_subs = _read_gitmodules_submodules(repo.path)

    if not probe.error and not probe.is_dirty and not probe.merging:
        probe.message = ""
    return _repo_refresh_snapshot_from_probe(probe)


def apply_repo_refresh_snapshot(repo: Repo, snapshot: RepoRefreshSnapshot) -> None:
    """Apply a refresh snapshot to the temporary repo projection object."""
    repo.branch = snapshot.branch
    repo.head = snapshot.head
    repo.upstream = snapshot.upstream
    repo.remote_url = snapshot.remote_url
    repo.remote_url_raw = snapshot.remote_url_raw
    repo.ahead = snapshot.ahead
    repo.behind = snapshot.behind
    repo.staged = list(snapshot.staged)
    repo.unstaged = list(snapshot.unstaged)
    repo.untracked = list(snapshot.untracked)
    repo.nested_subs = list(snapshot.nested_subs)
    repo.error = snapshot.error
    repo.merging = snapshot.merging
    repo.conflict_paths = list(snapshot.conflict_paths)
    repo.workflows = list(snapshot.workflows)
    repo.workflow_states_hydrated = snapshot.workflow_states_hydrated


def read_child_refresh_snapshot(
        ref: ChildRef,
        message: str = "",
) -> ChildRefreshSnapshot:
    """Query one nested checkout and return a typed child refresh snapshot."""
    head = ""
    in_sync = True
    rc, out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
    if rc == 0:
        head = out.strip()
        if ref.repo.head:
            in_sync = head == ref.repo.head
    else:
        in_sync = False

    rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
    branch = (out.strip() or "(detached)") if rc == 0 else ""

    rc, out, _ = git(ref.nested_path, ["status", "--porcelain=v1"])
    dirty = rc == 0 and bool(out.strip())

    rc, out, _ = git(ref.nested_path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream = out.strip() if rc == 0 and out.strip() else None
    ahead = 0
    behind = 0
    if upstream:
        rc, out, _ = git(ref.nested_path, [
            "rev-list", "--count", "--left-right",
            f"{upstream}...HEAD",
        ])
        if rc == 0:
            parts = out.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    merging = False
    rc, out, _ = git(ref.nested_path, ["rev-parse", "--git-dir"])
    if rc == 0 and out.strip():
        git_dir = Path(out.strip())
        if not git_dir.is_absolute():
            git_dir = (ref.nested_path / git_dir).resolve()
        for marker in MERGE_MARKER_FILES:
            if (git_dir / marker).exists():
                merging = True
                break
        if not merging:
            for marker in MERGE_MARKER_DIRS:
                if (git_dir / marker).is_dir():
                    merging = True
                    break

    return ChildRefreshSnapshot(
        kind=ref.kind,
        branch=branch,
        head=head,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        message=message if dirty or merging else "",
        error="",
        merging=merging,
        in_sync=in_sync,
    )


def apply_child_refresh_snapshot(
        ref: ChildRef,
        snapshot: ChildRefreshSnapshot,
) -> None:
    """Apply a refresh snapshot to the temporary child projection object."""
    ref.kind = snapshot.kind
    ref.branch = snapshot.branch
    ref.head = snapshot.head
    ref.upstream = snapshot.upstream
    ref.ahead = snapshot.ahead
    ref.behind = snapshot.behind
    ref.dirty = snapshot.dirty
    ref.error = snapshot.error
    ref.merging = snapshot.merging
    ref.in_sync = snapshot.in_sync


def _repo_refresh_snapshot_from_probe(repo: Repo) -> RepoRefreshSnapshot:
    return RepoRefreshSnapshot(
        branch=repo.branch,
        head=repo.head,
        upstream=repo.upstream,
        remote_url=repo.remote_url,
        remote_url_raw=repo.remote_url_raw,
        ahead=repo.ahead,
        behind=repo.behind,
        staged=list(repo.staged),
        unstaged=list(repo.unstaged),
        untracked=list(repo.untracked),
        nested_subs=list(repo.nested_subs),
        error=repo.error,
        merging=repo.merging,
        conflict_paths=list(repo.conflict_paths),
        workflows=list(repo.workflows),
        workflow_states_hydrated=repo.workflow_states_hydrated,
        message=repo.message,
    )


def refresh_repo(repo: Repo) -> None:
    """Re-query every cached field on a Repo from its working tree."""
    apply_repo_refresh_snapshot(
        repo,
        read_repo_refresh_snapshot(repo, message=repo.message),
    )


# ---------- Discovery + linkage --------------------------------------------


def discover_repos(workspace: Path) -> List[Repo]:
    """Return the workspace itself (if it's a git repo) plus every immediate
    child folder containing .git, sorted alphabetically. The folder this
    script lives in is included if (and only if) it's also a git repo —
    handy for managing idlegit's own checkout from idlegit itself.

    Hidden directories (dotfolders like `.github`, `.dotfiles`) are NOT
    excluded — GitHub treats `<owner>/.github` as a real repo (it hosts
    the org-/user-level README) and there's no reason to hide it from
    the workspace view. The membership criterion is "directory contains
    a `.git`", same as for non-hidden children. The workspace's own
    `.git` directory doesn't satisfy this (no nested `.git`), so it's
    safely skipped without needing a name filter."""
    repos: List[Repo] = []
    try:
        if (workspace / ".git").exists():
            repos.append(Repo(rel=".", path=workspace.resolve()))
    except OSError:
        return repos
    try:
        children = sorted(workspace.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return repos
    for child in children:
        try:
            if not child.is_dir():
                continue
            if (child / ".git").exists():
                repos.append(Repo(rel=child.name, path=child.resolve()))
        except OSError:
            continue
    return repos


# Serializes link_siblings calls so two supervisors finishing at the
# same time can't interleave. Without this, both calls would clear
# `r.children` and `r.siblings` at the start, then race-append the same
# ChildRefs into the same lists — producing duplicated submodule rows
# under the affected parents (the symptom: pushing on a canonical sub
# kicked off auto-sync, manual smart-sync ran on top of it, the row
# list now showed each child twice). Even with the atomic-swap pattern
# below, the lock is cheap insurance against two callers wasting the
# same `git` queries to compute the same answer.
_link_siblings_lock = threading.Lock()


@dataclass(frozen=True)
class LinkSiblingsSnapshot:
    """Store-ready sibling/child topology produced by relink."""

    repos: Tuple[Repo, ...]
    children_by_parent: Dict[int, Tuple[ChildRef, ...]]
    siblings_by_repo: Dict[int, Tuple[Tuple[Repo, Path], ...]]
    synthetic_by_url: Dict[str, Repo]

    def children_for(self, parent: Repo) -> List[ChildRef]:
        """Return the child rows for one parent repo."""
        return list(self.children_by_parent.get(id(parent), ()))

    def siblings_for(self, repo: Repo) -> List[Tuple[Repo, Path]]:
        """Return the sibling rows for one repo."""
        return list(self.siblings_by_repo.get(id(repo), ()))

    def parent_child_pairs(self) -> List[Tuple[Repo, ChildRef]]:
        """Return every explicit parent/child topology edge."""
        pairs: List[Tuple[Repo, ChildRef]] = []
        for parent in self.repos:
            for child in self.children_by_parent.get(id(parent), ()):
                pairs.append((parent, child))
        return pairs


def link_siblings(
        repos: List[Repo],
        subtrees: Optional[List[SubtreeSpec]] = None,
        *,
        busy_child_predicate: Optional[Callable[[ChildRef], bool]] = None,
        child_message_lookup: Optional[Callable[[ChildRef], str]] = None,
) -> None:
    """For each tracked repo X, find every other tracked repo Y that
    contains X — either as a nested submodule (auto-detected from
    .gitmodules) or as a subtree (declared in idlegit.conf). Records:
        - X.siblings — list of (Y, nested_path) for submodule sync-after-push
        - Y.children — ChildRef entries (kind="submodule"/"subtree") for the
          indented rows below Y on the main screen
    The workspace root is skipped for submodule auto-discovery (its
    submodules are already top-level rows); subtrees are honored regardless.

    Concurrency: link structure is snapshotted and finally swapped under
    `_link_siblings_lock`, while the expensive child git probes run
    outside it. If refresh_repo changes the structure while probes are
    running, the draft is discarded and rebuilt against the newer
    structure."""
    snapshot = read_link_siblings_snapshot(
        repos,
        subtrees,
        busy_child_predicate=busy_child_predicate,
        child_message_lookup=child_message_lookup,
    )
    apply_link_siblings_snapshot(snapshot)


def read_link_siblings_snapshot(
        repos: List[Repo],
        subtrees: Optional[List[SubtreeSpec]] = None,
        *,
        busy_child_predicate: Optional[Callable[[ChildRef], bool]] = None,
        child_message_lookup: Optional[Callable[[ChildRef], str]] = None,
) -> LinkSiblingsSnapshot:
    """Build a complete relink topology without mutating repo projections."""
    for _ in range(5):
        with _link_siblings_lock:
            signature = _link_structure_signature(repos, subtrees)
            draft = _build_link_siblings_draft(
                repos,
                subtrees,
                busy_child_predicate=busy_child_predicate,
                child_message_lookup=child_message_lookup,
            )
        _populate_child_refs(draft[3])
        with _link_siblings_lock:
            if signature != _link_structure_signature(repos, subtrees):
                continue
            return _finalized_link_siblings_snapshot(
                repos,
                draft,
                busy_child_predicate=busy_child_predicate,
                child_message_lookup=child_message_lookup,
            )

    # Pathological churn fallback: keep the old fully-serialized behavior
    # for one pass rather than spinning forever.
    with _link_siblings_lock:
        draft = _build_link_siblings_draft(
            repos,
            subtrees,
            busy_child_predicate=busy_child_predicate,
            child_message_lookup=child_message_lookup,
        )
        _populate_child_refs(draft[3])
        return _finalized_link_siblings_snapshot(
            repos,
            draft,
            busy_child_predicate=busy_child_predicate,
            child_message_lookup=child_message_lookup,
        )


def apply_link_siblings_snapshot(snapshot: LinkSiblingsSnapshot) -> None:
    """Apply a relink snapshot to temporary row projection fields."""
    for repo in snapshot.repos:
        repo.children = snapshot.children_for(repo)
        repo.siblings = snapshot.siblings_for(repo)
    for synthetic in snapshot.synthetic_by_url.values():
        synthetic.siblings = snapshot.siblings_for(synthetic)


def _link_structure_signature(
        repos: List[Repo],
        subtrees: Optional[List[SubtreeSpec]],
) -> Tuple[Tuple[object, ...], Tuple[Tuple[str, str, str, str], ...]]:
    repo_sig = tuple(
        (
            id(r),
            r.remote_url,
            r.rel,
            tuple((url, str(path.resolve())) for url, path in r.nested_subs),
        )
        for r in repos
    )
    subtree_sig = tuple(
        (spec.name, spec.parent, spec.source, spec.prefix)
        for spec in (subtrees or [])
    )
    return repo_sig, subtree_sig


def _build_link_siblings_draft(
        repos: List[Repo],
        subtrees: Optional[List[SubtreeSpec]],
        *,
        busy_child_predicate: Optional[Callable[[ChildRef], bool]] = None,
        child_message_lookup: Optional[Callable[[ChildRef], str]] = None,
) -> Tuple[
        Dict[int, List[ChildRef]],
        Dict[int, List[Tuple[Repo, Path]]],
        Dict[str, Repo],
        List[ChildRef],
]:
    url_to_repo = {r.remote_url: r for r in repos if r.remote_url}
    rel_to_repo = {r.rel: r for r in repos}

    # Build into local dicts; assign onto each Repo only at the end so
    # that another thread reading r.children mid-execution (during a
    # render, say) sees either the old snapshot or the new one — never
    # a partially-cleared list. Keyed by `id(repo)` because the Repo
    # dataclass has value-based `__eq__` and is therefore unhashable.
    new_children: Dict[int, List[ChildRef]] = {id(r): [] for r in repos}
    new_siblings: Dict[int, List[Tuple[Repo, Path]]] = {id(r): [] for r in repos}

    # Synthetic canonicals for submodule URLs that don't match any
    # tracked top-level repo (e.g. a submodule used by parents only,
    # never cloned standalone). Multiple parents pointing at the same
    # URL share one synthetic Repo so the user still sees both rows + a
    # working action menu, and we have a place to record drift between
    # them. Synthetic Repos are NOT added to `repos` / state.repos.
    synthetic_by_url: Dict[str, Repo] = {}

    def _make_synthetic(url: str, sub_path: Path) -> Repo:
        # Prefer the on-disk basename for the display name (preserves
        # original casing, e.g. "Upskill.Health.Unity"); fall back to a
        # URL-slug parse if for some reason the path is empty.
        name = sub_path.name
        if not name and url:
            m = re.search(r"[/:]([^/:]+?)(?:\.git)?$", url)
            if m:
                name = m.group(1)
        if not name:
            name = "submodule"
        synth = Repo(rel=name, path=sub_path, synthetic=True)
        # Synthetic Repos are created here, so they need their own
        # children/siblings buckets in the local dicts too — anything
        # treating them as a real Repo (sibling lookups, drift detect)
        # expects these to exist.
        new_children[id(synth)] = []
        new_siblings[id(synth)] = []
        return synth

    # Submodule references — discovered from each parent's .gitmodules.
    # Dedup at this stage too (paranoia + belt-and-braces): if a stale
    # `.gitmodules` somehow has the same submodule listed twice (same
    # URL + same path), only one ChildRef is produced.
    #
    # Two side-tables snapshotted from the pre-rebuild children:
    #   prev_submodule_msg — per-child commit message buffer; carried
    #     forward so the review screen doesn't lose what the user typed.
    #   busy_old_ref — the actual OLD ChildRef instance for any submodule
    #     the state-aware caller reports as row-busy. Rebuilding would mint
    #     a fresh ChildRef with no ownership state and a brand-new lock,
    #     which (a) drops the row's spinner mid-push and (b) splits the lock
    #     identity so the worker holding the OLD lock can't interlock with
    #     anything that consults the NEW one.
    prev_submodule_msg: Dict[Tuple[int, str], str] = {}
    busy_old_ref: Dict[Tuple[int, str], ChildRef] = {}
    for parent in repos:
        for old in parent.children:
            if old.kind != "submodule":
                continue
            nk = (id(parent), str(old.nested_path.resolve()))
            prev_submodule_msg[nk] = _child_message(
                old, child_message_lookup)
            if busy_child_predicate is not None and busy_child_predicate(old):
                busy_old_ref[nk] = old

    submodule_refs: List[ChildRef] = []
    for parent in repos:
        if parent.rel == ".":
            continue
        seen_for_parent: set = set()
        for url, sub_path in parent.nested_subs:
            key = (url, sub_path)
            if key in seen_for_parent:
                continue
            seen_for_parent.add(key)
            target = url_to_repo.get(url)
            if target is None:
                if url:
                    target = synthetic_by_url.get(url)
                    if target is None:
                        target = _make_synthetic(url, sub_path)
                        synthetic_by_url[url] = target
                else:
                    target = _make_synthetic("", sub_path)
            if target is parent:
                continue
            new_siblings[id(target)].append((parent, sub_path))
            nk = (id(parent), str(sub_path.resolve()))
            busy = busy_old_ref.get(nk)
            if busy is not None:
                # Reuse the in-flight ref so the lock + refreshing flag
                # persist across the rebuild. Skip `_populate` for this
                # ref — the worker that holds its lock owns mutations
                # to head/branch/dirty/etc. while it runs; a parallel
                # read from `_populate` would race the worker's
                # subprocess output. The next link_siblings call after
                # the worker releases will re-populate fresh.
                new_children[id(parent)].append(busy)
                continue
            prev_msg = prev_submodule_msg.get(nk, "")
            ref = ChildRef(
                repo=target, nested_path=sub_path, kind="submodule",
                message=prev_msg)
            new_children[id(parent)].append(ref)
            submodule_refs.append(ref)

    # Subtree references — declared in idlegit.conf.
    for spec in subtrees or []:
        parent = rel_to_repo.get(spec.parent)
        source = rel_to_repo.get(spec.source)
        if parent is None or source is None or parent is source:
            continue
        nested_path = (parent.path / spec.prefix).resolve()
        ref = ChildRef(repo=source, nested_path=nested_path, kind="subtree")
        # No cheap drift signal for subtrees; leave in_sync at default True.
        new_children[id(parent)].append(ref)

    return new_children, new_siblings, synthetic_by_url, submodule_refs


def _populate_child_ref(ref: ChildRef) -> None:
    apply_child_refresh_snapshot(
        ref,
        read_child_refresh_snapshot(ref, message=ref.message),
    )


def _populate_child_refs(submodule_refs: List[ChildRef]) -> None:
    if not submodule_refs:
        return
    max_workers = min(len(submodule_refs), MAX_PARALLEL_GIT_JOBS)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_populate_child_ref, submodule_refs))


def _child_message(
        child: ChildRef,
        child_message_lookup: Optional[Callable[[ChildRef], str]],
) -> str:
    if child_message_lookup is None:
        return child.message
    return child_message_lookup(child)


def _finalized_link_siblings_snapshot(
        repos: List[Repo],
        draft: Tuple[
            Dict[int, List[ChildRef]],
            Dict[int, List[Tuple[Repo, Path]]],
            Dict[str, Repo],
            List[ChildRef],
        ],
        *,
        busy_child_predicate: Optional[Callable[[ChildRef], bool]] = None,
        child_message_lookup: Optional[Callable[[ChildRef], str]] = None,
) -> LinkSiblingsSnapshot:
    new_children, new_siblings, synthetic_by_url, _ = draft

    prev_submodule_msg: Dict[Tuple[int, str], str] = {}
    busy_old_ref: Dict[Tuple[int, str], ChildRef] = {}
    for parent in repos:
        for old in parent.children:
            if old.kind != "submodule":
                continue
            nk = (id(parent), str(old.nested_path.resolve()))
            prev_submodule_msg[nk] = _child_message(
                old, child_message_lookup)
            if busy_child_predicate is not None and busy_child_predicate(old):
                busy_old_ref[nk] = old

    for parent in repos:
        reconciled: List[ChildRef] = []
        for ref in new_children[id(parent)]:
            if ref.kind != "submodule":
                reconciled.append(ref)
                continue
            nk = (id(parent), str(ref.nested_path.resolve()))
            busy = busy_old_ref.get(nk)
            if busy is not None:
                reconciled.append(busy)
                continue
            if ref.dirty or ref.merging:
                ref.message = prev_submodule_msg.get(nk, ref.message)
            else:
                ref.message = ""
            reconciled.append(ref)
        new_children[id(parent)] = reconciled

    # Drift detection for shared synthetic canonicals: when multiple
    # parents reference the same untracked submodule URL, treat the
    # most-common HEAD across their checkouts as the "canonical" and
    # flag laggards as out-of-sync (so they show a behind-coloured dot).
    # Reads from the LOCAL `new_children` so it sees the in-progress
    # state, not whatever was on `parent.children` from a previous call.
    for synthetic in synthetic_by_url.values():
        sib_refs = [ref for parent in repos
                    for ref in new_children[id(parent)]
                    if ref.repo is synthetic]
        if len(sib_refs) < 2:
            continue
        head_counts: Dict[str, int] = {}
        for r in sib_refs:
            if r.head:
                head_counts[r.head] = head_counts.get(r.head, 0) + 1
        if not head_counts:
            continue
        chosen = max(head_counts, key=head_counts.get)
        synthetic.head = chosen
        for r in sib_refs:
            r.in_sync = bool(r.head) and r.head == chosen

    for child_list in new_children.values():
        child_list.sort(
            key=lambda c: (c.kind, c.repo.display_name.lower()))

    return LinkSiblingsSnapshot(
        repos=tuple(repos),
        children_by_parent={
            parent_id: tuple(children)
            for parent_id, children in new_children.items()
        },
        siblings_by_repo={
            repo_identity: tuple(siblings)
            for repo_identity, siblings in new_siblings.items()
        },
        synthetic_by_url=dict(synthetic_by_url),
    )


# ---------- Sync helpers ---------------------------------------------------


def working_tree_signature(repo_path: Path) -> Tuple[Tuple[str, str], ...]:
    """Stable signature of every working-tree file that differs from HEAD
    or is untracked-not-ignored. Each entry is `(path, blob_hash)`; blob
    hash is the empty string for deletions. The signature is sorted so
    two checkouts with identical pending changes produce equal tuples,
    which lets the smart sync detect duplicate working trees that agents
    have produced across nested checkouts of the same submodule."""
    seen: dict = {}

    rc, out, _ = git(repo_path, ["diff", "HEAD", "--name-only", "-z"])
    if rc != 0:
        # Likely a fresh repo with no HEAD yet; fall through to untracked
        # so we still notice spurious files. An empty signature here just
        # excludes this checkout from dedup, which is the safe default.
        out = ""
    for p in out.split("\x00"):
        if not p or p in seen:
            continue
        full = repo_path / p
        if full.is_file():
            rc2, h, _ = git(repo_path, ["hash-object", "--", str(full)])
            seen[p] = h.strip() if rc2 == 0 else ""
        else:
            seen[p] = ""  # deletion (or directory replaced by symlink, etc.)

    rc, out, _ = git(repo_path, [
        "ls-files", "--others", "--exclude-standard", "-z",
    ])
    if rc == 0:
        for p in out.split("\x00"):
            if not p or p in seen:
                continue
            full = repo_path / p
            if full.is_file():
                rc2, h, _ = git(repo_path, ["hash-object", "--", str(full)])
                seen[p] = h.strip() if rc2 == 0 else ""

    return tuple(sorted(seen.items()))


def signature_mtime(repo_path: Path,
                    signature: Tuple[Tuple[str, str], ...]) -> float:
    """Latest mtime among files in the signature, used to pick the
    'most-recently-touched' winner among duplicate working trees. Missing
    or stat-failing files contribute nothing (don't drag the result down)."""
    latest = 0.0
    for p, _ in signature:
        try:
            mtime = (repo_path / p).stat().st_mtime
        except OSError:
            continue
        if mtime > latest:
            latest = mtime
    return latest


def _sync_submodule_origin_from_parent(
        parent_path: Path,
        sibling_path: Path) -> Tuple[bool, str]:
    sibling_resolved = sibling_path.resolve()
    expected_url = ""
    for url, sub_path in _read_gitmodules_submodules(parent_path):
        if sub_path == sibling_resolved:
            expected_url = url
            break
    if not expected_url:
        return True, ""

    rc, out, err = git(sibling_path, ["remote", "get-url", "origin"])
    if rc != 0:
        return False, f"origin lookup failed: {first_line(err)}"
    if canonicalize_url(out.strip()) == expected_url:
        return True, ""

    try:
        rel_path = sibling_resolved.relative_to(parent_path.resolve()).as_posix()
    except ValueError:
        return False, "submodule path is outside parent"
    rc, _, err = git(parent_path, ["submodule", "sync", "--", rel_path])
    if rc != 0:
        return False, f"submodule sync failed: {first_line(err)}"
    rc, out, err = git(sibling_path, ["remote", "get-url", "origin"])
    if rc != 0:
        return False, f"origin lookup failed after sync: {first_line(err)}"
    if canonicalize_url(out.strip()) != expected_url:
        return False, "submodule origin still differs from .gitmodules"
    return True, "origin synced; "


def sync_sibling(
        sibling_path: Path,
        branch: str,
        parent_path: Optional[Path] = None) -> Tuple[bool, str]:
    """Fetch + checkout origin/<branch> in a sibling's nested submodule
    checkout so it lines up with what we just pushed.

    SAFETY (cardinal rule): refuses if the sibling's HEAD has commits
    that aren't already on origin/<branch>. Plain `git checkout
    origin/<branch>` from a detached HEAD with unique commits prints a
    stderr warning + returns 0, which means the unique commits get
    orphaned and any files unique to those commits vanish from the
    working tree. The user resolves manually (e.g. by branching from
    HEAD before re-running the sync)."""
    if not is_safe_ref_arg(branch):
        return False, f"unsafe branch name: {branch or '(empty)'}"
    prefix = ""
    if parent_path is not None:
        ok, prefix = _sync_submodule_origin_from_parent(
            parent_path, sibling_path)
        if not ok:
            return False, prefix
    rc, _, err = git(sibling_path, ["fetch", "origin"])
    if rc != 0:
        return False, f"fetch failed: {first_line(err)}"
    target_ref = f"origin/{branch}"
    rc, _, _ = git(sibling_path, [
        "merge-base", "--is-ancestor", "HEAD", target_ref,
    ])
    if rc != 0:
        return False, (f"HEAD has commits not on {target_ref} "
                       "— would orphan them; manual: `git checkout -b "
                       "<name>` to keep them, then re-run sync")
    rc, _, err = git(sibling_path, ["checkout", target_ref])
    if rc != 0:
        return False, f"checkout failed: {first_line(err)}"
    return True, f"{prefix}synced"


# ---------- Safe staging (replacement for `git add -A`) -------------------


def list_registered_submodule_paths(repo_path: Path) -> "set[str]":
    """Set of paths that are registered submodules in `repo_path`'s
    `.gitmodules`. Empty when there's no `.gitmodules` or the file can't
    be parsed. Used by `safe_stage_all` to refuse staging changes that
    would destroy a submodule pointer."""
    if not (repo_path / ".gitmodules").exists():
        return set()
    rc, out, _ = git(repo_path, [
        "config", "-f", ".gitmodules",
        "--get-regexp", r"submodule\..+\.path",
    ])
    if rc != 0:
        return set()
    paths: set = set()
    for line in out.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            paths.add(parts[1].strip())
    return paths


def submodule_pointer_change_paths(repo_path: Path) -> List[str]:
    """Return registered submodule paths that are pure gitlink changes.

    Empty means either the tree is clean or propagation is unsafe because
    unrelated dirt / non-modification submodule state is present."""
    submodule_paths = list_registered_submodule_paths(repo_path)
    if not submodule_paths:
        return []

    # A submodule checkout with local dirt and a submodule checkout whose
    # HEAD moved can both appear as ` M path` in porcelain status. Parent
    # propagation must only touch the parent when nested checkouts are
    # clean, otherwise a visibly-dirty parent can slip through to commit
    # and push.
    rc, out, _ = git(repo_path, [
        "submodule", "foreach", "--quiet",
        "git status --porcelain=v1"])
    if rc != 0 or out.strip():
        return []

    rc, out, _ = git(repo_path, [
        "status", "--porcelain=v1", "-z", "--ignore-submodules=dirty"])
    if rc != 0:
        return []
    parts = out.split("\x00")
    changed_paths: List[str] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path_str = entry[3:]
        # `-z` emits the rename source as a separate NUL-chunk after
        # the status row — skip it so we don't reparse it as a status.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1
        if xy == "??":
            # Any untracked path means there's non-submodule dirt to
            # worry about; bail without claiming propagation.
            return []
        if path_str not in submodule_paths:
            return []
        # The submodule path must be a pure modification ("M" only —
        # not addition / deletion / typechange / conflict / etc.).
        # Anything else should be human-resolved; auto-committing
        # could record a brand-new gitlink, unregister the submodule,
        # or land mid-merge state.
        x, y = xy[0], xy[1]
        if x not in (" ", "M") or y not in (" ", "M"):
            return []
        if x == " " and y == " ":
            return []
        changed_paths.append(path_str)

    if not changed_paths:
        return []

    raw_changed: List[str] = []
    for diff_args in (["diff", "--raw", "-z"],
                      ["diff", "--cached", "--raw", "-z"]):
        rc, raw, _ = git(repo_path, diff_args)
        if rc != 0:
            return []
        raw_parts = raw.split("\x00")
        i = 0
        while i < len(raw_parts):
            meta = raw_parts[i]
            i += 1
            if not meta:
                continue
            if i >= len(raw_parts):
                return []
            path_str = raw_parts[i]
            i += 1
            fields = meta.split()
            if len(fields) < 5:
                return []
            old_mode = fields[0][1:]
            new_mode = fields[1]
            status = fields[4]
            if status.startswith(("R", "C")):
                i += 1
                return []
            if (old_mode != "160000" or new_mode != "160000"
                    or status != "M" or path_str not in submodule_paths):
                return []
            raw_changed.append(path_str)

    if sorted(set(raw_changed)) != sorted(set(changed_paths)):
        return []
    return changed_paths


def has_only_submodule_pointer_changes(repo_path: Path) -> bool:
    """True iff every change in `repo_path`'s working tree is a
    modification to a registered submodule's gitlink. False when the
    tree is clean (nothing to propagate), when any non-submodule path is
    dirty, or when any submodule path shows up as a deletion / addition
    / untracked entry (those need human attention — staging them would
    rewrite or destroy the gitlink).

    Smart-sync uses this as a precondition before auto-bumping a
    parent's submodule pointer after the canonical sync: if the parent
    has unrelated work in progress, propagation backs off and leaves
    the user to commit on their own."""
    return bool(submodule_pointer_change_paths(repo_path))


def safe_stage_all(repo_path: Path) -> Tuple[bool, str]:
    """Stage every change in `repo_path` the way `git add -A` would,
    except REFUSE outright when doing so would commit one of these two
    classes of damage (the cardinal-rule failure modes that have already
    cost this user real files):

      1. **Submodule-pointer deletion.** A `D` entry on a path
         registered in `.gitmodules` — happens when the submodule's
         working directory is empty (e.g. after `git submodule deinit`)
         and `git add -A` then stages the gitlink's removal as a real
         deletion in the parent. Committing this destroys the link.

      2. **Stray gitlink.** An `??` or `A` entry on a directory that
         contains a `.git` AND is not registered in `.gitmodules`.
         Some other tool may have placed a nested checkout at an
         unintended path (e.g. via a buggy script that doubled a
         relative+absolute prefix); `git add -A` would happily commit
         a gitlink at that bogus location.

    On detection, returns (False, msg) and stages NOTHING — the user
    investigates manually (re-init the submodule, remove the stray
    `.git`, etc.) and re-runs. Only when no risky entries are present
    does the actual `git add -A` run.

    Returns (ok, error_msg). Empty msg on success."""
    submodule_paths = list_registered_submodule_paths(repo_path)

    rc, out, err = git(repo_path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return False, first_line(err) or "git status failed"

    refused: List[str] = []
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path_str = entry[3:]
        # Renames/copies emit an extra NUL-separated old-name chunk
        # under `-z`; skip it so we don't reparse it as a status row.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1

        x, y = xy[0], xy[1]

        # 1. Refuse to stage submodule-pointer deletions.
        if (x == "D" or y == "D") and path_str in submodule_paths:
            refused.append(
                f"{path_str} (submodule pointer deletion — re-init the "
                "submodule or `git rm` it explicitly)")
            continue

        # 2. Refuse to stage stray gitlinks at unregistered paths.
        # Two flavours to catch: the entry itself IS a gitlink path,
        # OR the entry is an untracked DIRECTORY that contains an
        # embedded `.git` somewhere below. The second case is the
        # path-doubling failure mode — porcelain only reports the
        # top-level untracked dir, not the deeper embedded repo.
        if xy == "??" or x == "A" or y == "A":
            full = repo_path / path_str.rstrip("/")
            if (full / ".git").exists():
                normalized = path_str.rstrip("/")
                if normalized not in submodule_paths:
                    refused.append(
                        f"{normalized} (stray gitlink at unregistered "
                        "path — remove the nested .git or register it "
                        "in .gitmodules)")
                    continue
            elif xy == "??" and path_str.endswith("/"):
                for embedded in _find_embedded_gitlinks(full, repo_path):
                    if embedded not in submodule_paths:
                        refused.append(
                            f"{embedded} (stray gitlink at unregistered "
                            "path — remove the nested .git or register "
                            "it in .gitmodules)")
                if any(e for e in refused if e.startswith(path_str.rstrip("/"))):
                    continue

    if refused:
        return False, "refusing to stage: " + "; ".join(refused)

    rc, _, err = git(repo_path, ["add", "-A"])
    if rc != 0:
        return False, first_line(err) or "git add failed"
    return True, ""


def sync_subtree(parent_path: Path, prefix: str,
                 source_url: str, source_branch: str) -> Tuple[bool, str]:
    """Run `git subtree pull --squash` in the parent so the subtree's
    nested files catch up with the source repo's branch tip. NOTE: this
    creates a (squashed) merge commit in the parent — subtrees inherently
    can't be synced without one."""
    if not prefix:
        return False, "subtree prefix is empty"
    if not source_url:
        return False, "source repo has no remote URL"
    if source_url.startswith("-"):
        return False, "source repo remote URL looks like an option"
    if not is_safe_ref_arg(source_branch):
        return False, f"unsafe source branch: {source_branch or '(empty)'}"
    rc, status_out, _ = git(parent_path, ["status", "--porcelain=v1"])
    if rc != 0:
        return False, "parent status failed"
    if status_out.strip():
        return False, "parent has local changes"
    rc, _, err = git(parent_path, [
        "subtree", "pull", "--prefix=" + prefix,
        source_url, source_branch, "--squash",
    ])
    if rc != 0:
        return False, f"subtree pull failed: {first_line(err)}"
    return True, "subtree pulled"


# ---------- GitHub Actions integration (`gh` CLI) -------------------------
#
# Everything in this section is best-effort: if `gh` isn't on PATH or the
# repo isn't on github.com, the helpers return empty / a clean error and
# the rest of idlegit carries on as if the feature didn't exist.

import fnmatch  # noqa: E402
import json  # noqa: E402 — section-local convenience
import shutil  # noqa: E402


_GH_PATH: Optional[str] = shutil.which("gh")
_GH_SLUG_RE = re.compile(
    r"^(?:git@github\.com:|https?://(?:[^@/]+@)?github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?$"
)


def gh_available() -> bool:
    """True if the `gh` CLI is on PATH at idlegit startup. Cached at
    module-load time — no subprocess fork on every call."""
    return _GH_PATH is not None


def parse_github_slug(remote_url: Optional[str]) -> Optional[str]:
    """Extract `<owner>/<repo>` from a github.com remote URL (SSH or HTTPS,
    optionally with a user:token@ prefix and / or a .git suffix). Returns
    None for any non-github.com URL — the workflow features all gate on a
    non-None slug, so this is the single chokepoint."""
    if not remote_url:
        return None
    m = _GH_SLUG_RE.match(remote_url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def gh(args: List[str],
       timeout: float = DEFAULT_GH_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    """Run `gh` with the given args. Mirrors `git()` — never raises; a
    missing CLI or OSError is reported as rc=1."""
    if _GH_PATH is None:
        return 1, "", "gh CLI not on PATH"
    try:
        p = subprocess.run(
            [_GH_PATH, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except OSError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"gh timed out after {timeout:g}s"
    return p.returncode, p.stdout, p.stderr


def _parse_on_block(text: str) -> dict:
    """Best-effort parse of a workflow file's `on:` block. Returns a dict
    with: 'push' (bool), 'workflow_dispatch' (bool), 'push_branches'
    (List[str]), 'push_branches_ignore' (List[str]), 'push_tags'
    (List[str]), 'push_tags_ignore' (List[str]).

    Handles the three common YAML forms:
        on: push                                # scalar
        on: [push, pull_request]                # flow seq
        on:                                     # mapping
          push:
            branches: [a, b]
        on:                                     # mapping w/ block list
          push:
            tags:
            - 'v*.*.*'
    Tag filters matter because `on: push: tags: [...]` with no
    `branches:` means the workflow only fires on tag push — branch
    pushes don't trigger it. Anything more exotic (anchors,
    multi-document files, deeply mixed flow/block) falls through to
    "trigger detected" without branch / tag info, which keeps the
    predicate permissive rather than wrongly excluding."""
    result: dict = {
        "push": False,
        "workflow_dispatch": False,
        "push_branches": [],
        "push_branches_ignore": [],
        "push_tags": [],
        "push_tags_ignore": [],
    }
    lines = text.splitlines()

    on_idx: Optional[int] = None
    for i, raw in enumerate(lines):
        if raw.startswith(("on:", "on ")) and not raw.startswith((" ", "\t")):
            on_idx = i
            break
    if on_idx is None:
        return result

    on_line = lines[on_idx]
    suffix = on_line[on_line.index(":") + 1:].split("#", 1)[0].strip()

    # Form 1: `on: push` (or any single scalar trigger).
    if suffix and not suffix.startswith("[") and not suffix.startswith("{"):
        token = suffix.strip().strip('"\'')
        if token == "push":
            result["push"] = True
        elif token == "workflow_dispatch":
            result["workflow_dispatch"] = True
        return result

    # Form 2: `on: [push, ...]` flow seq.
    if suffix.startswith("["):
        inner = suffix.split("]", 1)[0][1:]
        tokens = [t.strip().strip('"\'') for t in inner.split(",") if t.strip()]
        result["push"] = "push" in tokens
        result["workflow_dispatch"] = "workflow_dispatch" in tokens
        return result

    # Form 3: nested mapping. Walk indented children until we hit a
    # column-0 line again (next top-level key or blank past EOF).
    children: List[Tuple[int, str]] = []
    j = on_idx + 1
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            j += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:  # next top-level key
            break
        children.append((indent, line))
        j += 1
    if not children:
        return result

    base_indent = min(ind for ind, _ in children)
    i = 0
    while i < len(children):
        indent, line = children[i]
        if indent != base_indent or ":" not in line:
            i += 1
            continue
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        rest = rest.split("#", 1)[0].strip()

        if key == "workflow_dispatch":
            result["workflow_dispatch"] = True
            i += 1
            continue

        if key != "push":
            i += 1
            continue

        result["push"] = True
        # Walk push:'s sub-block for `branches:` / `branches-ignore:`
        # / `tags:` / `tags-ignore:`. Tags share the same shape as
        # branches — same inline-flow / inline-single / block-list
        # parsing applies, just into a different result bucket.
        sub = i + 1
        while sub < len(children) and children[sub][0] > indent:
            sub_indent, sub_line = children[sub]
            sub_stripped = sub_line.strip()
            if ":" not in sub_stripped:
                sub += 1
                continue
            sub_key, _, sub_rest = sub_stripped.partition(":")
            sub_key = sub_key.strip()
            sub_rest = sub_rest.split("#", 1)[0].strip()
            target_field: Optional[str] = None
            if sub_key == "branches":
                target_field = "push_branches"
            elif sub_key == "branches-ignore":
                target_field = "push_branches_ignore"
            elif sub_key == "tags":
                target_field = "push_tags"
            elif sub_key == "tags-ignore":
                target_field = "push_tags_ignore"
            if target_field is None:
                sub += 1
                continue
            # Inline form: branches: [a, b]
            if sub_rest.startswith("["):
                inner = sub_rest.split("]", 1)[0][1:]
                for t in inner.split(","):
                    val = t.strip().strip('"\'')
                    if val:
                        result[target_field].append(val)
                sub += 1
                continue
            # Single value inline: branches: master
            if sub_rest:
                result[target_field].append(sub_rest.strip('"\''))
                sub += 1
                continue
            # Block list form: items can sit at the SAME indent as the
            # parent key in YAML's compact style — e.g.:
            #     branches:
            #     - master
            # so we walk forward, accepting `- value` lines at indent
            # >= sub_indent, and bailing out as soon as we hit a non-list
            # line (that's a sibling key under push:).
            block = sub + 1
            while block < len(children):
                b_indent, b_line = children[block]
                if b_indent < sub_indent:
                    break
                b_stripped = b_line.strip()
                if not b_stripped.startswith("-"):
                    break
                val = b_stripped[1:].strip().strip('"\'')
                if val:
                    result[target_field].append(val)
                block += 1
            sub = block
        i = sub

    return result


def _parse_workflow_dispatch_inputs(text: str) -> "List[WorkflowInput]":
    """Best-effort scan for `on.workflow_dispatch.inputs` entries —
    returns each input as a `WorkflowInput(name, description,
    default)` in declaration order. Hand-rolled rather than pulling
    in PyYAML so the project keeps its zero-dependency promise; the
    parser handles the common shape:

        on:
          workflow_dispatch:
            inputs:
              version:
                description: 'Version to deploy'
                default: 'v1.0'
                required: true
                type: string

    Sub-fields it doesn't know about (type / required / options) are
    silently ignored — value handling on the dispatch side is
    generic (string), and required-but-empty inputs surface as a gh
    error rather than a parse-time concern. Returns [] when the
    workflow isn't dispatchable, the YAML is exotic enough to
    confuse the line scanner, or the `inputs:` block is absent."""
    lines = text.splitlines()

    on_idx: Optional[int] = None
    for i, raw in enumerate(lines):
        if raw.startswith(("on:", "on ")) and not raw.startswith((" ", "\t")):
            on_idx = i
            break
    if on_idx is None:
        return []

    def _walk_children(start_idx: int, parent_indent: int
                       ) -> List[Tuple[int, int, str]]:
        """Yield (line_idx, indent, line) for indented children of
        the line at start_idx (which sits at `parent_indent`).
        Stops at the next line whose indent is <= parent_indent.
        Skips blanks and comment-only lines."""
        out: List[Tuple[int, int, str]] = []
        j = start_idx + 1
        while j < len(lines):
            line = lines[j]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                j += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= parent_indent:
                break
            out.append((j, indent, line))
            j += 1
        return out

    on_children = _walk_children(on_idx, 0)
    if not on_children:
        return []

    # Locate `workflow_dispatch:` directly under `on:`.
    base_indent = min(ind for _, ind, _ in on_children)
    wd_idx: Optional[int] = None
    wd_indent: int = 0
    for line_idx, indent, line in on_children:
        if indent != base_indent:
            continue
        stripped = line.strip()
        if stripped.startswith("workflow_dispatch:") or \
                stripped == "workflow_dispatch":
            wd_idx = line_idx
            wd_indent = indent
            break
    if wd_idx is None:
        return []

    # Then `inputs:` directly under `workflow_dispatch:`.
    wd_children = _walk_children(wd_idx, wd_indent)
    inputs_idx: Optional[int] = None
    inputs_indent: int = 0
    if wd_children:
        wd_base = min(ind for _, ind, _ in wd_children)
        for line_idx, indent, line in wd_children:
            if indent != wd_base:
                continue
            stripped = line.strip()
            if stripped.startswith("inputs:") or stripped == "inputs":
                inputs_idx = line_idx
                inputs_indent = indent
                break
    if inputs_idx is None:
        return []

    # Walk every input name (one per child at the inputs-base indent)
    # and pick up `description:` / `default:` from its sub-block.
    input_children = _walk_children(inputs_idx, inputs_indent)
    if not input_children:
        return []
    name_indent = min(ind for _, ind, _ in input_children)
    inputs: List[WorkflowInput] = []
    i = 0
    while i < len(input_children):
        line_idx, indent, line = input_children[i]
        if indent != name_indent:
            i += 1
            continue
        stripped = line.strip().split("#", 1)[0].strip()
        if not stripped.endswith(":"):
            # Skip exotic `name: <inline>` forms — inputs always
            # nest sub-fields under the name in real workflow YAML.
            i += 1
            continue
        name = stripped[:-1].strip().strip('"').strip("'")
        if not name:
            i += 1
            continue
        entry = WorkflowInput(name=name)
        # Sub-fields of this input — siblings deeper than name_indent
        # until the next sibling at name_indent.
        sub = i + 1
        while sub < len(input_children):
            sub_idx, sub_indent, sub_line = input_children[sub]
            if sub_indent <= name_indent:
                break
            sub_stripped = sub_line.strip()
            if ":" in sub_stripped:
                k, _, v = sub_stripped.partition(":")
                k = k.strip()
                v = v.split("#", 1)[0].strip()
                v = v.strip('"').strip("'")
                if k == "description":
                    entry.description = v
                elif k == "default":
                    entry.default = v
            sub += 1
        inputs.append(entry)
        i = sub if sub > i + 1 else i + 1
    return inputs


def discover_workflows_local(repo_path: Path) -> List[WorkflowInfo]:
    """Scan `<repo>/.github/workflows/*.{yml,yaml}` and return a
    WorkflowInfo per file. Workflow `name:` falls back to the filename.
    Trigger metadata is parsed via `_parse_on_block`, dispatch inputs
    via `_parse_workflow_dispatch_inputs`, so callers get everything
    they need to predict pushes and prompt for inputs without
    re-parsing the YAML themselves."""
    wf_dir = repo_path / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    found: List[WorkflowInfo] = []
    try:
        entries = sorted(wf_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for yml in entries:
        if not yml.is_file():
            continue
        if yml.suffix not in (".yml", ".yaml"):
            continue
        try:
            text = yml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        wf_name = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name:"):
                wf_name = stripped[len("name:"):].strip().strip('"').strip("'")
                if wf_name:
                    break
        if not wf_name:
            wf_name = yml.stem
        on_info = _parse_on_block(text)
        # Inputs are only meaningful when the workflow is
        # dispatchable — `_parse_workflow_dispatch_inputs` returns
        # [] either way, but skipping the call when not dispatchable
        # avoids walking the YAML for nothing.
        wf_inputs: List[WorkflowInput] = []
        if on_info["workflow_dispatch"]:
            wf_inputs = _parse_workflow_dispatch_inputs(text)
        found.append(WorkflowInfo(
            name=wf_name,
            path=str(yml.relative_to(repo_path)),
            state="",
            dispatchable=on_info["workflow_dispatch"],
            triggers_push=on_info["push"],
            push_branches=on_info["push_branches"],
            push_branches_ignore=on_info["push_branches_ignore"],
            push_tags=on_info["push_tags"],
            push_tags_ignore=on_info["push_tags_ignore"],
            inputs=wf_inputs,
        ))
    return found


def would_run_on_push(wf: WorkflowInfo, branch: str) -> bool:
    """Predict whether `wf` will fire on a push to `branch`. Considers:
      - whether the workflow declares `on: push` at all
      - GitHub workflow state (`disabled_*` short-circuits to False)
      - branches / branches-ignore glob patterns
      - tag-only filtering (`on: push: tags: […]` with no
        `branches:` only fires on tag pushes — branch pushes don't
        trigger it)
    `branch` matching uses fnmatch so the standard `feature/*` style
    patterns work. State is treated permissively when unknown (empty
    string = "we haven't queried gh yet"); only an explicit
    `disabled_*` marks the workflow as won't-run."""
    if not wf.triggers_push:
        return False
    if wf.state.startswith("disabled"):
        return False
    # Tag-only push filter: `tags:` is set but `branches:` isn't.
    # GitHub's semantics here are that a branch push never triggers
    # the workflow, regardless of branch name — so the predicate
    # short-circuits to False for the branch-push prediction.
    if (wf.push_tags or wf.push_tags_ignore) and not wf.push_branches:
        return False
    if wf.push_branches:
        if not any(fnmatch.fnmatchcase(branch, p) for p in wf.push_branches):
            return False
    for ignore in wf.push_branches_ignore:
        if fnmatch.fnmatchcase(branch, ignore):
            return False
    return True


def list_remote_workflow_states(slug: str) -> "dict[str, str]":
    """Return a mapping of workflow name → GitHub workflow state for
    every workflow in the repo, including disabled ones (`--all`).
    Returns {} on any failure so callers can treat it as "no remote
    info available" without special-casing."""
    rc, out, _ = gh([
        "workflow", "list",
        "--repo", slug,
        "--all",
        "--json", "name,state,path",
    ])
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {row.get("name", ""): row.get("state", "")
            for row in data if isinstance(row, dict)}


def merge_remote_workflow_states(workflows: List[WorkflowInfo],
                                 slug: str) -> None:
    """Mutate `workflows` in place so each WorkflowInfo's `state` field
    reflects the GitHub-side state. Does nothing when the gh call fails
    or returns no rows. Workflows we can't match by name keep whatever
    state they had (empty string by default)."""
    states = list_remote_workflow_states(slug)
    if not states:
        return
    for wf in workflows:
        remote = states.get(wf.name, "")
        if remote:
            wf.state = remote


def list_recent_runs(slug: str, branch: str, commit: str,
                     limit: int = 20) -> List[dict]:
    """`gh run list` filtered to a specific branch + commit. Used by the
    post-push tracker to find the runs that just got triggered. Returns
    [] if `gh` is unavailable, the slug is wrong, or the response can't
    be parsed."""
    rc, out, _ = gh([
        "run", "list",
        "--repo", slug,
        "--branch", branch,
        "--commit", commit,
        "--limit", str(limit),
        "--json", "databaseId,name,workflowName,status,conclusion,createdAt,headSha,url",
    ])
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def get_run_view(slug: str, run_id: int) -> Optional[dict]:
    """Detailed view of one run: status, conclusion, jobs (each with their
    own status + steps). Returns None on failure so the caller can skip
    polling rather than crashing."""
    rc, out, _ = gh([
        "run", "view", str(run_id),
        "--repo", slug,
        "--json", "status,conclusion,name,workflowName,jobs,url,headSha",
    ])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def dispatch_workflow(slug: str, workflow_name: str,
                      ref: str,
                      inputs: "Optional[dict]" = None
                      ) -> Tuple[bool, str]:
    """Trigger a workflow_dispatch on the given branch. Surfaces the
    gh error (single line) if the workflow isn't dispatchable, the
    ref is invalid, or the user lacks permission.

    `inputs` (when supplied) is a `{name: value}` dict — non-empty
    entries are forwarded as `-F <name>=<value>`. Required-but-empty
    inputs aren't pre-checked here; we let GitHub return the
    canonical error so the user sees the same message they'd get on
    the web UI."""
    if not workflow_name or workflow_name.startswith("-"):
        return False, "unsafe workflow name"
    if not is_safe_ref_arg(ref):
        return False, "unsafe workflow ref"
    args = [
        "workflow", "run", workflow_name,
        "--repo", slug,
        "--ref", ref,
    ]
    if inputs:
        for name, value in inputs.items():
            if not name or name.startswith("-"):
                # Defence-in-depth: gh's argv parsing would already
                # reject these, but skipping here keeps the error
                # surface tight.
                continue
            if value == "":
                continue
            args.extend(["-F", f"{name}={value}"])
    rc, _, err = gh(args)
    if rc != 0:
        return False, first_line(err)
    return True, "dispatched"


def cancel_run(slug: str, run_id: int) -> Tuple[bool, str]:
    """`gh run cancel <run_id> --repo <slug>` — request that GitHub
    cancel an in-flight workflow run. Returns (ok, message). The
    existing `_poll_run` poller will pick up the resulting "cancelled"
    conclusion on its next iteration and flip the parent task to
    `warn`, so the caller doesn't need to do anything else."""
    rc, _, err = gh([
        "run", "cancel", str(run_id), "--repo", slug,
    ])
    if rc != 0:
        return False, first_line(err)
    return True, "cancellation requested"


# Cap on `gh run view --log` output so a 50MB log doesn't blow up the
# in-memory line list. The diff-viewer uses the same shape — see
# `_MAX_DIFF_LINES` over in `ui/modals/diff_viewer.py`.
_MAX_RUN_LOG_LINES = 50_000


def fetch_run_log(slug: str, run_id: int,
                  job_id: Optional[int] = None,
                  only_failed: bool = False
                  ) -> Tuple[bool, List[str], str]:
    """Fetch a workflow-run log via `gh run view`. Returns
    `(ok, lines, error)`. On rc != 0 the lines list is empty and
    `error` carries the first line of gh's stderr. Caps at
    `_MAX_RUN_LOG_LINES` to keep the viewer's list bounded — when the
    cap trips, an explicit truncation notice is appended as the last
    line so the user sees why they hit the end."""
    args = ["run", "view", str(run_id), "--repo", slug]
    if job_id is not None:
        args += ["--job", str(job_id)]
    args.append("--log-failed" if only_failed else "--log")
    rc, out, err = gh(args, timeout=60.0)
    if rc != 0:
        return False, [], first_line(err)
    lines = out.splitlines() if out else []
    if len(lines) > _MAX_RUN_LOG_LINES:
        lines = lines[:_MAX_RUN_LOG_LINES]
        lines.append(f"... (truncated at {_MAX_RUN_LOG_LINES} lines)")
    return True, lines, ""


# ---------- Target-state query (for the action menu) -----------------------


def query_target_state(path: Path, max_commits: int = 5) -> TargetState:
    """Snapshot the fields the action-menu header needs for a target
    checkout: current branch, upstream tracking ref, ahead/behind counts,
    origin URL, and the last `max_commits` commits on HEAD. Read-only —
    no git mutations. Empty/zero fields on any sub-query failure so the
    menu can still open against a detached / unborn / origin-less repo."""
    branch = ""
    rc, out, _ = git(path, ["branch", "--show-current"])
    if rc == 0:
        branch = out.strip() or "(detached)"

    upstream: Optional[str] = None
    rc, out, _ = git(path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and out.strip():
        upstream = out.strip()

    ahead = 0
    behind = 0
    if upstream:
        rc, out, _ = git(path, [
            "rev-list", "--count", "--left-right", f"{upstream}...HEAD"])
        if rc == 0:
            parts = out.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    rc, out, _ = git(path, ["remote", "get-url", "origin"])
    has_origin = rc == 0 and bool(out.strip())

    merging = False
    rc, out, _ = git(path, ["rev-parse", "--git-dir"])
    if rc == 0 and out.strip():
        gd = Path(out.strip())
        if not gd.is_absolute():
            gd = (path / gd).resolve()
        for marker in MERGE_MARKER_FILES:
            if (gd / marker).exists():
                merging = True
                break
        if not merging:
            for marker in MERGE_MARKER_DIRS:
                if (gd / marker).is_dir():
                    merging = True
                    break

    rc, out, _ = git(path, ["status", "--porcelain=v1"])
    dirty = rc == 0 and bool(out.strip())

    commits: List[str] = []
    rc, out, _ = git(path, [
        "log", f"-n{max_commits}", "--pretty=format:%h %s (%cr)"])
    if rc == 0 and out.strip():
        commits = out.strip().splitlines()

    return TargetState(
        branch=branch, upstream=upstream, ahead=ahead, behind=behind,
        has_origin=has_origin, merging=merging, dirty=dirty,
        recent_commits=commits,
    )


def query_working_tree(path: Path) -> List[FileEntry]:
    """Snapshot the working tree at `path` for the action-menu's tree
    pane: one FileEntry per changed/untracked file. Combines porcelain
    status (X/Y codes, untracked) with `git diff --numstat HEAD` so
    each row carries an insertion/deletion count when available."""
    entries: Dict[str, FileEntry] = {}
    rc, out, _ = git(path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return []
    for xy, p in _iter_porcelain_z_entries(out):
        if xy == "??":
            entries[p] = FileEntry(path=p, x=" ", y=" ", untracked=True)
            continue
        entries[p] = FileEntry(path=p, x=xy[0], y=xy[1])

    rc, out, _ = git(path, ["diff", "--numstat", "HEAD"])
    if rc != 0:
        rc, out, _ = git(path, ["diff", "--numstat"])
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
            fe = entries.get(parts[2])
            if fe is not None:
                fe.inserted = ins
                fe.deleted = dels

    # Sort: staged first, then unstaged-modified, then untracked. Within
    # each band, alphabetical for predictability.
    def sort_key(fe: FileEntry) -> Tuple[int, str]:
        band = 2 if fe.untracked else (0 if fe.x != " " else 1)
        return (band, fe.path.lower())

    return sorted(entries.values(), key=sort_key)


def load_commits(path: Path, skip: int, count: int) -> Tuple[
        List[CommitEntry], bool]:
    """Page through `git log` for the action-menu's commits tab. Returns
    (entries, exhausted) — `exhausted` flips True when git returned
    fewer rows than asked, meaning we've walked back to the root."""
    rc, out, _ = git(path, [
        "log", f"--skip={skip}", f"-n{count}",
        "--pretty=format:%h%x09%cr%x09%s",
    ])
    if rc != 0 or not out.strip():
        return [], True
    rows: List[CommitEntry] = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        rows.append(CommitEntry(
            sha=parts[0], relative=parts[1], subject=parts[2]))
    return rows, len(rows) < count


def list_remotes(path: Path) -> List[Tuple[str, str]]:
    """Return [(name, url), ...] for every remote configured on the repo,
    in `git remote` order (which is usually configured order). Each
    remote appears once — `git remote -v` lists fetch + push variants;
    we keep the fetch URL since that's what the user typically edits."""
    rc, out, _ = git(path, ["remote"])
    if rc != 0:
        return []
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    result: List[Tuple[str, str]] = []
    for name in names:
        rc2, url, _ = git(path, ["remote", "get-url", name])
        result.append((name, url.strip() if rc2 == 0 else ""))
    return result


def clone_repo(url: str, dest: Path, branch: str = "",
               recurse_submodules: bool = True) -> Tuple[bool, str]:
    """Run `git clone` into `dest`. `dest` must not already exist (or
    must be empty) — git refuses on a non-empty target. Returns
    (ok, message); message is the first non-empty stderr line on
    failure, or "" on success."""
    if not url or url.startswith("-"):
        return False, "remote URL looks like an option"
    args = ["clone"]
    if branch:
        if not is_safe_ref_arg(branch):
            return False, "branch name looks like an option"
        args.extend(["--branch", branch])
    if recurse_submodules:
        args.append("--recurse-submodules")
    args.extend(["--", url, str(dest)])
    # Run from the parent dir so a relative dest resolves predictably.
    parent = dest.parent if dest.parent.exists() else Path.cwd()
    rc, _, err = git(parent, args, timeout=600)
    if rc == 0:
        return True, ""
    return False, first_line(err)


def list_branches(path: Path) -> Tuple[List[str], str]:
    """Return (sorted unique branch names, current_branch). Local branches
    listed first; remote-tracking branches without a local counterpart
    come second (their `origin/` prefix stripped). HEAD is excluded."""
    current = ""
    rc, out, _ = git(path, ["branch", "--show-current"])
    if rc == 0:
        current = out.strip()

    rc, out, _ = git(path, [
        "branch", "-a", "--format=%(refname:short)"])
    if rc != 0:
        return [], current

    locals_seen: List[str] = []
    remote_only: List[str] = []
    have_local: set = set()
    for line in out.strip().splitlines():
        name = line.strip()
        if not name:
            continue
        if name.startswith("origin/HEAD"):
            continue
        if name.startswith("origin/"):
            short = name[len("origin/"):]
            if short and short not in have_local:
                if short not in remote_only:
                    remote_only.append(short)
        else:
            if name not in locals_seen:
                locals_seen.append(name)
                have_local.add(name)

    remote_only = [b for b in remote_only if b not in have_local]
    return locals_seen + remote_only, current


def list_stashes(path: Path) -> List[Tuple[str, str]]:
    """Return [(ref, message), ...] for every stash entry. `ref` is
    the stable form `stash@{N}` so the apply action gets a positional
    target that survives between calls; `message` is the
    human-friendly "On <branch>: <subject>" line.

    Empty list when there are no stashes (or the repo predates the
    `refs/stash` ref so `git stash list` returns nothing). Failures
    are coerced to an empty list — same shape as `list_branches`."""
    rc, out, _ = git(path, ["stash", "list", "--format=%gd%x09%gs"])
    if rc != 0:
        return []
    rows: List[Tuple[str, str]] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t", 1)
        ref = parts[0].strip()
        msg = parts[1] if len(parts) > 1 else ""
        if not ref:
            continue
        rows.append((ref, msg))
    return rows


def list_tags_at(path: Path, sha: str) -> List[str]:
    """Return every tag pointing at `sha`, in `git tag` order. Refuses
    `-`-prefixed shas as a defence-in-depth (git's argv parsing already
    rejects them in this position) so the caller doesn't have to."""
    if not sha or sha.startswith("-"):
        return []
    rc, out, _ = git(path, ["tag", "--points-at", sha])
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def query_commit_files(path: Path, sha: str) -> List[FileEntry]:
    """File-changes list for a single commit, in the same FileEntry
    shape `query_working_tree` returns so the review-pane file row
    renderer can display either without branching. `x` is the change
    status letter (M / A / D / R / …); `y` stays blank since commits
    don't carry a worktree-side state. Insertion / deletion counts
    come from `git show --numstat`."""
    if not sha or sha.startswith("-"):
        return []
    entries: Dict[str, FileEntry] = {}
    rc, name_out, _ = git(path, [
        "show", "--name-status", "--format=", sha,
    ])
    if rc != 0:
        return []
    for line in name_out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0] if parts[0] else " "
        # Renames have shape `R100\told\tnew` — pick the new path so
        # the row reflects what's in the tree after the commit.
        path_s = parts[-1]
        if path_s in entries:
            continue
        entries[path_s] = FileEntry(path=path_s, x=status, y=" ")
    rc2, num_out, _ = git(path, [
        "show", "--numstat", "--format=", sha,
    ])
    if rc2 == 0:
        for line in num_out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
            fe = entries.get(parts[-1])
            if fe is not None:
                fe.inserted = ins
                fe.deleted = dels
    return sorted(entries.values(), key=lambda f: f.path.lower())


def get_commit_details(path: Path, sha: str
                       ) -> Tuple[str, str, str, str]:
    """Return `(author, date, subject, body)` for `sha`. `date` uses
    git's --date=short (YYYY-MM-DD) for a stable display format.
    Empty tuple on failure / unsafe sha — caller treats blank fields
    as "not yet loaded" so the modal can render before this lands."""
    if not sha or sha.startswith("-"):
        return ("", "", "", "")
    # Use a NUL separator so multi-line bodies don't collide with
    # tab-separated fields.
    rc, out, _ = git(path, [
        "show", "-s",
        "--format=%an%x00%ad%x00%s%x00%b",
        "--date=short", sha,
    ])
    if rc != 0 or not out:
        return ("", "", "", "")
    parts = out.split("\x00", 3)
    while len(parts) < 4:
        parts.append("")
    return (parts[0], parts[1], parts[2], parts[3].rstrip("\n"))


def query_file_log(path: Path, file_path: str, *,
                   max_count: int = 200,
                   sha: str = "") -> List[str]:
    """Return one-line `<short-sha> <date> <subject>` rows for
    commits that touched `file_path`. When `sha` is supplied the
    log is scoped to commits reachable from that sha (useful when
    the diff viewer is opened against a specific commit). Empty
    list on failure / unsafe input."""
    if not file_path or file_path.startswith("-"):
        return []
    args = ["log", f"--max-count={max_count}",
            "--pretty=format:%h %ad %s", "--date=short"]
    if sha:
        if sha.startswith("-"):
            return []
        args.append(sha)
    args.extend(["--", file_path])
    rc, out, _ = git(path, args)
    if rc != 0 or not out:
        return []
    return out.splitlines()


def query_file_blame(path: Path, file_path: str, *,
                     sha: str = "") -> List[str]:
    """Return blame output lines for `file_path` (at `sha` if
    given). Each row is the standard `<short-sha> (<author> <date>
    <line-number>) <line content>` format git emits with
    `--abbrev=8`. Empty list when blame fails (binary file,
    untracked, etc.)."""
    if not file_path or file_path.startswith("-"):
        return []
    args = ["blame", "--abbrev=8"]
    if sha:
        if sha.startswith("-"):
            return []
        args.append(sha)
    args.extend(["--", file_path])
    rc, out, _ = git(path, args)
    if rc != 0:
        return []
    return out.splitlines()


def query_commit_reflog(path: Path, sha: str) -> List[str]:
    """Return HEAD reflog entries that mention `sha` as the
    resolved commit. Each row: `<short-sha> <reflog-selector>
    <reflog-message>`, e.g. `a1b2c3d HEAD@{2} reset: moving to
    HEAD~1`. Filtered client-side by matching the first
    space-separated token against the requested sha (full or
    short) — `git reflog` lacks a built-in filter and we'd rather
    do the comparison locally than hit the network. Empty list on
    failure or no matches."""
    if not sha:
        return []
    rc, out, _ = git(path, [
        "reflog", "--pretty=format:%h %gd %gs",
    ])
    if rc != 0 or not out:
        return []
    sha_short = sha[:8]
    matches: List[str] = []
    for line in out.splitlines():
        if not line:
            continue
        head = line.split(" ", 1)[0]
        if (head == sha_short or head == sha
                or sha.startswith(head) or head.startswith(sha_short)):
            matches.append(line)
    return matches


def commit_show_diff(path: Path, sha: str, file_path: str) -> List[str]:
    """Return the per-file diff lines for a commit — i.e. what
    `git show <sha> -- <path>` produces, split into lines. Used by
    the diff viewer when it's opened from the commit view modal."""
    if (not sha or sha.startswith("-")
            or not file_path or file_path.startswith("-")):
        return ["(unsafe sha or path)"]
    rc, out, err = git(path, [
        "show", sha, "--", file_path,
    ])
    if rc != 0 and not out:
        return [(err or "(no diff available)").strip()]
    return out.splitlines() if out else ["(no diff)"]


def list_remote_tracking_refs(path: Path) -> List[str]:
    """Return remote-tracking refs in their full form (e.g.
    `origin/main`, `upstream/dev`). Used by the set-upstream picker —
    `git branch --set-upstream-to=` wants the qualified ref, so we
    keep the prefix here rather than stripping it the way
    `list_branches` does."""
    rc, out, _ = git(path, ["branch", "-r", "--format=%(refname:short)"])
    if rc != 0:
        return []
    refs: List[str] = []
    for line in out.splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        refs.append(ref)
    return sorted(refs)


# ---------- Commit-message suggestion --------------------------------------


def _collect_changes_at(path: Path,
                        staged: List[Tuple[str, str]],
                        unstaged: List[Tuple[str, str]],
                        untracked: List[str],
                        auto_stage: bool) -> List[FileChange]:
    """Build FileChange entries for a working tree at `path`. Status lists
    are passed in; refresh_repo caches them on the Repo, and ad-hoc scans
    (e.g. nested submodule checkouts) call `_scan_path_status` first."""
    changes: Dict[str, FileChange] = {}

    diff_stats: Dict[str, int] = {}
    rc, out, _ = git(path, ["diff", "--numstat", "HEAD"])
    if rc != 0:
        rc, out, _ = git(path, ["diff", "--numstat"])
    if rc == 0:
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
                diff_stats[parts[2]] = ins + dels
            except ValueError:
                continue

    def classify(status: str) -> str:
        if status == "A":
            return "added"
        if status == "D":
            return "deleted"
        return "modified"

    def weight_for(kind: str, p: str) -> float:
        if kind == "added":
            try:
                return float((path / p).stat().st_size)
            except OSError:
                return 0.0
        if kind == "modified":
            return float(diff_stats.get(p, 0))
        return 0.0

    sources: List[Tuple[str, str]] = list(staged)
    if auto_stage:
        sources += list(unstaged)

    for status, p in sources:
        if p in changes:
            continue
        kind = classify(status)
        changes[p] = FileChange(path=p, kind=kind, weight=weight_for(kind, p))

    if auto_stage:
        for p in untracked:
            if p in changes:
                continue
            changes[p] = FileChange(path=p, kind="added", weight=weight_for("added", p))

    return list(changes.values())


def _scan_path_status(path: Path) -> Tuple[
        List[Tuple[str, str]], List[Tuple[str, str]], List[str], bool]:
    """Run `git status --porcelain=v1 -z` at `path` and split entries into
    (staged, unstaged, untracked, has_conflicts). Used by the ad-hoc Tab-
    suggest path on nested submodule checkouts."""
    staged: List[Tuple[str, str]] = []
    unstaged: List[Tuple[str, str]] = []
    untracked: List[str] = []
    has_conflicts = False
    rc, out, _ = git(path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return staged, unstaged, untracked, has_conflicts
    for xy, p in _iter_porcelain_z_entries(out):
        if xy == "??":
            untracked.append(p)
            continue
        if xy in CONFLICT_CODES:
            has_conflicts = True
            continue
        x, y = xy[0], xy[1]
        if x != " ":
            staged.append((x, p))
        if y != " ":
            unstaged.append((y, p))
    return staged, unstaged, untracked, has_conflicts


def collect_changes(repo: Repo, auto_stage: bool) -> List[FileChange]:
    """Build the FileChange list driving the review screen and the
    commit-message suggester for `repo`. When `auto_stage` is True, the
    unstaged + untracked sides feed in alongside the staged set
    (matches the "stage all" pipeline); when False, only already-staged
    paths show up. Pure transform over the snapshot already captured
    on the Repo dataclass — no re-querying git here."""
    return _collect_changes_at(
        repo.path, repo.staged, repo.unstaged, repo.untracked, auto_stage)


def _format_suggestion(changes: List[FileChange],
                       max_added: int, max_updated: int, max_deleted: int) -> str:
    """Pick the top files per category and join them into the canonical
    'add: a, b; update: c, d; remove: e' string. Imperative tense to
    match git's commit-message convention ("add foo" reads as "this
    commit will add foo", not "this commit added foo")."""
    if not changes:
        return ""
    by_kind: Dict[str, List[FileChange]] = {"added": [], "modified": [], "deleted": []}
    for c in changes:
        by_kind[c.kind].append(c)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda c: (-c.weight, c.path.lower()))

    caps = {"added": max_added, "modified": max_updated, "deleted": max_deleted}
    # Internal kind keys stay past-tense to avoid touching every FileChange
    # producer. Only the user-facing labels go imperative.
    label = {"added": "add", "modified": "update", "deleted": "remove"}
    parts: List[str] = []
    for kind in ("added", "modified", "deleted"):
        cap = caps[kind]
        if cap == 0:
            continue
        picks = by_kind[kind] if cap < 0 else by_kind[kind][:cap]
        if not picks:
            continue
        names = [Path(c.path).name for c in picks]
        parts.append(f"{label[kind]}: {', '.join(names)}")
    return "; ".join(parts)


def suggest_commit_message(repo: Repo, *,
                           max_added: int, max_updated: int, max_deleted: int,
                           auto_stage: bool) -> str:
    """Build an 'add: a, b; update: c, d; remove: e' message from the top
    files of each kind, ranked by weight. Returns '' if there is nothing to
    commit or the repo is mid-merge."""
    if repo.merging:
        return ""
    return _format_suggestion(
        collect_changes(repo, auto_stage),
        max_added, max_updated, max_deleted)


def suggest_commit_message_at(path: Path, *,
                              max_added: int, max_updated: int, max_deleted: int,
                              auto_stage: bool) -> str:
    """Like `suggest_commit_message`, but scans the working tree at `path`
    fresh — used for nested submodule checkouts where the status is not
    cached on a Repo. Returns '' if status fails or unmerged paths exist."""
    staged, unstaged, untracked, conflicts = _scan_path_status(path)
    if conflicts:
        return ""
    return _format_suggestion(
        _collect_changes_at(path, staged, unstaged, untracked, auto_stage),
        max_added, max_updated, max_deleted)


def suggest_commit_message_for_paths(
        path: Path, paths: List[str], *,
        max_added: int, max_updated: int, max_deleted: int) -> str:
    """Like `suggest_commit_message_at`, but restricted to a specific
    set of paths — the files the user has checked on the review
    screen. Every checked file is treated as headed for the index at
    commit time (auto_stage semantics) regardless of its current x/y
    state, so the suggestion describes the commit that would actually
    land. Returns '' on failure or if there's nothing to suggest."""
    staged, unstaged, untracked, conflicts = _scan_path_status(path)
    if conflicts:
        return ""
    paths_set = set(paths)
    staged = [(s, p) for (s, p) in staged if p in paths_set]
    unstaged = [(s, p) for (s, p) in unstaged if p in paths_set]
    untracked = [p for p in untracked if p in paths_set]
    return _format_suggestion(
        _collect_changes_at(path, staged, unstaged, untracked,
                            auto_stage=True),
        max_added, max_updated, max_deleted)


# ---------- LFS helpers ----------------------------------------------------


def format_size(num_bytes: int) -> str:
    """Human-readable size for the LFS-warning rows on the review
    screen. Always MB or GB — the LFS threshold is MB-scaled so any
    value worth flagging lives in that band, and the narrower set of
    units keeps the column visually tidy."""
    mb = num_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def format_time_ago(seconds: float) -> str:
    """Render a non-negative elapsed-seconds value as a compact "Ns / Nm
    ago" tag. Used in the sidebar so each task carries a recency hint that
    refreshes on every redraw. Negative inputs (clock skew, monotonic
    weirdness) are clamped to "now"."""
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def derive_lfs_pattern(path: str) -> str:
    """Best-effort LFS pattern. Use the file extension if it has one,
    otherwise fall back to the literal basename."""
    ext = Path(path).suffix
    if ext and len(ext) > 1:
        return f"*{ext}"
    return Path(path).name


def apply_lfs_tracking(cand: LFSCandidate) -> Tuple[bool, str]:
    """Add an LFS rule for this file, stage .gitattributes, and re-stage the
    file so the upcoming commit routes the blob through git-lfs."""
    repo = cand.repo
    pattern = derive_lfs_pattern(cand.path)
    rc, _, err = git(repo.path, ["lfs", "track", pattern])
    if rc != 0:
        return False, f"lfs track failed: {first_line(err)}"
    rc, _, err = git(repo.path, ["add", ".gitattributes"])
    if rc != 0:
        return False, f"add .gitattributes failed: {first_line(err)}"
    git(repo.path, ["rm", "--cached", "--ignore-unmatch", cand.path])
    rc, _, err = git(repo.path, ["add", cand.path])
    if rc != 0:
        return False, f"re-add failed: {first_line(err)}"
    return True, f"LFS-tracked via {pattern}"


def find_lfs_warnings(repo: Repo, auto_stage: bool,
                      threshold_bytes: int) -> List[Tuple[str, str]]:
    """Return [(path, size-str)] for files >= threshold_bytes that would be
    committed but aren't routed through git-lfs by .gitattributes. A
    threshold of 0 disables the check entirely."""
    if threshold_bytes <= 0:
        return []
    if auto_stage:
        candidates = [p for _, p in repo.staged]
        candidates += [p for _, p in repo.unstaged]
        candidates += list(repo.untracked)
    else:
        candidates = [p for _, p in repo.staged]

    warnings: List[Tuple[str, str]] = []
    seen: set = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        full = repo.path / path
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size < threshold_bytes:
            continue
        rc, out, _ = git(repo.path, ["check-attr", "filter", "--", path])
        is_lfs = rc == 0 and ": lfs" in out
        if not is_lfs:
            warnings.append((path, format_size(size)))
    return warnings


# ---------- Safe-merge: conflict-by-conflict resolution -------------------
#
# Everything here is Cardinal-Rule safe. We resolve a merge the way a human
# would by hand: write the *chosen* content into the working-tree file and
# `git add` it, then `git commit` once the index is clean. We never run
# `reset --hard`, `checkout --`, `clean`, `rebase`, or force-push, and we
# never delete a file on the user's behalf (modify/delete conflicts are
# surfaced as "manual" rather than auto-resolved). The only place safe-merge
# can discard anything is dropping the *backup stash it created itself*, and
# only when the user explicitly opts in on the confirm screen.

# Conflict marker prefixes git writes into a conflicted working-tree file.
_CONFLICT_OURS = "<<<<<<<"
_CONFLICT_BASE = "|||||||"
_CONFLICT_SEP = "======="
_CONFLICT_THEIRS = ">>>>>>>"


def merge_head_sha(path: Path) -> Optional[str]:
    """Short SHA of MERGE_HEAD when a merge is in progress, else None.
    Lets callers tell an in-progress merge (adopt its conflicts) from a
    clean tree (start a fresh merge)."""
    rc, out, _ = git(path, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
    if rc == 0 and out.strip():
        return out.strip()[:12]
    return None


def describe_merge_side(path: Path, ref: str, role: str,
                        branch_label: str = "") -> MergeSide:
    """Build a rich label for one side of a merge: branch/ref, short SHA,
    commit subject, and the origin remote (so conflict versions read as
    `origin · feature-x @ a1b2c3d` rather than bare ours/theirs)."""
    side = MergeSide(role=role)
    if ref and is_safe_ref_arg(ref):
        rc, out, _ = git(path, ["log", "-1", "--format=%h%x09%s", ref])
        if rc == 0 and out.strip():
            bits = out.strip().split("\t", 1)
            side.short_sha = bits[0].strip()
            side.subject = (bits[1] if len(bits) > 1 else "").strip()
    if branch_label:
        side.branch = branch_label
    elif role == "ours":
        rc, out, _ = git(path, ["branch", "--show-current"])
        side.branch = out.strip() or "(detached)"
    else:
        side.branch = ref or "MERGE_HEAD"
    rc, out, _ = git(path, ["remote", "get-url", "origin"])
    if rc == 0 and out.strip():
        side.remote = canonicalize_url(out.strip())
    return side


def head_short_info(path: Path) -> Tuple[str, str]:
    """(short_sha, subject) of the current HEAD — used to describe the
    merge commit on the confirm screen after it lands."""
    rc, out, _ = git(path, ["log", "-1", "--format=%h%x09%s"])
    if rc == 0 and out.strip():
        bits = out.strip().split("\t", 1)
        return bits[0].strip(), (bits[1] if len(bits) > 1 else "").strip()
    return "", ""


def create_named_stash(path: Path, name: str) -> Tuple[str, str]:
    """Snapshot the working tree (tracked + untracked) into a named stash
    as a pre-merge backup. Returns `(status, detail)` where status is
    "created" (a stash was made), "empty" (nothing to stash — clean tree),
    or "error". Cardinal-Rule safe: a stash preserves both index and
    worktree state and is fully recoverable via `git stash list`."""
    if not name or name.startswith("-"):
        return "error", "unsafe stash name"
    rc, out, err = git(path, ["stash", "push", "--include-untracked",
                              "-m", name])
    if rc != 0:
        return "error", first_line(err)
    if "no local changes" in (out + " " + err).lower():
        return "empty", "nothing to stash"
    return "created", first_line(out) or name


def drop_named_stash(path: Path, name: str) -> Tuple[bool, str]:
    """Drop the backup stash whose message contains `name`. This is the
    one data-discarding operation in safe-merge — it is ONLY ever called
    when the user explicitly ticks the off-by-default "remove backup
    stash" box on the confirm screen, against a stash safe-merge created
    itself moments earlier. Returns `(ok, detail-or-ref)`."""
    for ref, msg in list_stashes(path):
        if name in msg and not ref.startswith("-"):
            rc, _, err = git(path, ["stash", "drop", ref])
            if rc != 0:
                return False, first_line(err)
            return True, ref
    return False, "backup stash not found"


def is_fast_forward_merge(path: Path, ref: str) -> bool:
    """True when merging `ref` into the current branch would fast-forward
    (HEAD is an ancestor of `ref`). The branch picker uses this to route
    clean fast-forwards through the simple `ff_merge` action and divergent
    merges into the safe-merge resolver."""
    if not is_safe_ref_arg(ref):
        return False
    rc, _, _ = git(path, ["merge-base", "--is-ancestor", "HEAD", ref])
    return rc == 0


def begin_safe_merge(path: Path, ref: str) -> Tuple[int, str, str]:
    """Start a merge of `ref` into the current branch, stopping before the
    commit so safe-merge controls conflict resolution and the final commit.

    `--no-ff` guarantees a real merge commit (the whole point of the
    dialog), and `--no-commit` hands control back to us. `diff3` conflict
    style records the merge-base inside the markers so the UI can show the
    common ancestor. Returns git's `(rc, out, err)` — rc != 0 on conflicts
    (expected), 0 for a clean-but-uncommitted merge."""
    if not is_safe_ref_arg(ref):
        return 1, "", "unsafe merge ref"
    return git(path, [
        "-c", "merge.conflictStyle=diff3",
        "merge", "--no-ff", "--no-commit", ref,
    ])


def _conflict_stages(repo_path: Path, rel: str) -> "set[int]":
    """Set of index stages present for a conflicted path (1=base, 2=ours,
    3=theirs). Distinguishes both-modified (2 & 3) from modify/delete
    (only one of 2/3)."""
    stages: "set[int]" = set()
    rc, out, _ = git(repo_path, ["ls-files", "-u", "-z", "--", rel])
    if rc != 0:
        return stages
    for entry in out.split("\x00"):
        if "\t" not in entry:
            continue
        meta = entry.split("\t", 1)[0].split()
        if len(meta) >= 3:
            try:
                stages.add(int(meta[2]))
            except ValueError:
                pass
    return stages


def _read_index_stage_bytes(repo_path: Path, stage: int,
                            rel: str) -> Optional[bytes]:
    """Raw bytes of a conflicted path at an index stage (2=ours, 3=theirs).
    Used to resolve binary/whole-file conflicts by writing the chosen
    side's exact content — never `checkout --ours/--theirs`, which the
    Cardinal Rule forbids."""
    try:
        p = subprocess.run(
            ["git", "show", f":{stage}:{rel}"],
            cwd=str(repo_path), capture_output=True,
            timeout=DEFAULT_GIT_TIMEOUT_SECONDS, env=_git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def _is_marker(line: str, prefix: str) -> bool:
    """True when `line` (line-ending stripped) is a git conflict marker of
    the given 7-char kind. Git emits the marker as exactly 7 characters
    (the bare `=======` separator) or 7 characters + a space + a label
    (`<<<<<<< HEAD`, `>>>>>>> branch`, `||||||| base`). Requiring that 8th
    char to be a space (or nothing) keeps a content line of all-`=` longer
    than 7 (e.g. a Markdown H1 underline) from being misread as a
    separator."""
    bare = line.rstrip("\r\n")
    if not bare.startswith(prefix):
        return False
    return len(bare) == len(prefix) or bare[len(prefix)] == " "


def _parse_conflict_markers(text: str):
    """Split a conflicted file's text into an ordered rebuild plan.

    Returns `(parts, hunks)` or None when there are no parseable markers
    (or they're malformed — in which case the caller treats the file as
    needing manual/binary handling rather than guessing). Lines keep their
    endings so a resolved file rebuilds byte-for-byte; marker lines are
    dropped."""
    lines = text.splitlines(keepends=True)
    parts: List[Tuple[str, object]] = []
    hunks: List[ConflictHunk] = []
    ctx: List[str] = []
    found = False
    i = 0
    n = len(lines)

    while i < n:
        if _is_marker(lines[i], _CONFLICT_OURS):
            found = True
            if ctx:
                parts.append(("ctx", ctx))
                ctx = []
            ours: List[str] = []
            base: List[str] = []
            theirs: List[str] = []
            i += 1
            while i < n and not (_is_marker(lines[i], _CONFLICT_BASE)
                                 or _is_marker(lines[i], _CONFLICT_SEP)):
                ours.append(lines[i])
                i += 1
            if i < n and _is_marker(lines[i], _CONFLICT_BASE):
                i += 1
                while i < n and not _is_marker(lines[i], _CONFLICT_SEP):
                    base.append(lines[i])
                    i += 1
            if i >= n or not _is_marker(lines[i], _CONFLICT_SEP):
                return None  # malformed — no separator
            i += 1  # skip =======
            while i < n and not _is_marker(lines[i], _CONFLICT_THEIRS):
                theirs.append(lines[i])
                i += 1
            if i >= n or not _is_marker(lines[i], _CONFLICT_THEIRS):
                return None  # malformed — no closing marker
            i += 1  # skip >>>>>>>
            hunks.append(ConflictHunk(ours=ours, theirs=theirs, base=base))
            parts.append(("hunk", len(hunks) - 1))
        else:
            ctx.append(lines[i])
            i += 1
    if ctx:
        parts.append(("ctx", ctx))
    if not found:
        return None
    # Sanity gate: a well-formed conflict has exactly one opening, one
    # separator, and one closing marker per hunk. If a line of file
    # *content* legitimately starts with a marker (a doc about git, a diff
    # fixture), the prefix scan above mis-splits and these counts won't
    # line up — reject so the caller falls back to a safe whole-file pick
    # instead of presenting a corrupted hunk view.
    n_open = sum(1 for ln in lines if _is_marker(ln, _CONFLICT_OURS))
    n_sep = sum(1 for ln in lines if _is_marker(ln, _CONFLICT_SEP))
    n_close = sum(1 for ln in lines if _is_marker(ln, _CONFLICT_THEIRS))
    if not (n_open == n_sep == n_close == len(hunks)):
        return None
    return parts, hunks


def _classify_conflict(repo_path: Path, rel: str) -> ConflictFile:
    """Inspect one conflicted path and build its ConflictFile (kind +
    rebuild plan). Modify/delete and undecodable files are marked
    "manual" — safe-merge never deletes or force-overwrites them."""
    cf = ConflictFile(path=rel)
    stages = _conflict_stages(repo_path, rel)
    cf.ours_present = 2 in stages
    cf.theirs_present = 3 in stages
    if not (cf.ours_present and cf.theirs_present):
        cf.kind = "manual"
        if cf.theirs_present and not cf.ours_present:
            cf.note = "deleted by us, modified by them"
        elif cf.ours_present and not cf.theirs_present:
            cf.note = "modified by us, deleted by them"
        else:
            cf.note = "rename/special conflict"
        return cf
    full = repo_path / rel
    try:
        data = full.read_bytes()
    except OSError as e:
        cf.kind = "manual"
        cf.note = f"unreadable: {e}"
        return cf
    if b"\x00" in data:
        cf.kind = "binary"
        cf.note = "binary file"
        return cf
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        cf.kind = "binary"
        cf.note = "non-UTF-8 file"
        return cf
    parsed = _parse_conflict_markers(text)
    if parsed is None:
        # Both stages exist but no clean markers — treat as a whole-file
        # pick so the user can still resolve it without us guessing.
        cf.kind = "binary"
        cf.note = "no conflict markers"
        return cf
    cf.kind = "text"
    cf.parts, cf.hunks = parsed
    return cf


def parse_safe_merge_conflicts(path: Path) -> List[ConflictFile]:
    """Enumerate every unmerged path in `path` and classify each into a
    ConflictFile. Order follows `git status` (stable, alphabetical-ish)."""
    files: List[ConflictFile] = []
    rc, out, _ = git(path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return files
    seen: "set[str]" = set()
    for xy, rel in _iter_porcelain_z_entries(out):
        if xy in CONFLICT_CODES and rel not in seen:
            seen.add(rel)
            files.append(_classify_conflict(path, rel))
    return files


def rebuild_resolved_text(cf: ConflictFile) -> Optional[str]:
    """Reassemble a text ConflictFile from its parts, substituting each
    hunk's chosen side. Returns None if any hunk is still undecided."""
    out: List[str] = []
    for kind, val in cf.parts:
        if kind == "ctx":
            out.extend(val)  # type: ignore[arg-type]
            continue
        hunk = cf.hunks[val]  # type: ignore[index]
        if hunk.choice == "ours":
            out.extend(hunk.ours)
        elif hunk.choice == "theirs":
            out.extend(hunk.theirs)
        elif hunk.choice == "both":
            out.extend(hunk.ours)
            out.extend(hunk.theirs)
        else:
            return None
    return "".join(out)


def write_conflict_resolution(repo_path: Path,
                              cf: ConflictFile) -> Tuple[bool, str]:
    """Write the user's chosen resolution for one file into the working
    tree and stage it. Text files rebuild from hunk choices; binary/
    whole-file picks write the chosen index stage's bytes. Returns
    `(ok, detail)`. Never touches a "manual" file."""
    full = repo_path / cf.path
    if cf.kind == "text":
        content = rebuild_resolved_text(cf)
        if content is None:
            return False, "undecided hunk"
        try:
            with full.open("w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        except OSError as e:
            return False, str(e)
    elif cf.kind == "binary":
        stage = {"ours": 2, "theirs": 3}.get(cf.whole_choice, 0)
        if stage == 0:
            return False, "undecided file"
        data = _read_index_stage_bytes(repo_path, stage, cf.path)
        if data is None:
            return False, "could not read chosen version"
        try:
            full.write_bytes(data)
        except OSError as e:
            return False, str(e)
    else:
        return False, "manual conflict — resolve outside idlegit"
    rc, _, err = git(repo_path, ["add", "--", cf.path])
    if rc != 0:
        return False, first_line(err)
    return True, ""


def remaining_conflict_paths(path: Path) -> List[str]:
    """Paths still unmerged in the index (stage > 0). Empty when every
    conflict is resolved — the gate before completing the merge commit."""
    rc, out, _ = git(path, ["ls-files", "-u", "-z"])
    if rc != 0:
        return []
    seen: List[str] = []
    for entry in out.split("\x00"):
        if "\t" not in entry:
            continue
        rel = entry.split("\t", 1)[1]
        if rel and rel not in seen:
            seen.append(rel)
    return seen


def complete_safe_merge_commit(path: Path,
                               message: str = "") -> Tuple[int, str, str]:
    """Finalize the in-progress merge with a commit. Uses git's prepared
    MERGE_MSG (`--no-edit`) unless an explicit message is given. Refuses
    if any path is still unmerged. Plain `git commit` — no amend, no
    history rewrite."""
    if remaining_conflict_paths(path):
        return 1, "", "unresolved conflicts remain"
    if message:
        return git(path, ["commit", "-m", message])
    return git(path, ["commit", "--no-edit"])
