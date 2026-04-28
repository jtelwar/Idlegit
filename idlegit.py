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
DEFAULT_MAX_VISIBLE_REPO_ROWS = 0  # 0 = use all available height

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
    max_visible_repo_rows: int = DEFAULT_MAX_VISIBLE_REPO_ROWS
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
    max_visible_repo_rows = DEFAULT_MAX_VISIBLE_REPO_ROWS
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
            max_visible_repo_rows = cp.getint(
                "idlegit", "max_visible_repo_rows", fallback=DEFAULT_MAX_VISIBLE_REPO_ROWS)
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
        max_visible_repo_rows=max(0, max_visible_repo_rows),
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
    suggesting: bool = False  # background suggest in flight for this row
    refreshing: bool = False  # inline refresh in flight for this row

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
    dirty: bool = False  # working-tree changes in the nested checkout
    suggesting: bool = False  # background suggest in flight for this row


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
    max_visible_repo_rows: int = DEFAULT_MAX_VISIBLE_REPO_ROWS
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    selected: int = 2  # 0,1 = toggles; 2..N+1 = repos
    body_scroll: int = 0  # how many body rows are scrolled past the top
    auto_stage: bool = True
    auto_push: bool = True
    tasks: Tasks = field(default_factory=Tasks)
    spinner_frame: int = 0
    # Cursor position within the focused row's message field. Reset to the
    # end of the message whenever the focused row changes (see _reset_field_cursor).
    field_cursor: int = 0
    # Modal stack — only the topmost modal handles input. Esc closes the
    # topmost; closing the action menu returns to the main view.
    action_menu: Optional["ActionMenu"] = None
    branch_picker: Optional["BranchPicker"] = None
    reset_prompt: Optional["ResetPrompt"] = None
    global_menu: Optional["GlobalMenu"] = None

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


@dataclass
class ActionMenuItem:
    """One row in the Tab-context-menu. `enabled=False` greys it out and
    Enter is a no-op; `reason` (if any) appears next to the label."""
    id: str
    label: str
    enabled: bool = True
    reason: str = ""


@dataclass
class ActionMenu:
    """Modal opened with Tab on a repo / submodule child row. Carries the
    pre-computed metadata (branch, upstream, ahead/behind…) and recent
    commits so we don't have to query git on every redraw."""
    target_label: str          # e.g. "Upskill.Health.API" / "↳ Domain.Models in API"
    target_path: Path          # where git commands run for this menu
    # Reference back to whatever owns the row so post-action refresh knows
    # what to rescan. Exactly one of these is set.
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    # Pre-computed metadata (frozen at open time).
    branch: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    state_label: str = "clean"
    state_pair: int = 0
    items: List[ActionMenuItem] = field(default_factory=list)
    selected: int = 0
    commits: List[str] = field(default_factory=list)


@dataclass
class BranchPicker:
    """Sub-modal triggered from the action menu's "switch branch" item."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    branches: List[str] = field(default_factory=list)
    current: str = ""
    selected: int = 0
    scroll: int = 0


@dataclass
class ResetPrompt:
    """Sub-modal triggered from the action menu's "soft reset" item.
    `count` is the user-typed number; 0 means "wipe all unpushed (reset to
    @{u})"."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    ahead: int = 0
    count: int = 0
    typed: str = ""  # raw input buffer; empty means count=0


@dataclass
class GlobalMenu:
    """Modal opened with Shift+Tab — workspace-wide actions that aren't
    tied to a single row."""
    items: List[ActionMenuItem] = field(default_factory=list)
    selected: int = 0


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
    script lives in is included if (and only if) it's also a git repo —
    handy for managing idlegit's own checkout from idlegit itself."""
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
    submodule_refs: List[ChildRef] = []
    for parent in repos:
        if parent.rel == ".":
            continue
        for url, sub_path in parent.nested_subs:
            target = url_to_repo.get(url)
            if target is None or target is parent:
                continue
            target.siblings.append((parent, sub_path))
            ref = ChildRef(repo=target, nested_path=sub_path, kind="submodule")
            parent.children.append(ref)
            submodule_refs.append(ref)

    # Populate per-child state (HEAD + dirty flag) in parallel — one git
    # status + one rev-parse per child, capped at the number of children.
    def _populate(ref: ChildRef) -> None:
        rc, out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
        if rc == 0:
            ref.head = out.strip()
            ref.in_sync = bool(ref.repo.head) and ref.head == ref.repo.head
        else:
            ref.in_sync = False
        rc, out, _ = git(ref.nested_path, ["status", "--porcelain=v1"])
        ref.dirty = rc == 0 and bool(out.strip())

    if submodule_refs:
        with ThreadPoolExecutor(max_workers=len(submodule_refs)) as ex:
            list(ex.map(_populate, submodule_refs))

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


