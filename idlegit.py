#!/usr/bin/env python3
"""
idlegit — interactive git multi-repo manager.

Scans a workspace for git repos (the workspace itself if it is one, plus
immediate child folders that contain .git) and lets you commit/push them
from a single screen.

    ↑/↓ navigates rows · type to enter a per-row commit message
    Tab on a repo row replaces its message with one suggested from the
        current working-tree changes ("added: a, b; updated: c, d; deleted: e")
    Enter opens a review screen · Enter again kicks off the work in the
        background — the right-hand sidebar tracks each git operation live
    Space toggles auto-stage / auto-push when on a toggle row
    Ctrl+R / F5 prunes completed tasks and re-fetches all repo state
    Esc clears a row's message, or quits (with confirmation if any
        commit messages are queued elsewhere)

Configuration:
    idlegit.conf next to this script (see file for inline comments).
"""
from __future__ import annotations

import configparser
import curses
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_FILE = TOOL_DIR / "idlegit.conf"

DEFAULT_ROOT = ".."
DEFAULT_SUGGEST = 3
DEFAULT_LFS_WARN_MB = 100  # GitHub rejects non-LFS pushes for blobs over 100 MB.
DEFAULT_BRANCH_DISPLAY_MAX = 12
DEFAULT_NAME_DISPLAY_MAX = 40
DEFAULT_TRUNCATION_MODE = "middle"
TRUNCATION_MODES = ("start", "middle", "end")

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# git status XY codes that indicate an unmerged path.
CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

# .git/<marker> files/dirs that mean a merge-like operation is in progress.
MERGE_MARKER_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
MERGE_MARKER_DIRS = ("rebase-merge", "rebase-apply")


# ---------- Config ----------------------------------------------------------


@dataclass
class SubtreeSpec:
    """One [subtree.<name>] section from idlegit.conf — declares that
    `parent` repo embeds `source` repo's content at `prefix`."""
    name: str
    parent: str  # rel of the parent tracked repo ("." for workspace root)
    source: str  # rel of the source tracked repo
    prefix: str  # path inside parent (e.g. "vendor/lib")


@dataclass
class Config:
    workspace: Path
    suggest_added: int = DEFAULT_SUGGEST
    suggest_updated: int = DEFAULT_SUGGEST
    suggest_deleted: int = DEFAULT_SUGGEST
    lfs_warn_bytes: int = DEFAULT_LFS_WARN_MB * 1024 * 1024
    default_auto_stage: bool = True
    default_auto_push: bool = True
    branch_display_max: int = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = DEFAULT_NAME_DISPLAY_MAX
    name_truncation: str = DEFAULT_TRUNCATION_MODE
    branch_truncation: str = DEFAULT_TRUNCATION_MODE
    subtrees: List["SubtreeSpec"] = field(default_factory=list)


def load_config() -> Config:
    """Read idlegit.conf and return a Config. Missing keys fall back to
    defaults; a malformed file falls back wholesale."""
    root_str = DEFAULT_ROOT
    suggest_added = DEFAULT_SUGGEST
    suggest_updated = DEFAULT_SUGGEST
    suggest_deleted = DEFAULT_SUGGEST
    lfs_warn_mb = DEFAULT_LFS_WARN_MB
    default_auto_stage = True
    default_auto_push = True
    branch_display_max = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max = DEFAULT_NAME_DISPLAY_MAX
    name_truncation = DEFAULT_TRUNCATION_MODE
    branch_truncation = DEFAULT_TRUNCATION_MODE
    subtrees: List[SubtreeSpec] = []

    if CONFIG_FILE.exists():
        try:
            cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
            cp.read(CONFIG_FILE)
            root_str = cp.get("idlegit", "root", fallback=DEFAULT_ROOT)
            suggest_added = cp.getint("idlegit", "suggest_added", fallback=DEFAULT_SUGGEST)
            suggest_updated = cp.getint("idlegit", "suggest_updated", fallback=DEFAULT_SUGGEST)
            suggest_deleted = cp.getint("idlegit", "suggest_deleted", fallback=DEFAULT_SUGGEST)
            lfs_warn_mb = cp.getint("idlegit", "lfs_warn_mb", fallback=DEFAULT_LFS_WARN_MB)
            default_auto_stage = cp.getboolean(
                "idlegit", "default_auto_stage", fallback=True)
            default_auto_push = cp.getboolean(
                "idlegit", "default_auto_push", fallback=True)
            branch_display_max = cp.getint(
                "idlegit", "branch_display_max", fallback=DEFAULT_BRANCH_DISPLAY_MAX)
            name_display_max = cp.getint(
                "idlegit", "name_display_max", fallback=DEFAULT_NAME_DISPLAY_MAX)
            name_truncation = cp.get(
                "idlegit", "name_truncation",
                fallback=DEFAULT_TRUNCATION_MODE).strip().lower()
            branch_truncation = cp.get(
                "idlegit", "branch_truncation",
                fallback=DEFAULT_TRUNCATION_MODE).strip().lower()
            for section in cp.sections():
                if not section.startswith("subtree."):
                    continue
                name = section[len("subtree."):]
                parent_rel = cp.get(section, "parent", fallback="").strip()
                source_rel = cp.get(section, "source", fallback="").strip()
                prefix = cp.get(section, "prefix", fallback="").strip().strip("/")
                if parent_rel and source_rel and prefix:
                    subtrees.append(SubtreeSpec(
                        name=name, parent=parent_rel,
                        source=source_rel, prefix=prefix,
                    ))
        except (configparser.Error, OSError, ValueError):
            pass

    if name_truncation not in TRUNCATION_MODES:
        name_truncation = DEFAULT_TRUNCATION_MODE
    if branch_truncation not in TRUNCATION_MODES:
        branch_truncation = DEFAULT_TRUNCATION_MODE

    p = Path(root_str).expanduser()
    if not p.is_absolute():
        p = TOOL_DIR / p

    return Config(
        workspace=p.resolve(),
        suggest_added=max(0, suggest_added),
        suggest_updated=max(0, suggest_updated),
        suggest_deleted=max(0, suggest_deleted),
        lfs_warn_bytes=max(0, lfs_warn_mb) * 1024 * 1024,
        default_auto_stage=default_auto_stage,
        default_auto_push=default_auto_push,
        branch_display_max=branch_display_max,
        name_display_max=name_display_max,
        name_truncation=name_truncation,
        branch_truncation=branch_truncation,
        subtrees=subtrees,
    )


# ---------- Models ----------------------------------------------------------