# ---------- Target-state query (for the action menu) -----------------------


@dataclass
class TargetState:
    """Snapshot of a working tree's state, queried fresh when the action
    menu opens — covers both top-level repos and nested submodule paths."""
    branch: str
    upstream: Optional[str]
    ahead: int
    behind: int
    has_origin: bool
    merging: bool
    dirty: bool
    recent_commits: List[str]


def query_target_state(path: Path, max_commits: int = 5) -> TargetState:
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
    have_local = set()
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

    # Filter out remote-only entries that already have a local
    remote_only = [b for b in remote_only if b not in have_local]
    return locals_seen + remote_only, current


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


def kick_off_inline_refresh(state: State) -> None:
    """Re-discover repos in the workspace, removing gone entries and adding
    new ones inline, and refresh every kept/new repo in parallel — each
    one toggling its `refreshing` flag so the row spinner animates next to
    its name. The main view stays visible the whole time; no overlay."""
    workspace = (TOOL_DIR / DEFAULT_ROOT).resolve()
    # If the user pointed root somewhere else via config, the existing repos
    # all share a common parent — use any one as the workspace anchor.
    if state.repos:
        workspace = state.repos[0].path.parent
        if state.repos[0].rel == ".":
            workspace = state.repos[0].path

    def worker() -> None:
        # Re-discover to pick up newly-added / removed folders.
        try:
            fresh = discover_repos(workspace)
        except Exception:
            fresh = []
        fresh_by_rel = {r.rel: r for r in fresh}
        kept_rels = {r.rel for r in state.repos if r.rel in fresh_by_rel}

        # Remove repos that vanished from disk.
        state.repos[:] = [r for r in state.repos if r.rel in kept_rels]

        # Insert any newly-discovered repos in the original sort order.
        existing_rels = {r.rel for r in state.repos}
        for r in fresh:
            if r.rel not in existing_rels:
                state.repos.append(r)
        state.repos.sort(
            key=lambda r: (r.rel != ".", r.rel.lower() if r.rel != "." else ""))

        # Mark every repo as refreshing, then run refreshes in parallel.
        for r in state.repos:
            r.refreshing = True

        def refresh_one(r: Repo) -> None:
            try:
                r.refresh()
            finally:
                r.refreshing = False

        if state.repos:
            with ThreadPoolExecutor(max_workers=len(state.repos)) as ex:
                list(ex.map(refresh_one, state.repos))
        link_siblings(state.repos, state.subtrees)

        # Clamp selection in case we trimmed/grew the row list.
        state.selected = max(0, min(state.selected, max(0, state.total_rows - 1)))
        state.body_scroll = max(
            0, min(state.body_scroll, max(0, state.total_rows - 1)))

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


# ---------- Action menu helpers -------------------------------------------


def _refresh_target_state(state: State,
                          target_repo: Optional[Repo],
                          target_parent: Optional[Repo]) -> None:
    """Re-fetch state for just one row's repo (the user's spec — don't
    refresh-all). For top-level rows we refresh the Repo itself; for
    submodule child rows we refresh the parent (its dirty state changes
    when the nested checkout moves) and then re-link siblings so the
    child's HEAD/in_sync/dirty fields catch up."""
    if target_repo is not None:
        target_repo.refresh()
    if target_parent is not None:
        target_parent.refresh()
    link_siblings(state.repos, state.subtrees)


def kick_off_action(state: State, action_id: str, *,
                    target_label: str, target_path: Path,
                    target_repo: Optional[Repo],
                    target_parent: Optional[Repo],
                    branch_arg: str = "",
                    reset_count: int = 0) -> None:
    """Spawn a daemon worker that runs one git action against `target_path`,
    publishes its progress to the sidebar, and quietly re-queries that one
    repo's state when it finishes. Returns immediately so the UI is free."""

    def worker() -> None:
        if action_id == "fetch":
            t = state.tasks.add(f"{target_label}: fetch")
            rc, _, err = git(target_path, ["fetch", "--all"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "pull":
            t = state.tasks.add(f"{target_label}: pull")
            rc, _, err = git(target_path, ["pull"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "push":
            t = state.tasks.add(f"{target_label}: push")
            rc_b, b_out, _ = git(target_path, ["branch", "--show-current"])
            cur_branch = b_out.strip() if rc_b == 0 else ""
            rc_u, u_out, _ = git(target_path, [
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            has_upstream = rc_u == 0 and bool(u_out.strip())
            if has_upstream:
                rc, _, err = git(target_path, ["push"])
            elif cur_branch:
                rc, _, err = git(target_path, [
                    "push", "--set-upstream", "origin", cur_branch])
            else:
                rc, err = 1, "no current branch"
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "soft_reset":
            if reset_count <= 0:
                t = state.tasks.add(
                    f"{target_label}: soft reset all unpushed (to @{{u}})")
                rc, _, err = git(target_path, ["reset", "--soft", "@{u}"])
            else:
                t = state.tasks.add(
                    f"{target_label}: soft reset HEAD~{reset_count}")
                rc, _, err = git(target_path, [
                    "reset", "--soft", f"HEAD~{reset_count}"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "switch_branch":
            t = state.tasks.add(f"{target_label}: checkout {branch_arg}")
            rc, _, err = git(target_path, ["checkout", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        else:
            return  # unknown action — nothing to do

        _refresh_target_state(state, target_repo, target_parent)

    threading.Thread(target=worker, daemon=True).start()


def open_action_menu(state: State) -> None:
    """Build and install the ActionMenu modal for the focused row."""
    if state.on_toggle:
        return
    cur_repo = state.current_repo
    cur_child = state.current_child

    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    if cur_repo is not None:
        label = cur_repo.display_name
        target_path = cur_repo.path
        target_repo = cur_repo
    elif cur_child is not None and cur_child[1].kind == "submodule":
        parent, child = cur_child
        label = f"↳ {child.repo.display_name} in {parent.display_name}"
        target_path = child.nested_path
        target_parent = parent
        target_child = child
    else:
        return  # subtree row or otherwise unsupported

    ts = query_target_state(target_path)

    items = [
        ActionMenuItem(
            id="fetch", label="fetch (all branches)",
            enabled=ts.has_origin,
            reason="" if ts.has_origin else "no origin"),
        ActionMenuItem(
            id="pull", label="pull",
            enabled=ts.has_origin and ts.upstream is not None and not ts.merging,
            reason=("merging" if ts.merging
                    else ("no upstream" if ts.upstream is None
                          else ("" if ts.has_origin else "no origin")))),
        ActionMenuItem(
            id="switch_branch", label="switch branch…",
            enabled=not ts.merging,
            reason="" if not ts.merging else "merging"),
        ActionMenuItem(
            id="soft_reset",
            label=f"soft reset ({ts.ahead} unpushed)…",
            enabled=ts.ahead > 0,
            reason="" if ts.ahead > 0 else "no unpushed commits"),
        ActionMenuItem(
            id="push", label="push",
            enabled=ts.has_origin,
            reason="" if ts.has_origin else "no origin"),
    ]

    # Pick the first enabled item as the initial selection.
    initial = 0
    for i, it in enumerate(items):
        if it.enabled:
            initial = i
            break

    # State badge for the metadata header.
    if ts.merging:
        state_label, state_pair = "merging", PAIR_ERR
    elif ts.ahead > 0 and ts.behind > 0:
        state_label, state_pair = "diverged", PAIR_ERR
    elif ts.dirty:
        state_label, state_pair = "dirty", PAIR_DIRTY
    elif ts.behind > 0:
        state_label, state_pair = "behind", PAIR_BEHIND
    elif ts.ahead > 0:
        state_label, state_pair = "ahead", PAIR_AHEAD
    elif ts.upstream is None:
        state_label, state_pair = "no upstream", 0  # 0 = no pair → A_DIM only
    else:
        state_label, state_pair = "clean", PAIR_OK

    state.action_menu = ActionMenu(
        target_label=label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        branch=ts.branch,
        upstream=ts.upstream,
        ahead=ts.ahead,
        behind=ts.behind,
        state_label=state_label,
        state_pair=state_pair,
        items=items,
        selected=initial,
        commits=ts.recent_commits,
    )


def open_branch_picker(state: State) -> None:
    """Open the branch picker submodal off the active action menu."""
    menu = state.action_menu
    if menu is None:
        return
    branches, current = list_branches(menu.target_path)
    initial = 0
    for i, b in enumerate(branches):
        if b == current:
            initial = i
            break
    state.branch_picker = BranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        branches=branches,
        current=current,
        selected=initial,
    )


def open_reset_prompt(state: State) -> None:
    """Open the soft-reset count prompt off the active action menu."""
    menu = state.action_menu
    if menu is None:
        return
    state.reset_prompt = ResetPrompt(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        ahead=menu.ahead,
    )


def open_global_menu(state: State) -> None:
    """Open the workspace-wide modal (Shift+Tab)."""
    has_dirty_empty = False
    for repo in state.repos:
        if repo.is_dirty and not repo.message.strip():
            has_dirty_empty = True
            break
    if not has_dirty_empty:
        for parent in state.repos:
            for child in parent.children:
                if (child.kind == "submodule" and child.dirty
                        and not child.message.strip()):
                    has_dirty_empty = True
                    break
            if has_dirty_empty:
                break
    items = [
        ActionMenuItem(
            id="suggest_all", label="Suggest all empty commit messages",
            enabled=has_dirty_empty,
            reason="" if has_dirty_empty else "no dirty rows with empty message"),
        ActionMenuItem(
            id="refresh_all", label="Refresh all repos", enabled=True),
    ]
    state.global_menu = GlobalMenu(items=items, selected=0)


# ---------- Background suggest workers ------------------------------------


def _suggest_into_repo(state: State, repo: Repo) -> None:
    repo.suggesting = True
    try:
        result = suggest_commit_message(
            repo,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result:
            repo.message = result
    finally:
        repo.suggesting = False


def _suggest_into_child(state: State, child: ChildRef) -> None:
    child.suggesting = True
    try:
        result = suggest_commit_message_at(
            child.nested_path,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result:
            child.message = result
    finally:
        child.suggesting = False


def kick_off_suggest_for(state: State, target) -> None:
    """Run a single suggestion in a background thread; UI shows a spinner
    in the field meanwhile via target.suggesting."""
    if isinstance(target, Repo):
        threading.Thread(
            target=_suggest_into_repo, args=(state, target), daemon=True).start()
    else:  # ChildRef
        threading.Thread(
            target=_suggest_into_child, args=(state, target), daemon=True).start()


def kick_off_bulk_suggest(state: State) -> None:
    """For every dirty row with an empty message, kick off a background
    suggestion. Each row animates independently."""
    for repo in state.repos:
        if repo.is_dirty and not repo.message.strip() and not repo.suggesting:
            kick_off_suggest_for(state, repo)
    for parent in state.repos:
        for child in parent.children:
            if (child.kind == "submodule" and child.dirty
                    and not child.message.strip() and not child.suggesting):
                kick_off_suggest_for(state, child)


# ---------- Modal key handlers --------------------------------------------


def handle_action_menu_key(state: State, key: int) -> None:
    menu = state.action_menu
    if menu is None:
        return
    if key == 27:
        state.action_menu = None
        return
    if key == curses.KEY_UP and menu.items:
        menu.selected = (menu.selected - 1) % len(menu.items)
        return
    if key == curses.KEY_DOWN and menu.items:
        menu.selected = (menu.selected + 1) % len(menu.items)
        return
    if key in (10, 13, curses.KEY_ENTER) and menu.items:
        item = menu.items[menu.selected]
        if not item.enabled:
            return
        if item.id == "switch_branch":
            open_branch_picker(state)
            return
        if item.id == "soft_reset":
            open_reset_prompt(state)
            return
        kick_off_action(
            state, item.id,
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
        )
        state.action_menu = None


def handle_branch_picker_key(state: State, key: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    if key == 27:
        state.branch_picker = None
        return
    if not picker.branches:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        branch = picker.branches[picker.selected]
        kick_off_action(
            state, "switch_branch",
            target_label=picker.target_label,
            target_path=picker.target_path,
            target_repo=picker.target_repo,
            target_parent=picker.target_parent,
            branch_arg=branch,
        )
        state.branch_picker = None
        state.action_menu = None


def handle_reset_prompt_key(state: State, key: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    if key == 27:
        state.reset_prompt = None
        return
    if key in (10, 13, curses.KEY_ENTER):
        try:
            n = int(prompt.typed) if prompt.typed else 0
        except ValueError:
            n = 0
        kick_off_action(
            state, "soft_reset",
            target_label=prompt.target_label,
            target_path=prompt.target_path,
            target_repo=prompt.target_repo,
            target_parent=prompt.target_parent,
            reset_count=max(0, n),
        )
        state.reset_prompt = None
        state.action_menu = None
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        prompt.typed = prompt.typed[:-1]
        return
    if 48 <= key <= 57:  # 0–9
        prompt.typed += chr(key)


def handle_global_menu_key(state: State, key: int) -> Optional[str]:
    menu = state.global_menu
    if menu is None:
        return None
    if key == 27:
        state.global_menu = None
        return None
    if key == curses.KEY_UP and menu.items:
        menu.selected = (menu.selected - 1) % len(menu.items)
        return None
    if key == curses.KEY_DOWN and menu.items:
        menu.selected = (menu.selected + 1) % len(menu.items)
        return None
    if key in (10, 13, curses.KEY_ENTER) and menu.items:
        item = menu.items[menu.selected]
        if not item.enabled:
            return None
        if item.id == "suggest_all":
            kick_off_bulk_suggest(state)
            state.global_menu = None
            return None
        if item.id == "refresh_all":
            state.global_menu = None
            return "refresh"
    return None


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


def _body_height_for(state: State, h: int) -> int:
    """Height (in rows) available for the repo body. Reserves space for the
    title (1), toggles row (1) + blank (1), one blank line before hints,
    two hint lines, and the state legend (1) — 7 rows of chrome total.
    Capped by `state.max_visible_repo_rows` if > 0; floored at 1 so at least one
    repo row is always visible."""
    chrome = 7
    avail = h - chrome
    if avail < 1:
        return 1
    if state.max_visible_repo_rows > 0:
        avail = min(avail, state.max_visible_repo_rows)
    return max(1, avail)


def _ensure_focused_visible(state: State, body_h: int, total_body: int) -> None:
    """Adjust state.body_scroll so the focused body row is on-screen."""
    if state.on_toggle:
        return
    body_idx = state.selected - 2
    if body_idx < 0 or body_idx >= total_body:
        return
    if body_idx < state.body_scroll:
        state.body_scroll = body_idx
    elif body_idx >= state.body_scroll + body_h:
        state.body_scroll = body_idx - body_h + 1
    state.body_scroll = max(0, min(state.body_scroll, max(0, total_body - body_h)))


def draw_main(stdscr, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    sidebar_x, sidebar_w = sidebar_geometry(w)
    main_w = sidebar_x

    body_h = _body_height_for(state, h)
    if main_w < 80 or h < 8:
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

    # Body rows: every selectable row below the toggles. Scroll so the
    # focused row stays visible inside the [body_scroll, body_scroll+body_h)
    # window before rendering.
    base_y = 4
    body_rows = state.selectable_rows()[2:]
    _ensure_focused_visible(state, body_h, len(body_rows))
    visible_start = state.body_scroll
    visible_end = min(len(body_rows), visible_start + body_h)

    spinner_char = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]
    y_for_body: Dict[int, int] = {}
    for screen_i, body_idx in enumerate(range(visible_start, visible_end)):
        row = body_rows[body_idx]
        y = base_y + screen_i
        y_for_body[body_idx] = y
        full_idx = body_idx + 2
        focused = (state.selected == full_idx)
        if row[0] == "repo":
            row_cursor = state.field_cursor if focused else 0
            draw_repo_row(stdscr, y, row[1], focused,
                          name_w, branch_w, field_x, field_w,
                          nm, bm, nmode, bmode, row_cursor, spinner_char)
        else:  # child
            row_cursor = state.field_cursor if focused else 0
            draw_child_row(stdscr, y, row[2], focused,
                           name_w, branch_w, field_x, field_w, nm, nmode,
                           row_cursor, spinner_char)

    # Subtle scroll markers when content is clipped above / below.
    if visible_start > 0:
        safe_addstr(stdscr, base_y - 1, 2,
                    f"↑ {visible_start} more above", curses.A_DIM)
    if visible_end < len(body_rows):
        below = len(body_rows) - visible_end
        safe_addstr(stdscr, base_y + body_h, 2,
                    f"↓ {below} more below", curses.A_DIM)

    # Hints + state legend (anchored to the bottom of the available height
    # so a shrunken terminal still shows them).
    hint_y = base_y + body_h + 1
    safe_addstr(stdscr, hint_y, 2,
                "↑/↓ navigate · Tab menu · Shift+Tab workspace · Left/Shift+Left suggest · Enter review",
                curses.A_DIM)
    safe_addstr(stdscr, hint_y + 1, 2,
                "Space toggles · Ctrl+R refresh · Ctrl+S sync · Esc clears / back / quits",
                curses.A_DIM)
    draw_state_legend(stdscr, hint_y + 2, 2)

    # Sidebar (right side)
    if sidebar_w > 0:
        draw_sidebar(stdscr, state, sidebar_x, sidebar_w)

    # Modals overlay the main panel. Draw the deepest active modal last so
    # it ends up on top. The sidebar is left untouched.
    modal_active = (state.action_menu is not None
                    or state.global_menu is not None
                    or state.branch_picker is not None
                    or state.reset_prompt is not None)
    if state.action_menu is not None:
        draw_action_menu(stdscr, state, sidebar_x)
    if state.global_menu is not None:
        draw_global_menu(stdscr, state, sidebar_x)
    if state.branch_picker is not None:
        draw_branch_picker(stdscr, state, sidebar_x)
    if state.reset_prompt is not None:
        draw_reset_prompt(stdscr, state, sidebar_x)

    # Cursor — only fire on rows with an editable message field that's
    # currently visible in the body window. Skipped when a modal is open
    # (the modal owns its own cursor / no cursor).
    cursor_set = False
    if not modal_active and not state.on_toggle:
        body_idx = state.selected - 2
        if 0 <= body_idx < len(body_rows) and body_idx in y_for_body:
            row = body_rows[body_idx]
            target = None
            if row[0] == "repo":
                target = row[1] if (row[1].is_dirty or row[1].message) else None
            elif row[0] == "child" and row[2].kind == "submodule":
                target = row[2] if (row[2].dirty or row[2].message) else None
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
                  field_cursor: int = 0,
                  spinner_char: str = " ") -> None:
    name_attr = curses.A_BOLD if focused else 0
    safe_addstr(stdscr, y, 2,
                truncate(repo.display_name, name_max, name_mode).ljust(name_w),
                name_attr)

    branch_str = f"[{truncate(repo.branch, branch_max, branch_mode)}]".ljust(branch_w)
    safe_addstr(stdscr, y, 2 + name_w, branch_str,
                curses.color_pair(PAIR_BRANCH))

    if repo.refreshing:
        safe_addstr(stdscr, y, 2 + name_w + branch_w,
                    f" {spinner_char} ", curses.color_pair(PAIR_BRANCH))
    else:
        _, state_attr = state_color(repo)
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)

    # Field rendering: live message field if there's something to commit;
    # spinner placeholder while a background suggest is in flight; otherwise
    # nothing — the row stays clean and Tab opens the action menu.
    if repo.suggesting and not repo.message:
        inner_w = field_w - 2
        text = (f" {spinner_char} generating…").ljust(inner_w + 2)
        safe_addstr(stdscr, y, field_x, text,
                    curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
    elif repo.is_dirty or repo.message:
        inner_w = field_w - 2
        visible, _ = field_visible(repo.message, field_cursor, inner_w, focused)
        field_text = " " + visible.ljust(inner_w) + " "
        field_attr = curses.A_REVERSE if focused else curses.A_UNDERLINE
        safe_addstr(stdscr, y, field_x, field_text, field_attr)


def draw_child_row(stdscr, y: int, child: ChildRef, focused: bool,
                   name_w: int, branch_w: int, field_x: int, field_w: int,
                   name_max: int, name_mode: str,
                   field_cursor: int = 0,
                   spinner_char: str = " ") -> None:
    glyph = "↳" if child.kind == "submodule" else "⊕"
    name_attr = curses.A_BOLD if focused else curses.A_DIM
    safe_addstr(stdscr, y, 4,
                f"{glyph} {truncate(child.repo.display_name, name_max, name_mode)}",
                name_attr)
    if child.kind == "submodule":
        dot_color = PAIR_OK if child.in_sync else PAIR_BEHIND
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ",
                    curses.color_pair(dot_color))
        # Field: spinner while a background suggest is in flight, the
        # editable message field when there's something to commit, nothing
        # otherwise.
        if child.suggesting and not child.message:
            inner_w = field_w - 2
            text = (f" {spinner_char} generating…").ljust(inner_w + 2)
            safe_addstr(stdscr, y, field_x, text,
                        curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
        elif child.dirty or child.message:
            inner_w = field_w - 2
            visible, _ = field_visible(
                child.message, field_cursor, inner_w, focused)
            field_text = " " + visible.ljust(inner_w) + " "
            field_attr = curses.A_REVERSE if focused else curses.A_UNDERLINE
            safe_addstr(stdscr, y, field_x, field_text, field_attr)
    # Subtrees skip the sync-status dot AND the field — files belong to the
    # parent's working tree, so commits go via the parent row.


# ---------- Modal drawing --------------------------------------------------


def _modal_geometry(stdscr, sidebar_x: int, content_w: int,
                    content_h: int) -> Tuple[int, int, int, int]:
    """Return (x, y, w, h) for a centered modal box that fits within the
    main panel (left of the sidebar) and leaves the sidebar visible."""
    h, w = stdscr.getmaxyx()
    main_w = sidebar_x if sidebar_x > 0 else w
    box_w = min(content_w, max(40, main_w - 2))
    box_h = min(content_h, max(8, h - 2))
    x = max(1, (main_w - box_w) // 2)
    y = max(1, (h - box_h) // 2)
    return x, y, box_w, box_h


def _draw_modal_fill(stdscr, x: int, y: int, w: int, h: int, sb: int) -> None:
    fill = " " * w
    for row in range(y, y + h):
        safe_addstr(stdscr, row, x, fill, sb)


def draw_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.action_menu
    if menu is None:
        return
    n_items = len(menu.items)
    n_commits = max(1, min(5, len(menu.commits)))
    # title + blank + branch + metadata + sep + items + sep + commits-header
    # + commits + blank + hint
    content_h = 1 + 1 + 1 + 1 + 1 + n_items + 1 + 1 + n_commits + 1 + 1
    x, y, w, h = _modal_geometry(stdscr, sidebar_x, 70, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    _draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x, menu.target_label[: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    # Metadata: branch + state badge, then upstream/ahead/behind line.
    line = y + 2
    branch_str = f"[{menu.branch}]"
    safe_addstr(stdscr, line, inner_x, branch_str,
                curses.color_pair(PAIR_BRANCH))
    state_attr = (curses.color_pair(menu.state_pair)
                  if menu.state_pair else (sb | curses.A_DIM))
    safe_addstr(stdscr, line, inner_x + len(branch_str) + 1,
                f"● {menu.state_label}", state_attr)

    line += 1
    if menu.upstream:
        meta = f"upstream: {menu.upstream}  ·  ahead {menu.ahead} / behind {menu.behind}"
    else:
        meta = "no upstream"
    safe_addstr(stdscr, line, inner_x, meta[: w - 4], sb | curses.A_DIM)

    line += 1
    safe_addstr(stdscr, line, inner_x, "─" * (w - 4), sb | curses.A_DIM)

    # Action items.
    line += 1
    for i, item in enumerate(menu.items):
        focused = (i == menu.selected)
        prefix = "→ " if focused else "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        if focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        text = (prefix + label).ljust(w - 4)
        safe_addstr(stdscr, line, inner_x, text, attr)
        line += 1

    # Separator + recent commits.
    safe_addstr(stdscr, line, inner_x, "─" * (w - 4), sb | curses.A_DIM)
    line += 1
    safe_addstr(stdscr, line, inner_x, "Recent commits",
                sb | curses.A_BOLD)
    line += 1
    if menu.commits:
        for commit_line in menu.commits[:5]:
            safe_addstr(stdscr, line, inner_x + 2,
                        commit_line[: w - 6], sb | curses.A_DIM)
            line += 1
    else:
        safe_addstr(stdscr, line, inner_x + 2,
                    "(no commits on this branch yet)",
                    sb | curses.A_DIM)
        line += 1

    # Hints at the bottom row of the box.
    safe_addstr(stdscr, y + h - 1, inner_x,
                "↑/↓ select · Enter run · Esc back",
                sb | curses.A_DIM)


def draw_global_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.global_menu
    if menu is None:
        return
    n_items = len(menu.items)
    content_h = 1 + 1 + n_items + 1 + 1
    x, y, w, h = _modal_geometry(stdscr, sidebar_x, 50, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    _draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x, "Workspace actions",
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    line = y + 2
    for i, item in enumerate(menu.items):
        focused = (i == menu.selected)
        prefix = "→ " if focused else "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        if focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        safe_addstr(stdscr, line, inner_x, (prefix + label).ljust(w - 4), attr)
        line += 1

    safe_addstr(stdscr, y + h - 1, inner_x,
                "↑/↓ select · Enter run · Esc back",
                sb | curses.A_DIM)


def draw_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    body_h = max(3, min(15, len(picker.branches)))
    content_h = 1 + 1 + body_h + 1 + 1
    x, y, w, h = _modal_geometry(stdscr, sidebar_x, 50, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    _draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x,
                f"Switch branch — {picker.target_label}"[: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    if not picker.branches:
        safe_addstr(stdscr, y + 2, inner_x,
                    "(no branches found)", sb | curses.A_DIM)
        safe_addstr(stdscr, y + h - 1, inner_x,
                    "Esc back", sb | curses.A_DIM)
        return

    # Clamp scroll so the selected entry is visible.
    if picker.selected < picker.scroll:
        picker.scroll = picker.selected
    elif picker.selected >= picker.scroll + body_h:
        picker.scroll = picker.selected - body_h + 1
    picker.scroll = max(0, min(picker.scroll,
                               max(0, len(picker.branches) - body_h)))

    for i in range(body_h):
        idx = picker.scroll + i
        if idx >= len(picker.branches):
            break
        name = picker.branches[idx]
        focused = (idx == picker.selected)
        is_current = (name == picker.current)
        marker = "* " if is_current else "  "
        prefix = "→ " if focused else marker
        text = (prefix + name).ljust(w - 4)
        attr = sb | curses.A_REVERSE if focused else sb
        if is_current and not focused:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, y + 2 + i, inner_x, text, attr)

    if picker.scroll > 0:
        safe_addstr(stdscr, y + 1, inner_x,
                    f"↑ {picker.scroll} more above", sb | curses.A_DIM)
    if picker.scroll + body_h < len(picker.branches):
        below = len(picker.branches) - (picker.scroll + body_h)
        safe_addstr(stdscr, y + 2 + body_h, inner_x,
                    f"↓ {below} more below", sb | curses.A_DIM)

    safe_addstr(stdscr, y + h - 1, inner_x,
                "↑/↓ select · Enter checkout · Esc back",
                sb | curses.A_DIM)


def draw_reset_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    content_h = 7
    x, y, w, h = _modal_geometry(stdscr, sidebar_x, 56, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    _draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x,
                f"Soft reset — {prompt.target_label}"[: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    safe_addstr(stdscr, y + 2, inner_x,
                f"unpushed commits on this branch: {prompt.ahead}",
                sb | curses.A_DIM)
    safe_addstr(stdscr, y + 3, inner_x,
                "Number to reset:  (Enter on 0 wipes ALL unpushed)",
                sb | curses.A_DIM)

    visible = prompt.typed if prompt.typed else "0"
    field_text = f" {visible} "
    safe_addstr(stdscr, y + 4, inner_x, field_text.ljust(w - 4),
                sb | curses.A_REVERSE)

    safe_addstr(stdscr, y + h - 1, inner_x,
                "type a number · Enter run · Esc back",
                sb | curses.A_DIM)


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

    # Tab opens the per-row action menu; Shift+Tab opens the workspace-wide
    # menu. Both work regardless of whether the focused row has a field.
    if key == 9:  # Tab
        open_action_menu(state)
        return None
    if key == curses.KEY_BTAB:  # Shift+Tab
        open_global_menu(state)
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

    # Left on an empty row generates a single suggestion (cursor-left has
    # nothing to do anyway). Shift+Left does the same for every dirty row
    # with an empty message.
    if key == curses.KEY_LEFT:
        if not msg:
            kick_off_suggest_for(state, target_message_holder)
            return None
        state.field_cursor = max(0, cur - 1)
        return None
    if key == curses.KEY_SLEFT and not msg:
        kick_off_bulk_suggest(state)
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
        max_visible_repo_rows=cfg.max_visible_repo_rows,
        subtrees=cfg.subtrees,
        auto_stage=cfg.default_auto_stage,
        auto_push=cfg.default_auto_push,
    )

    while True:
        draw_main(stdscr, state)
        # Advance spinner whenever any background work is in flight so
        # animations (sidebar tasks, in-field suggest, in-row refresh) tick.
        anim_running = (state.tasks.has_running()
                        or any(r.suggesting or r.refreshing for r in state.repos)
                        or any(c.suggesting
                               for r in state.repos for c in r.children))
        if anim_running:
            state.spinner_frame = (state.spinner_frame + 1) % len(SPINNER_FRAMES)

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == -1:
            continue  # tick — loop back to redraw and animate

        # Modal dispatch (deepest first). Each modal owns its key handling
        # and may close itself by clearing its slot on state.
        if state.reset_prompt is not None:
            handle_reset_prompt_key(state, key)
            continue
        if state.branch_picker is not None:
            handle_branch_picker_key(state, key)
            continue
        if state.action_menu is not None:
            handle_action_menu_key(state, key)
            continue
        if state.global_menu is not None:
            res = handle_global_menu_key(state, key)
            if res == "refresh":
                kick_off_inline_refresh(state)
            continue

        action = handle_main_key(state, key)
        if action == "quit":
            return
        if action == "confirm-quit":
            if confirm_quit(stdscr, state):
                return
            continue
        if action == "refresh":
            state.tasks.prune_completed()
            kick_off_inline_refresh(state)
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