@dataclass
class Repo:
    rel: str
    path: Path
    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    remote_url: Optional[str] = None  # canonicalized for matching
    remote_url_raw: Optional[str] = None  # original git remote URL for ops
    ahead: int = 0
    behind: int = 0
    staged: List[Tuple[str, str]] = field(default_factory=list)
    unstaged: List[Tuple[str, str]] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    nested_subs: List[Tuple[str, Path]] = field(default_factory=list)
    siblings: List[Tuple["Repo", Path]] = field(default_factory=list)
    children: List["ChildRef"] = field(default_factory=list)
    message: str = ""
    error: str = ""
    merging: bool = False
    conflict_paths: List[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.rel == ".":
            return f"{self.path.name} (root)"
        return self.rel

    @property
    def is_dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked)

    def refresh(self) -> None:
        self.branch = ""
        self.head = ""
        self.upstream = None
        self.remote_url = None
        self.remote_url_raw = None
        self.ahead = 0
        self.behind = 0
        self.staged = []
        self.unstaged = []
        self.untracked = []
        self.nested_subs = []
        # siblings + children are filled by link_siblings() after every repo refreshes
        self.error = ""
        self.merging = False
        self.conflict_paths = []

        rc, out, err = git(self.path, ["rev-parse", "--is-inside-work-tree"])
        if rc != 0:
            self.error = "not a git work tree"
            return

        rc, out, _ = git(self.path, ["branch", "--show-current"])
        self.branch = out.strip() or "(detached)"

        rc, out, _ = git(self.path, ["rev-parse", "HEAD"])
        if rc == 0:
            self.head = out.strip()

        rc, out, _ = git(self.path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        self.upstream = out.strip() if rc == 0 and out.strip() else None

        rc, out, _ = git(self.path, ["remote", "get-url", "origin"])
        if rc == 0 and out.strip():
            self.remote_url_raw = out.strip()
            self.remote_url = canonicalize_url(out.strip())

        if self.upstream:
            rc, out, _ = git(self.path, [
                "rev-list", "--count", "--left-right",
                f"{self.upstream}...HEAD",
            ])
            if rc == 0:
                parts = out.split()
                if len(parts) == 2:
                    try:
                        self.behind = int(parts[0])
                        self.ahead = int(parts[1])
                    except ValueError:
                        pass

        rc, out, err = git(self.path, ["status", "--porcelain=v1", "-z"])
        if rc != 0:
            self.error = (err or "git status failed").strip().splitlines()[0]
            return
        for entry in out.split("\x00"):
            if len(entry) < 3:
                continue
            xy = entry[:2]
            p = entry[3:]
            if xy == "??":
                self.untracked.append(p)
                continue
            if xy in CONFLICT_CODES:
                self.merging = True
                self.conflict_paths.append(p)
                continue
            x, y = xy[0], xy[1]
            if x != " ":
                self.staged.append((x, p))
            if y != " ":
                self.unstaged.append((y, p))

        # Detect mid-merge / mid-rebase / mid-cherry-pick / mid-revert via .git markers.
        rc, out, _ = git(self.path, ["rev-parse", "--git-dir"])
        if rc == 0 and out.strip():
            git_dir = Path(out.strip())
            if not git_dir.is_absolute():
                git_dir = (self.path / git_dir).resolve()
            if not self.merging:
                for marker in MERGE_MARKER_FILES:
                    if (git_dir / marker).exists():
                        self.merging = True
                        break
            if not self.merging:
                for marker in MERGE_MARKER_DIRS:
                    if (git_dir / marker).is_dir():
                        self.merging = True
                        break

        if (self.path / ".gitmodules").exists():
            rc, out, _ = git(self.path, [
                "config", "-f", ".gitmodules",
                "--get-regexp", r"submodule\..+\.path",
            ])
            if rc == 0:
                for line in out.strip().splitlines():
                    parts = line.split(maxsplit=1)
                    if len(parts) != 2:
                        continue
                    key, path_str = parts
                    if not key.startswith("submodule.") or not key.endswith(".path"):
                        continue
                    name = key[len("submodule."):-len(".path")]
                    rc2, url_out, _ = git(self.path, [
                        "config", "-f", ".gitmodules",
                        f"submodule.{name}.url",
                    ])
                    if rc2 != 0 or not url_out.strip():
                        continue
                    sub_path = (self.path / path_str.strip()).resolve()
                    self.nested_subs.append((canonicalize_url(url_out.strip()), sub_path))


@dataclass
class ChildRef:
    """A nested-content reference inside another tracked repo. Either a real
    git submodule (kind="submodule"), in which case `head` and `in_sync` are
    populated, or a configured subtree (kind="subtree"), in which case sync
    state is not measured here (subtree drift requires `git subtree split`).

    `message` is the per-child pending commit message (only used for
    submodule kinds — a subtree's working tree belongs to its parent)."""
    repo: Repo
    nested_path: Path
    head: str = ""
    in_sync: bool = True
    kind: str = "submodule"
    message: str = ""


@dataclass
class Task:
    """One unit of background work shown in the right-hand sidebar."""
    label: str
    status: str = "running"  # running / ok / fail / warn
    message: str = ""


class Tasks:
    """Thread-safe list of background-work entries. Worker threads add and
    update; the draw loop snapshots."""
    def __init__(self) -> None:
        self.items: List[Task] = []
        self.lock = threading.Lock()

    def add(self, label: str) -> Task:
        with self.lock:
            t = Task(label=label)
            self.items.append(t)
            return t

    def update(self, task: Task, status: str, message: str = "") -> None:
        with self.lock:
            task.status = status
            if message:
                task.message = message

    def snapshot(self) -> List[Task]:
        with self.lock:
            return list(self.items)

    def prune_completed(self) -> None:
        with self.lock:
            self.items = [t for t in self.items if t.status == "running"]

    def has_running(self) -> bool:
        with self.lock:
            return any(t.status == "running" for t in self.items)


@dataclass
class State:
    repos: List[Repo]
    workspace_name: str
    suggest_added: int = DEFAULT_SUGGEST
    suggest_updated: int = DEFAULT_SUGGEST
    suggest_deleted: int = DEFAULT_SUGGEST
    lfs_warn_bytes: int = DEFAULT_LFS_WARN_MB * 1024 * 1024
    branch_display_max: int = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = DEFAULT_NAME_DISPLAY_MAX
    name_truncation: str = DEFAULT_TRUNCATION_MODE
    branch_truncation: str = DEFAULT_TRUNCATION_MODE
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    selected: int = 2  # 0,1 = toggles; 2..N+1 = repos
    auto_stage: bool = True
    auto_push: bool = True
    tasks: Tasks = field(default_factory=Tasks)
    spinner_frame: int = 0
    # Cursor position within the focused row's message field. Reset to the
    # end of the message whenever the focused row changes (see _reset_field_cursor).
    field_cursor: int = 0

    def selectable_rows(self) -> List[Tuple]:
        """Flat selectable list: ('toggle', 0|1, None), ('repo', repo, None),
        ('child', parent_repo, child_ref). Toggles are first, then each repo
        followed by its children (interleaved, in display order)."""
        rows: List[Tuple] = [("toggle", 0, None), ("toggle", 1, None)]
        for repo in self.repos:
            rows.append(("repo", repo, None))
            for child in repo.children:
                rows.append(("child", repo, child))
        return rows

    @property
    def total_rows(self) -> int:
        return 2 + sum(1 + len(r.children) for r in self.repos)

    @property
    def on_toggle(self) -> bool:
        return self.selected < 2

    @property
    def current_repo(self) -> Optional[Repo]:
        rows = self.selectable_rows()
        if 0 <= self.selected < len(rows):
            r = rows[self.selected]
            if r[0] == "repo":
                return r[1]
        return None

    @property
    def current_child(self) -> Optional[Tuple[Repo, ChildRef]]:
        """If the focused row is a child, return (parent_repo, child_ref).
        Returns None when on a toggle or top-level repo row."""
        rows = self.selectable_rows()
        if 0 <= self.selected < len(rows):
            r = rows[self.selected]
            if r[0] == "child":
                return r[1], r[2]
        return None

    @property
    def has_messages(self) -> bool:
        if any(r.message.strip() for r in self.repos):
            return True
        for repo in self.repos:
            for child in repo.children:
                if child.message.strip():
                    return True
        return False


@dataclass
class LFSCandidate:
    repo: Repo
    path: str
    size_str: str
    line_index: int = -1
    track: bool = False


@dataclass
class FileChange:
    """One file change in the working tree, classified for commit-message
    suggestion. `weight` is the sort key (higher = more 'interesting')."""
    path: str
    kind: str  # "added" / "modified" / "deleted"
    weight: float = 0.0


# ---------- Git -------------------------------------------------------------


def git(path: Path, args: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(
        ["git", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


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


def discover_repos(workspace: Path) -> List[Repo]:
    """Return the workspace itself (if it's a git repo) plus every immediate
    child folder containing .git, sorted alphabetically. The folder this
    script lives in is always skipped."""
    repos: List[Repo] = []
    if (workspace / ".git").exists():
        repos.append(Repo(rel=".", path=workspace))
    try:
        children = sorted(workspace.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return repos
    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        try:
            if child.resolve() == TOOL_DIR:
                continue
        except OSError:
            continue
        if (child / ".git").exists():
            repos.append(Repo(rel=child.name, path=child.resolve()))
    return repos


def link_siblings(repos: List[Repo],
                  subtrees: Optional[List[SubtreeSpec]] = None) -> None:
    """For each tracked repo X, find every other tracked repo Y that
    contains X — either as a nested submodule (auto-detected from
    .gitmodules) or as a subtree (declared in idlegit.conf). Records:
        - X.siblings — list of (Y, nested_path) for submodule sync-after-push
        - Y.children — ChildRef entries (kind="submodule"/"subtree") for the
          indented rows below Y on the main screen
    The workspace root is skipped for submodule auto-discovery (its
    submodules are already top-level rows); subtrees are honored regardless."""
    url_to_repo = {r.remote_url: r for r in repos if r.remote_url}
    rel_to_repo = {r.rel: r for r in repos}
    for r in repos:
        r.siblings = []
        r.children = []

    # Submodule references — discovered from each parent's .gitmodules.
    for parent in repos:
        if parent.rel == ".":
            continue
        for url, sub_path in parent.nested_subs:
            target = url_to_repo.get(url)
            if target is None or target is parent:
                continue
            target.siblings.append((parent, sub_path))
            ref = ChildRef(repo=target, nested_path=sub_path, kind="submodule")
            rc, out, _ = git(sub_path, ["rev-parse", "HEAD"])
            if rc == 0:
                ref.head = out.strip()
                ref.in_sync = bool(target.head) and ref.head == target.head
            else:
                ref.in_sync = False
            parent.children.append(ref)

    # Subtree references — declared in idlegit.conf.
    for spec in subtrees or []:
        parent = rel_to_repo.get(spec.parent)
        source = rel_to_repo.get(spec.source)
        if parent is None or source is None or parent is source:
            continue
        nested_path = (parent.path / spec.prefix).resolve()
        ref = ChildRef(repo=source, nested_path=nested_path, kind="subtree")
        # No cheap drift signal for subtrees; leave in_sync at default True.
        parent.children.append(ref)

    for r in repos:
        r.children.sort(key=lambda c: (c.kind, c.repo.display_name.lower()))


def sync_sibling(sibling_path: Path, branch: str) -> Tuple[bool, str]:
    """Fetch + checkout origin/<branch> in a sibling's nested submodule
    checkout so it lines up with what we just pushed."""
    rc, _, err = git(sibling_path, ["fetch", "origin"])
    if rc != 0:
        return False, f"fetch failed: {first_line(err)}"
    rc, _, err = git(sibling_path, ["checkout", f"origin/{branch}"])
    if rc != 0:
        return False, f"checkout failed: {first_line(err)}"
    return True, "synced"


def sync_subtree(parent_path: Path, prefix: str,
                 source_url: str, source_branch: str) -> Tuple[bool, str]:
    """Run `git subtree pull --squash` in the parent so the subtree's
    nested files catch up with the source repo's branch tip. NOTE: this
    creates a (squashed) merge commit in the parent — subtrees inherently
    can't be synced without one."""
    if not source_url:
        return False, "source repo has no remote URL"
    rc, _, err = git(parent_path, [
        "subtree", "pull", "--prefix=" + prefix,
        source_url, source_branch, "--squash",
    ])
    if rc != 0:
        return False, f"subtree pull failed: {first_line(err)}"
    return True, "subtree pulled"


# ---------- Commit-message suggestion --------------------------------------


def _collect_changes_at(path: Path,
                        staged: List[Tuple[str, str]],
                        unstaged: List[Tuple[str, str]],
                        untracked: List[str],
                        auto_stage: bool) -> List[FileChange]:
    """Build FileChange entries for a working tree at `path`. Status lists
    are passed in; Repo.refresh caches them on the Repo, and ad-hoc scans
    (e.g. nested submodule checkouts) call `_scan_path_status` first."""
    changes: Dict[str, FileChange] = {}

    diff_stats: Dict[str, int] = {}
    rc, out, _ = git(path, ["diff", "--numstat", "HEAD"])
    if rc != 0:
        # No HEAD yet (initial commit) — fall back to working-vs-index.
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
        return 0.0  # deleted — no working tree to measure

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
    for entry in out.split("\x00"):
        if len(entry) < 3:
            continue
        xy = entry[:2]
        p = entry[3:]
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
    """Walk the repo's pending changes and classify each one as
    added / modified / deleted with a weight for sorting.

    With auto_stage on we count staged + unstaged + untracked (everything
    `git add -A` will pick up). With it off we look at staged only — that's
    all that will actually be committed."""
    return _collect_changes_at(
        repo.path, repo.staged, repo.unstaged, repo.untracked, auto_stage)


def _format_suggestion(changes: List[FileChange],
                       max_added: int, max_updated: int, max_deleted: int) -> str:
    """Pick the top files per category and join them into the canonical
    'added: a, b; updated: c, d; deleted: e' string."""
    if not changes:
        return ""
    by_kind: Dict[str, List[FileChange]] = {"added": [], "modified": [], "deleted": []}
    for c in changes:
        by_kind[c.kind].append(c)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda c: (-c.weight, c.path.lower()))

    caps = {"added": max_added, "modified": max_updated, "deleted": max_deleted}
    label = {"added": "added", "modified": "updated", "deleted": "deleted"}
    parts: List[str] = []
    for kind in ("added", "modified", "deleted"):
        cap = caps[kind]
        if cap <= 0:
            continue
        picks = by_kind[kind][:cap]
        if not picks:
            continue
        names = [Path(c.path).name for c in picks]
        parts.append(f"{label[kind]}: {', '.join(names)}")
    return "; ".join(parts)


def suggest_commit_message(repo: Repo, *,
                           max_added: int, max_updated: int, max_deleted: int,
                           auto_stage: bool) -> str:
    """Build a 'added: a, b; updated: c, d; deleted: e' message from the top
    files of each kind, ranked by weight. A category with max == 0 is
    omitted entirely. Returns '' if there is nothing to commit or the repo
    is mid-merge."""
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


# ---------- Background commit pipeline -------------------------------------


def commit_worker(repo: Repo, msg: str, auto_stage: bool, auto_push: bool,
                  lfs_cands: List[LFSCandidate], tasks: Tasks) -> None:
    """Run the full stage / commit / push / sync pipeline for one repo,
    publishing each step into `tasks` so the sidebar shows it live.

    This runs in a worker thread; never touches curses state."""
    name = repo.display_name

    # Phase 1 — LFS tracking for any toggled candidates.
    for cand in lfs_cands:
        if not cand.track:
            continue
        t = tasks.add(f"{name}: lfs track {Path(cand.path).name}")
        ok, msg_ret = apply_lfs_tracking(cand)
        tasks.update(t, "ok" if ok else "fail", msg_ret)

    # Phase 2 — stage / commit / push / sync.
    if repo.merging:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "merge in progress")
        return

    if auto_stage:
        t = tasks.add(f"{name}: git add -A")
        rc, _, err = git(repo.path, ["add", "-A"])
        if rc != 0:
            tasks.update(t, "fail", first_line(err))
            return
        tasks.update(t, "ok")

    rc, _, _ = git(repo.path, ["diff", "--cached", "--quiet"])
    if rc == 0:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    t = tasks.add(f"{name}: commit")
    rc, _, err = git(repo.path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    t = tasks.add(f"{name}: push")
    if repo.upstream:
        rc, _, err = git(repo.path, ["push"])
    else:
        rc, _, err = git(repo.path, ["push", "--set-upstream", "origin", repo.branch])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    for sib_repo, sib_path in repo.siblings:
        t = tasks.add(f"  ↳ sync {sib_repo.display_name}")
        ok, sync_msg = sync_sibling(sib_path, repo.branch)
        tasks.update(t, "ok" if ok else "fail", sync_msg)


def commit_worker_for_child(parent: Repo, ref: ChildRef, msg: str,
                            auto_stage: bool, auto_push: bool,
                            tasks: Tasks) -> None:
    """Run the stage / commit / push pipeline against `ref.nested_path` —
    the working tree of a nested submodule checkout inside `parent`.

    After a successful push, sync every other place this submodule is
    checked out (the canonical top-level repo + every other parent's nested
    copy) so they all advance to the new commit."""
    name = f"{ref.repo.display_name} (in {parent.display_name})"

    # Submodules are typically in detached HEAD. Refuse rather than guessing
    # which branch the user wanted to commit on.
    rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
    nested_branch = out.strip() if rc == 0 else ""
    if not nested_branch:
        t = tasks.add(f"{name}: cannot commit")
        tasks.update(t, "fail",
                     "detached HEAD — checkout a branch in the nested "
                     "submodule first")
        return

    if auto_stage:
        t = tasks.add(f"{name}: git add -A")
        rc, _, err = git(ref.nested_path, ["add", "-A"])
        if rc != 0:
            tasks.update(t, "fail", first_line(err))
            return
        tasks.update(t, "ok")

    rc, _, _ = git(ref.nested_path, ["diff", "--cached", "--quiet"])
    if rc == 0:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    t = tasks.add(f"{name}: commit")
    rc, _, err = git(ref.nested_path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    rc, up_out, _ = git(ref.nested_path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    has_upstream = rc == 0 and up_out.strip()

    t = tasks.add(f"{name}: push")
    if has_upstream:
        rc, _, err = git(ref.nested_path, ["push"])
    else:
        rc, _, err = git(ref.nested_path, [
            "push", "--set-upstream", "origin", nested_branch])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    # After pushing from this nested checkout, every other instance of the
    # source repo (top-level + sibling parents' nested copies) is now behind
    # — fetch and check out the new commit in each.
    targets: List[Tuple[str, Path]] = [
        (f"top-level {ref.repo.display_name}", ref.repo.path),
    ]
    for other_parent, other_path in ref.repo.siblings:
        if other_path == ref.nested_path:
            continue
        targets.append(
            (f"{ref.repo.display_name} in {other_parent.display_name}",
             other_path))

    for label, target_path in targets:
        t = tasks.add(f"  ↳ sync {label}")
        ok, sync_msg = sync_sibling(target_path, nested_branch)
        tasks.update(t, "ok" if ok else "fail", sync_msg)


def kick_off_sync_siblings(state: State) -> None:
    """Sync every child reference shown on the main screen:
        - submodules: fetch + checkout origin/<branch> in the nested checkout
          (read-only — no commits)
        - subtrees:   git subtree pull --squash (creates a squashed merge
          commit in the parent — that is the subtree workflow)
    """
    work_items: List[Tuple[Repo, ChildRef]] = []
    for parent in state.repos:
        for ref in parent.children:
            work_items.append((parent, ref))
    if not work_items:
        return

    def worker() -> None:
        threads: List[threading.Thread] = []
        for parent, ref in work_items:
            glyph = "↳" if ref.kind == "submodule" else "⊕"
            t = state.tasks.add(
                f"{glyph} {ref.repo.display_name} in {parent.display_name}")

            def do_one(t=t, parent=parent, ref=ref) -> None:
                if ref.kind == "submodule":
                    ok, msg = sync_sibling(ref.nested_path, ref.repo.branch)
                else:  # subtree
                    try:
                        prefix = str(ref.nested_path.relative_to(parent.path))
                    except ValueError:
                        prefix = ""
                    ok, msg = sync_subtree(
                        parent.path, prefix,
                        ref.repo.remote_url_raw or "", ref.repo.branch)
                state.tasks.update(t, "ok" if ok else "fail", msg)

            th = threading.Thread(target=do_one, daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        # Silent refresh so child sync-status dots and parent dirty markers
        # (subtree pulls leave new commits) update immediately.
        for r in state.repos:
            r.refresh()
        link_siblings(state.repos, state.subtrees)

    threading.Thread(target=worker, daemon=True).start()


def kick_off_workers(state: State, candidates: List[LFSCandidate]) -> None:
    """Launch one worker thread per repo / nested-child with a queued
    message and a supervisor thread that silently re-fetches repo state
    once everything finishes. Returns immediately so the UI stays
    responsive."""
    repo_plans: List[Tuple[Repo, str, List[LFSCandidate]]] = []
    for repo in state.repos:
        msg = repo.message.strip()
        if not msg:
            continue
        repo_cands = [c for c in candidates if c.repo is repo]
        repo_plans.append((repo, msg, repo_cands))
        repo.message = ""

    child_plans: List[Tuple[Repo, ChildRef, str]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind != "submodule":
                continue
            msg = ref.message.strip()
            if not msg:
                continue
            child_plans.append((parent, ref, msg))
            ref.message = ""

    if not repo_plans and not child_plans:
        return

    workers: List[threading.Thread] = []
    for repo, msg, repo_cands in repo_plans:
        w = threading.Thread(
            target=commit_worker,
            args=(repo, msg, state.auto_stage, state.auto_push, repo_cands, state.tasks),
            daemon=True,
        )
        w.start()
        workers.append(w)

    for parent, ref, msg in child_plans:
        w = threading.Thread(
            target=commit_worker_for_child,
            args=(parent, ref, msg, state.auto_stage, state.auto_push, state.tasks),
            daemon=True,
        )
        w.start()
        workers.append(w)

    def supervisor() -> None:
        for w in workers:
            w.join()
        # Silent refresh once every worker finishes — the sidebar already
        # tells the story, so no spinner overlay here.
        for r in state.repos:
            r.refresh()
        link_siblings(state.repos, state.subtrees)

    threading.Thread(target=supervisor, daemon=True).start()


# ---------- Colors ----------------------------------------------------------


PAIR_BRANCH = 1
PAIR_DIRTY = 2
PAIR_TOGGLE_ON = 3
PAIR_TOGGLE_OFF = 4
PAIR_HINT = 5
PAIR_OK = 6
PAIR_ERR = 7
PAIR_WARN = 8
PAIR_HEADER = 9
PAIR_AHEAD = 10
PAIR_BEHIND = 11
# Sidebar pairs share a darker bg so the panel reads as a distinct surface.
PAIR_SB_FG = 12
PAIR_SB_CYAN = 13
PAIR_SB_OK = 14
PAIR_SB_ERR = 15
PAIR_SB_WARN = 16


def init_colors() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(PAIR_BRANCH, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_DIRTY, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_TOGGLE_ON, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_TOGGLE_OFF, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_HINT, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_ERR, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_HEADER, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_AHEAD, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_BEHIND, curses.COLOR_MAGENTA, bg)
    sb_bg = curses.COLOR_BLACK
    curses.init_pair(PAIR_SB_FG, curses.COLOR_WHITE, sb_bg)
    curses.init_pair(PAIR_SB_CYAN, curses.COLOR_CYAN, sb_bg)
    curses.init_pair(PAIR_SB_OK, curses.COLOR_GREEN, sb_bg)
    curses.init_pair(PAIR_SB_ERR, curses.COLOR_RED, sb_bg)
    curses.init_pair(PAIR_SB_WARN, curses.COLOR_YELLOW, sb_bg)


def state_color(repo: Repo) -> Tuple[str, int]:
    """Return (state-label, attr) for the dot showing this repo's state."""
    if repo.error:
        return "error", curses.color_pair(PAIR_ERR)
    if repo.merging:
        return "merging", curses.color_pair(PAIR_ERR)
    if repo.ahead > 0 and repo.behind > 0:
        return "diverged", curses.color_pair(PAIR_ERR)
    if repo.is_dirty:
        return "dirty", curses.color_pair(PAIR_DIRTY)
    if repo.behind > 0:
        return "behind", curses.color_pair(PAIR_BEHIND)
    if repo.ahead > 0:
        return "ahead", curses.color_pair(PAIR_AHEAD)
    if not repo.upstream:
        return "no upstream", curses.A_DIM
    return "clean", curses.color_pair(PAIR_OK)


def truncate(text: str, max_len: int, mode: str = DEFAULT_TRUNCATION_MODE) -> str:
    """Cap text at max_len (incl. ellipsis). `mode` is one of "start" (drop
    from the front, keep the tail), "middle" (drop from the middle, keep
    both ends), or "end" (drop from the back, keep the head). Unknown
    modes fall back to "middle". max_len <= 0 disables truncation entirely."""
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    keep = max_len - 1
    if mode == "start":
        return "…" + text[-keep:]
    if mode == "end":
        return text[:keep] + "…"
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + "…" + text[-tail:]


def field_visible(message: str, cursor: int, inner_w: int,
                  focused: bool) -> Tuple[str, int]:
    """Return (visible_text, cursor_offset_within_visible) for a message
    field of `inner_w` cells. When focused, the window is centered on the
    cursor (clamped at the ends) so the cursor is always visible. When not
    focused, the window simply shows the tail of the message — matching the
    pre-cursor behaviour where typing pushed text toward the right edge."""
    if len(message) <= inner_w:
        return message, cursor
    if not focused:
        return message[-inner_w:], inner_w
    half = inner_w // 2
    start = max(0, min(cursor - half, len(message) - inner_w))
    return message[start:start + inner_w], cursor - start


# ---------- Drawing helpers -------------------------------------------------


def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that swallows errors when writing to the bottom-right corner
    or off-screen after a resize."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    text = text[: max(0, w - x)]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


# ---------- Loading screen --------------------------------------------------


def refresh_all(stdscr, repos: List[Repo], name_max: int,
                name_mode: str = DEFAULT_TRUNCATION_MODE,
                subtrees: Optional[List[SubtreeSpec]] = None,
                header: str = "loading repos") -> None:
    """Refresh every repo in parallel, animating a spinner while it runs."""
    if not repos:
        return
    done = [False] * len(repos)

    def work(i: int) -> None:
        repos[i].refresh()
        done[i] = True

    curses.curs_set(0)
    with ThreadPoolExecutor(max_workers=len(repos)) as ex:
        futures = [ex.submit(work, i) for i in range(len(repos))]
        frame = 0
        while not all(f.done() for f in futures):
            draw_loading(stdscr, repos, done, name_max, name_mode, header,
                         SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
            curses.napms(80)
            frame += 1
        draw_loading(stdscr, repos, done, name_max, name_mode, header, "✓")
        curses.napms(120)
        for f in futures:
            f.result()  # surface thread exceptions if any
    link_siblings(repos, subtrees)


def draw_loading(stdscr, repos: List[Repo], done: List[bool],
                 name_max: int, name_mode: str,
                 header: str, spinner: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    completed = sum(done)
    total = len(repos)

    title = "idlegit"
    summary = f"{spinner}  {header} ({completed}/{total})"

    name_w = max(len(truncate(r.display_name, name_max, name_mode)) for r in repos)
    block_h = 4 + len(repos)
    top = max(1, (h - block_h) // 2)
    cx = w // 2

    safe_addstr(stdscr, top, max(0, cx - len(title) // 2), title,
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    safe_addstr(stdscr, top + 2, max(0, cx - len(summary) // 2),
                summary, curses.color_pair(PAIR_BRANCH))

    list_left = max(0, cx - (name_w + 4) // 2)
    for i, repo in enumerate(repos):
        if done[i]:
            mark, attr = "✓", curses.color_pair(PAIR_OK)
        else:
            mark, attr = "·", curses.A_DIM
        safe_addstr(stdscr, top + 4 + i, list_left,
                    f"  {mark}  {truncate(repo.display_name, name_max, name_mode)}",
                    attr)

    stdscr.refresh()


# ---------- Sidebar ---------------------------------------------------------


def sidebar_geometry(w: int) -> Tuple[int, int]:
    """(sidebar_x, sidebar_w). Width 0 means no sidebar (terminal too narrow)."""
    if w < 100:
        return w, 0
    sidebar_w = max(20, min(40, w - 80))
    return w - sidebar_w, sidebar_w


def draw_sidebar(stdscr, state: State, x: int, w: int) -> None:
    if w <= 0:
        return
    h, _ = stdscr.getmaxyx()
    sb = curses.color_pair(PAIR_SB_FG)

    # Fill the panel with the sidebar bg so it reads as a distinct surface.
    fill = " " * w
    for y in range(h):
        safe_addstr(stdscr, y, x, fill, sb)

    # Header — sat down a couple of rows so it doesn't crowd the main title.
    header_y = 2
    safe_addstr(stdscr, header_y, x + 1, "Tasks",
                curses.color_pair(PAIR_SB_CYAN) | curses.A_BOLD)

    items = state.tasks.snapshot()
    if not items:
        safe_addstr(stdscr, header_y + 2, x + 1, "(no tasks yet)",
                    sb | curses.A_DIM)
        safe_addstr(stdscr, header_y + 3, x + 1, "Ctrl+R to refresh",
                    sb | curses.A_DIM)
        return

    spinner = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]
    y = header_y + 2
    for i, t in enumerate(items):
        if y >= h - 1:
            remaining = len(items) - i
            safe_addstr(stdscr, h - 1, x + 1,
                        f"+{remaining} more (Ctrl+R clears)",
                        sb | curses.A_DIM)
            break
        if t.status == "running":
            icon, color = spinner, PAIR_SB_CYAN
        elif t.status == "ok":
            icon, color = "✓", PAIR_SB_OK
        elif t.status == "fail":
            icon, color = "✗", PAIR_SB_ERR
        else:  # warn
            icon, color = "⚠", PAIR_SB_WARN
        safe_addstr(stdscr, y, x + 1, icon, curses.color_pair(color))
        label_attr = sb if t.status == "running" else (sb | curses.A_DIM)
        safe_addstr(stdscr, y, x + 3, t.label[: max(0, w - 4)], label_attr)
        y += 1
        # Show error / warn detail below, dimmed.
        if t.message and t.status in ("fail", "warn") and y < h - 1:
            detail = t.message[: max(0, w - 6)]
            safe_addstr(stdscr, y, x + 5, detail,
                        curses.color_pair(color) | curses.A_DIM)
            y += 1


# ---------- Main screen -----------------------------------------------------


def draw_main(stdscr, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    visual_rows = len(state.repos) + sum(len(r.children) for r in state.repos)

    sidebar_x, sidebar_w = sidebar_geometry(w)
    main_w = sidebar_x

    if main_w < 80 or h < 9 + visual_rows:
        safe_addstr(stdscr, 0, 0, "terminal too small — resize and try again",
                    curses.color_pair(PAIR_ERR))
        stdscr.refresh()
        return

    safe_addstr(stdscr, 0, 0, "idlegit",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    if state.workspace_name:
        safe_addstr(stdscr, 0, len("idlegit"), " · ", curses.A_DIM)
        safe_addstr(stdscr, 0, len("idlegit") + 3, state.workspace_name,
                    curses.A_BOLD | curses.color_pair(PAIR_BRANCH))

    # Toggles row (selected == 0 or 1)
    toggle_y = 2
    draw_toggle(stdscr, toggle_y, 2, "auto-stage", state.auto_stage,
                state.selected == 0)
    draw_toggle(stdscr, toggle_y, 22, "auto-push", state.auto_push,
                state.selected == 1)

    # Column geometry — both names and branches truncated per state config.
    nm = state.name_display_max
    bm = state.branch_display_max
    nmode = state.name_truncation
    bmode = state.branch_truncation
    name_w = max(len(truncate(r.display_name, nm, nmode)) for r in state.repos) + 2
    branch_w = max(len(f"[{truncate(r.branch, bm, bmode)}]") for r in state.repos) + 2
    marker_w = 3
    field_x = 2 + name_w + branch_w + marker_w
    field_w = max(20, main_w - field_x - 2)

    # Repo rows, with any tracked-repo children rendered indented underneath.
    # `y_for_body` is parallel to selectable_rows()[2:] (i.e., everything
    # below the toggle row) so the cursor logic below can map state.selected
    # straight to a screen y.
    base_y = 4
    y_for_body: List[int] = []
    body_rows = state.selectable_rows()[2:]
    y = base_y
    for i, row in enumerate(body_rows):
        y_for_body.append(y)
        full_idx = i + 2
        focused = (state.selected == full_idx)
        if row[0] == "repo":
            row_cursor = state.field_cursor if focused else 0
            draw_repo_row(stdscr, y, row[1], focused,
                          name_w, branch_w, field_x, field_w,
                          nm, bm, nmode, bmode, row_cursor)
        else:  # child
            row_cursor = state.field_cursor if focused else 0
            draw_child_row(stdscr, y, row[2], focused,
                           name_w, branch_w, field_x, field_w, nm, nmode,
                           row_cursor)
        y += 1

    # Hints + state legend
    hint_y = y + 1
    safe_addstr(stdscr, hint_y, 2,
                "↑/↓ navigate · ←/→/Home/End move cursor · Tab suggests · Enter review",
                curses.A_DIM)
    safe_addstr(stdscr, hint_y + 1, 2,
                "Space toggles · Ctrl+R refresh · Ctrl+S sync subs · Esc clears row / quits",
                curses.A_DIM)
    draw_state_legend(stdscr, hint_y + 2, 2)

    # Sidebar (right side)
    if sidebar_w > 0:
        draw_sidebar(stdscr, state, sidebar_x, sidebar_w)

    # Cursor — only fire on rows with an editable message field. The cursor's
    # on-screen x mirrors field_visible's window so the rendered text and
    # blinking cursor always agree.
    cursor_set = False
    if not state.on_toggle:
        body_idx = state.selected - 2
        if 0 <= body_idx < len(body_rows):
            row = body_rows[body_idx]
            target = None
            if row[0] == "repo":
                target = row[1]
            elif row[0] == "child" and row[2].kind == "submodule":
                target = row[2]
            if target is not None:
                inner_w = field_w - 2
                cur = max(0, min(state.field_cursor, len(target.message)))
                _, cur_in_visible = field_visible(
                    target.message, cur, inner_w, True)
                cur_x = field_x + 1 + cur_in_visible
                cur_y = y_for_body[body_idx]
                try:
                    stdscr.move(cur_y, cur_x)
                    curses.curs_set(1)
                    cursor_set = True
                except curses.error:
                    pass
    if not cursor_set:
        curses.curs_set(0)

    stdscr.refresh()


def draw_state_legend(stdscr, y: int, x: int) -> None:
    items = [
        ("clean", curses.color_pair(PAIR_OK)),
        ("dirty", curses.color_pair(PAIR_DIRTY)),
        ("merging", curses.color_pair(PAIR_ERR)),
        ("ahead", curses.color_pair(PAIR_AHEAD)),
        ("behind", curses.color_pair(PAIR_BEHIND)),
        ("no upstream", curses.A_DIM),
        ("error", curses.color_pair(PAIR_ERR)),
    ]
    cur = x
    for label, attr in items:
        safe_addstr(stdscr, y, cur, "●", attr)
        safe_addstr(stdscr, y, cur + 2, label, curses.A_DIM)
        cur += 2 + len(label) + 2


def draw_toggle(stdscr, y: int, x: int, label: str, value: bool, focused: bool) -> None:
    box = "[x]" if value else "[ ]"
    pair = PAIR_TOGGLE_ON if value else PAIR_TOGGLE_OFF
    attr = curses.color_pair(pair)
    if focused:
        attr |= curses.A_REVERSE
    safe_addstr(stdscr, y, x, f"{box} {label}", attr)


def draw_repo_row(stdscr, y: int, repo: Repo, focused: bool,
                  name_w: int, branch_w: int, field_x: int, field_w: int,
                  name_max: int, branch_max: int,
                  name_mode: str, branch_mode: str,
                  field_cursor: int = 0) -> None:
    name_attr = curses.A_BOLD if focused else 0
    safe_addstr(stdscr, y, 2,
                truncate(repo.display_name, name_max, name_mode).ljust(name_w),
                name_attr)

    branch_str = f"[{truncate(repo.branch, branch_max, branch_mode)}]".ljust(branch_w)
    safe_addstr(stdscr, y, 2 + name_w, branch_str,
                curses.color_pair(PAIR_BRANCH))

    _, state_attr = state_color(repo)
    safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)

    inner_w = field_w - 2
    visible, _ = field_visible(repo.message, field_cursor, inner_w, focused)
    field_text = " " + visible.ljust(inner_w) + " "
    field_attr = curses.A_REVERSE if focused else curses.A_UNDERLINE
    safe_addstr(stdscr, y, field_x, field_text, field_attr)


def draw_child_row(stdscr, y: int, child: ChildRef, focused: bool,
                   name_w: int, branch_w: int, field_x: int, field_w: int,
                   name_max: int, name_mode: str,
                   field_cursor: int = 0) -> None:
    glyph = "↳" if child.kind == "submodule" else "⊕"
    name_attr = curses.A_BOLD if focused else curses.A_DIM
    safe_addstr(stdscr, y, 4,
                f"{glyph} {truncate(child.repo.display_name, name_max, name_mode)}",
                name_attr)
    if child.kind == "submodule":
        dot_color = PAIR_OK if child.in_sync else PAIR_BEHIND
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ",
                    curses.color_pair(dot_color))
        # Editable commit-message field — committing here writes to
        # `child.nested_path`, not the top-level checkout.
        inner_w = field_w - 2
        visible, _ = field_visible(child.message, field_cursor, inner_w, focused)
        field_text = " " + visible.ljust(inner_w) + " "
        field_attr = curses.A_REVERSE if focused else curses.A_UNDERLINE
        safe_addstr(stdscr, y, field_x, field_text, field_attr)
    # Subtrees skip the sync-status dot AND the field — files belong to the
    # parent's working tree, so commits go via the parent row.


# ---------- LFS helpers -----------------------------------------------------


def format_size(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


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
    seen = set()
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


# ---------- Confirm screen --------------------------------------------------


def build_confirm_lines(state: State) -> Tuple[List[Tuple[str, int]], List[LFSCandidate]]:
    lines: List[Tuple[str, int]] = []
    lfs_candidates: List[LFSCandidate] = []
    repos = [r for r in state.repos if r.message.strip()]
    child_targets: List[Tuple[Repo, ChildRef]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind == "submodule" and ref.message.strip():
                child_targets.append((parent, ref))

    total = len(repos) + len(child_targets)
    lines.append((f"{total} target(s) to commit  ·  "
                  f"auto-stage: {'on' if state.auto_stage else 'off'}  ·  "
                  f"auto-push: {'on' if state.auto_push else 'off'}",
                  curses.A_DIM))
    lines.append(("", 0))

    threshold_mb = state.lfs_warn_bytes // (1024 * 1024)

    for repo in repos:
        header = f"{repo.display_name}  [{repo.branch}]"
        lines.append((header, curses.A_BOLD))

        if repo.merging:
            lines.append(("  ⚠ merge / rebase in progress — commit will be skipped",
                          curses.color_pair(PAIR_ERR)))
            lines.append(("    resolve conflicts and finish the operation, then re-run.",
                          curses.A_DIM))
            for cp in repo.conflict_paths:
                lines.append((f"      {cp}", curses.color_pair(PAIR_ERR)))
            lines.append(("", 0))
            continue

        lines.append((f'  message:  "{repo.message.strip()}"', 0))

        if state.auto_stage:
            files = [(s, p) for s, p in repo.staged]
            files += [(s, p) for s, p in repo.unstaged]
            files += [("?", p) for p in repo.untracked]
            stage_label = "stage:    "
        else:
            files = list(repo.staged)
            stage_label = "staged:   "

        if files:
            first = True
            for status, path in files:
                prefix = f"  {stage_label}" if first else "  " + " " * len(stage_label)
                lines.append((f"{prefix}{status}  {path}", curses.A_DIM))
                first = False
        else:
            lines.append(("  ⚠ no changes — will be skipped",
                          curses.color_pair(PAIR_WARN)))

        warnings = find_lfs_warnings(repo, state.auto_stage, state.lfs_warn_bytes)
        if warnings:
            lines.append((f"  ⚠ files ≥{threshold_mb} MB not LFS-tracked — push will fail:",
                          curses.color_pair(PAIR_ERR)))
            for path, size in warnings:
                cand = LFSCandidate(
                    repo=repo, path=path, size_str=size,
                    line_index=len(lines),
                )
                lfs_candidates.append(cand)
                # Placeholder text — render_candidate_line is the source of
                # truth at draw time so the [x]/[ ] checkbox stays live.
                lines.append(("", curses.color_pair(PAIR_ERR)))

        if state.auto_push:
            if repo.upstream:
                lines.append((f"  push:     yes → {repo.upstream}", 0))
            else:
                lines.append((f"  push:     yes (sets upstream → origin/{repo.branch})", 0))
            if repo.siblings:
                names = ", ".join(s[0].display_name for s in repo.siblings)
                lines.append((f"  sync:     {names}", 0))
        else:
            lines.append(("  push:     no", curses.A_DIM))

        lines.append(("", 0))

    # Nested-submodule commits — each in a child checkout, with its own
    # detached-HEAD check at execute time. We don't pre-scan the working
    # tree for stage previews or LFS warnings here (would require a separate
    # `git status` per child); the worker reports per-step in the sidebar.
    for parent, ref in child_targets:
        header = f"↳ {ref.repo.display_name} in {parent.display_name}"
        lines.append((header, curses.A_BOLD))
        lines.append((f'  message:  "{ref.message.strip()}"', 0))
        lines.append((f'  path:     {ref.nested_path}', curses.A_DIM))
        if state.auto_push:
            lines.append(("  push:     yes (from nested checkout)", 0))
            other_targets = [ref.repo.display_name + " (top-level)"]
            for other_parent, other_path in ref.repo.siblings:
                if other_path != ref.nested_path:
                    other_targets.append(
                        f"{ref.repo.display_name} in {other_parent.display_name}")
            if other_targets:
                lines.append((f"  sync:     {', '.join(other_targets)}", 0))
        else:
            lines.append(("  push:     no", curses.A_DIM))
        lines.append(("  ⚠ if the nested checkout is in detached HEAD, the "
                      "commit will be skipped", curses.A_DIM))
        lines.append(("", 0))

    return lines, lfs_candidates


def render_candidate_line(cand: LFSCandidate, focused: bool) -> Tuple[str, int]:
    check = "[x]" if cand.track else "[ ]"
    text = f"      {check}  {cand.path}  ({cand.size_str})"
    base = PAIR_OK if cand.track else PAIR_ERR
    attr = curses.color_pair(base)
    if focused:
        attr |= curses.A_REVERSE
    return text, attr


def draw_confirm(stdscr,
                 lines: List[Tuple[str, int]],
                 candidates: List[LFSCandidate],
                 cursor: int,
                 scroll: int) -> int:
    """Returns max scroll value for clamping."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, 0, 0, "Review",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))

    body_top = 2
    body_h = max(1, h - body_top - 2)
    max_scroll = max(0, len(lines) - body_h)
    scroll = max(0, min(scroll, max_scroll))

    cand_at_line = {c.line_index: i for i, c in enumerate(candidates)}

    for i in range(body_h):
        idx = scroll + i
        if idx >= len(lines):
            break
        if idx in cand_at_line:
            cand_idx = cand_at_line[idx]
            text, attr = render_candidate_line(
                candidates[cand_idx], focused=(cursor == cand_idx))
        else:
            text, attr = lines[idx]
        safe_addstr(stdscr, body_top + i, 0, text, attr)

    if max_scroll > 0:
        safe_addstr(stdscr, h - 2, 0,
                    f"({scroll}/{max_scroll} lines scrolled)", curses.A_DIM)

    if candidates:
        hint = "↑/↓ select file · Space toggles LFS · Enter execute · Esc back"
    else:
        hint = "Enter execute · Esc back · ↑/↓ scroll"
    safe_addstr(stdscr, h - 1, 0, hint, curses.A_DIM)

    curses.curs_set(0)
    stdscr.refresh()
    return max_scroll


def first_line(text: str) -> str:
    if not text:
        return "(no output)"
    for ln in text.strip().splitlines():
        if ln.strip():
            return ln.strip()
    return "(no output)"


# ---------- Key handling ----------------------------------------------------


def _focused_message_holder(state: State):
    """Return the Repo or ChildRef whose message field is currently editable,
    or None for toggle / subtree rows."""
    if state.on_toggle:
        return None
    if state.current_repo is not None:
        return state.current_repo
    cur_child = state.current_child
    if cur_child is not None and cur_child[1].kind == "submodule":
        return cur_child[1]
    return None


def _reset_field_cursor(state: State) -> None:
    """Park the cursor at the end of the focused row's message — runs after
    every selection change so each field starts in a familiar place."""
    holder = _focused_message_holder(state)
    state.field_cursor = len(holder.message) if holder is not None else 0


def handle_main_key(state: State, key: int) -> Optional[str]:
    if key == curses.KEY_RESIZE:
        return None

    if key in (18, curses.KEY_F5):  # Ctrl+R or F5 — refresh state, prune tasks
        return "refresh"
    if key == 19:  # Ctrl+S — fetch + checkout every tracked sibling
        return "sync"

    if key == curses.KEY_UP:
        state.selected = (state.selected - 1) % state.total_rows
        _reset_field_cursor(state)
        return None
    if key == curses.KEY_DOWN:
        state.selected = (state.selected + 1) % state.total_rows
        _reset_field_cursor(state)
        return None

    if key in (10, 13, curses.KEY_ENTER):
        if state.on_toggle:
            if state.selected == 0:
                state.auto_stage = not state.auto_stage
            else:
                state.auto_push = not state.auto_push
            return None
        if state.has_messages:
            return "confirm"
        return None

    target_message_holder = _focused_message_holder(state)

    if key == 27:
        if state.on_toggle:
            return "confirm-quit" if state.has_messages else "quit"
        if target_message_holder is not None and target_message_holder.message:
            target_message_holder.message = ""
            state.field_cursor = 0
            return None
        return "confirm-quit" if state.has_messages else "quit"

    if state.on_toggle:
        if key == ord(" "):
            if state.selected == 0:
                state.auto_stage = not state.auto_stage
            else:
                state.auto_push = not state.auto_push
        return None

    if target_message_holder is None:
        return None  # subtree row or otherwise non-editable

    msg = target_message_holder.message
    cur = max(0, min(state.field_cursor, len(msg)))

    # Cursor movement within the field.
    if key == curses.KEY_LEFT:
        state.field_cursor = max(0, cur - 1)
        return None
    if key == curses.KEY_RIGHT:
        state.field_cursor = min(len(msg), cur + 1)
        return None
    if key == curses.KEY_HOME or key == 1:  # Home or Ctrl+A
        state.field_cursor = 0
        return None
    if key == curses.KEY_END or key == 5:  # End or Ctrl+E
        state.field_cursor = len(msg)
        return None

    # Tab — replace the message with a working-tree-derived suggestion.
    # Top-level repos use the cached status; child rows scan their nested
    # checkout fresh.
    if key == 9:
        if isinstance(target_message_holder, Repo):
            suggested = suggest_commit_message(
                target_message_holder,
                max_added=state.suggest_added,
                max_updated=state.suggest_updated,
                max_deleted=state.suggest_deleted,
                auto_stage=state.auto_stage,
            )
        else:  # ChildRef (kind="submodule" — checked when target was set)
            suggested = suggest_commit_message_at(
                target_message_holder.nested_path,
                max_added=state.suggest_added,
                max_updated=state.suggest_updated,
                max_deleted=state.suggest_deleted,
                auto_stage=state.auto_stage,
            )
        if suggested:
            target_message_holder.message = suggested
            state.field_cursor = len(suggested)
        return None

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            target_message_holder.message = msg[: cur - 1] + msg[cur:]
            state.field_cursor = cur - 1
        return None
    if key == curses.KEY_DC:  # forward delete
        if cur < len(msg):
            target_message_holder.message = msg[:cur] + msg[cur + 1:]
        return None
    if 32 <= key < 127:
        target_message_holder.message = msg[:cur] + chr(key) + msg[cur:]
        state.field_cursor = cur + 1
        return None
    return None


def ensure_cursor_visible(line_index: int, scroll: int, body_h: int) -> int:
    """Return a new scroll value that keeps line_index on-screen."""
    if line_index < scroll:
        return line_index
    if line_index >= scroll + body_h:
        return max(0, line_index - body_h + 1)
    return scroll


def confirm_quit(stdscr, state: State) -> bool:
    """Show a 'Quit and discard N message(s)? [y/N]' prompt at the bottom of
    the main screen. Returns True if the user confirms, False to cancel."""
    draw_main(stdscr, state)
    h, _ = stdscr.getmaxyx()
    n = sum(1 for r in state.repos if r.message.strip())
    plural = "" if n == 1 else "s"
    prompt = f"Quit and discard {n} commit message{plural}? [y/N]"
    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
    except curses.error:
        pass
    safe_addstr(stdscr, h - 1, 2, prompt,
                curses.color_pair(PAIR_WARN) | curses.A_BOLD)
    curses.curs_set(0)
    stdscr.refresh()
    while True:
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return True
        if key == -1:
            continue
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, 10, 13, curses.KEY_ENTER):
            return False


# ---------- Main loop -------------------------------------------------------


def run(stdscr, cfg: Config) -> None:
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)
    stdscr.timeout(100)  # non-blocking getch — drives sidebar animation.

    repos = discover_repos(cfg.workspace)
    if not repos:
        safe_addstr(stdscr, 0, 0,
                    f"no git repos found under {cfg.workspace}",
                    curses.color_pair(PAIR_ERR))
        safe_addstr(stdscr, 2, 0,
                    f"edit {CONFIG_FILE.name} to point at a different root, then re-run.",
                    curses.A_DIM)
        stdscr.refresh()
        stdscr.timeout(-1)
        stdscr.getch()
        return

    refresh_all(stdscr, repos, cfg.name_display_max, cfg.name_truncation,
                cfg.subtrees)

    workspace_name = cfg.workspace.name or str(cfg.workspace)
    state = State(
        repos=repos,
        workspace_name=workspace_name,
        suggest_added=cfg.suggest_added,
        suggest_updated=cfg.suggest_updated,
        suggest_deleted=cfg.suggest_deleted,
        lfs_warn_bytes=cfg.lfs_warn_bytes,
        branch_display_max=cfg.branch_display_max,
        name_display_max=cfg.name_display_max,
        name_truncation=cfg.name_truncation,
        branch_truncation=cfg.branch_truncation,
        subtrees=cfg.subtrees,
        auto_stage=cfg.default_auto_stage,
        auto_push=cfg.default_auto_push,
    )

    while True:
        draw_main(stdscr, state)
        # Advance spinner only when there is something running, so completed
        # icons don't re-render unnecessarily on every tick.
        if state.tasks.has_running():
            state.spinner_frame = (state.spinner_frame + 1) % len(SPINNER_FRAMES)

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            continue  # tick — loop back to redraw and animate

        action = handle_main_key(state, key)
        if action == "quit":
            return
        if action == "confirm-quit":
            if confirm_quit(stdscr, state):
                return
            continue
        if action == "refresh":
            state.tasks.prune_completed()
            refresh_all(stdscr, state.repos, state.name_display_max,
                        state.name_truncation, state.subtrees, "refreshing")
            continue
        if action == "sync":
            kick_off_sync_siblings(state)
            continue
        if action == "confirm":
            stdscr.timeout(-1)  # confirm sub-loop wants blocking input
            try:
                handle_confirm(stdscr, state)
            finally:
                stdscr.timeout(100)


def handle_confirm(stdscr, state: State) -> None:
    """Inner loop for the review screen. Returns when the user confirms or
    backs out; commits run async after Enter, so we just hand off and exit."""
    lines, candidates = build_confirm_lines(state)
    cursor = 0 if candidates else -1
    scroll = 0
    while True:
        if cursor >= 0:
            h, _ = stdscr.getmaxyx()
            body_h = max(1, h - 4)
            scroll = ensure_cursor_visible(
                candidates[cursor].line_index, scroll, body_h)
        max_scroll = draw_confirm(stdscr, lines, candidates, cursor, scroll)
        scroll = min(scroll, max_scroll)
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == curses.KEY_RESIZE:
            continue
        if key == 27:
            return
        if key in (10, 13, curses.KEY_ENTER):
            kick_off_workers(state, candidates)
            return
        if key == ord(" ") and cursor >= 0:
            candidates[cursor].track = not candidates[cursor].track
            continue
        if key == curses.KEY_UP:
            if cursor >= 0:
                cursor = max(0, cursor - 1)
            else:
                scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            if cursor >= 0:
                cursor = min(len(candidates) - 1, cursor + 1)
            else:
                scroll = min(max_scroll, scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - 10)
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + 10)


def main() -> int:
    cfg = load_config()
    if not cfg.workspace.is_dir():
        print(f"error: workspace root {cfg.workspace} is not a directory",
              file=sys.stderr)
        print(f"edit {CONFIG_FILE} to set 'root' to a valid path.",
              file=sys.stderr)
        return 1
    try:
        curses.wrapper(lambda stdscr: run(stdscr, cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
