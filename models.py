"""Pure data structures shared across idlegit modules. No git or curses
imports here — anything in this file should be safe to import from
config.py, git_ops.py, workers.py, and ui.py without cycles."""
from __future__ import annotations

import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


def _monotonic() -> float:
    """Indirected so tests can stub it if needed."""
    return time.monotonic()


# ---------- Repo + child reference ----------------------------------------


@dataclass
class WorkflowInfo:
    """A GitHub Actions workflow advertised by `gh workflow list`. Cached on
    Repo so the review screen and the action menu can list them without
    re-hitting the API on every redraw.

    `triggers_push` / `push_branches` / `push_branches_ignore` are derived
    from the YAML's `on:` block so we can predict whether the workflow
    will fire on a push to the repo's current branch.

    `state` mirrors GitHub's workflow state (`active`, `disabled_manually`,
    `disabled_inactivity`, `disabled_fork`). Empty string until we've
    successfully merged `gh workflow list` output into this entry."""
    name: str
    path: str           # e.g. ".github/workflows/build.yml"
    state: str = ""     # "active" / "disabled_*" — empty until queried
    dispatchable: bool = False  # has `on: workflow_dispatch`
    triggers_push: bool = False  # has `on: push` in any form
    push_branches: List[str] = field(default_factory=list)
    push_branches_ignore: List[str] = field(default_factory=list)


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
    # True for the synthetic canonical Repo we mint when a submodule URL
    # has no matching top-level repo in the workspace. The synthetic
    # Repo only lives as ChildRef.repo for nested rows — it isn't in
    # state.repos and isn't refreshed; commit-pipeline sync targets
    # skip the "top-level" entry for these so we don't try to sync a
    # non-existent canonical checkout.
    synthetic: bool = False
    workflows: List[WorkflowInfo] = field(default_factory=list)
    # Per-repo, per-workflow tracking choices for the *next* commit. Map of
    # workflow name → bool (True = track this workflow's run after push).
    # Initialized from the global default when the review screen builds.
    track_workflow: "dict[str, bool]" = field(default_factory=dict)
    # "Then run" memory — workflow_dispatch chains the user has wired up
    # on the review screen.
    #   then_run_after_push:    fired once the push completes (before
    #                            any tracked-workflow runs land).
    #   then_run_after_workflow[name]: fired once the named tracked
    #                            workflow run finishes successfully.
    # Empty string (or absent dict entry) means "no follow-up". These
    # live across confirm-screen rebuilds so the user's last choice
    # survives navigation.
    then_run_after_push: str = ""
    then_run_after_workflow: "dict[str, str]" = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.rel == ".":
            return f"{self.path.name} (root)"
        return self.rel

    @property
    def is_dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked)


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
    branch: str = ""  # current branch in the nested checkout (or "(detached)")
    in_sync: bool = True
    kind: str = "submodule"
    message: str = ""
    dirty: bool = False  # working-tree changes in the nested checkout
    suggesting: bool = False  # background suggest in flight for this row
    # Full per-checkout state, populated by link_siblings._populate so
    # the row can be coloured with the same precedence as a top-level
    # Repo (dirty / ahead / behind / diverged / no-upstream / clean /
    # merging / error). The submodule's sync state vs. the canonical
    # is tracked separately by `in_sync` above.
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    merging: bool = False
    error: str = ""


# ---------- Subtree config + workspace state ------------------------------


@dataclass
class SubtreeSpec:
    """One [subtree.<name>] section from idlegit.conf — declares that
    `parent` repo embeds `source` repo's content at `prefix`."""
    name: str
    parent: str  # rel of the parent tracked repo ("." for workspace root)
    source: str  # rel of the source tracked repo
    prefix: str  # path inside parent (e.g. "vendor/lib")


# ---------- Background tasks ----------------------------------------------


_TERMINAL_STATUSES = frozenset({"ok", "fail", "warn"})


@dataclass(eq=False)
class Task:
    """One unit of background work shown in the right-hand sidebar.

    `eq=False` keeps Task identity-based — two distinct rows never
    compare equal even if all fields match, so `list.remove(task)` and
    the WeakKeyDictionary metadata side-table both behave by reference.

    `started_at` is set at construction so the sidebar can render a short
    "Ns / Nm / Nh / Nd ago" timestamp. `finished_at` is set on the first
    transition out of "running" — the auto-remove timer counts from there."""
    label: str
    # running / pending / ok / fail / warn. "pending" is non-terminal
    # like running, but signals the task is waiting on something else
    # (e.g. a chained then-run waiting for its parent workflow to land).
    status: str = "running"
    message: str = ""
    started_at: float = field(default_factory=_monotonic)
    finished_at: Optional[float] = None
    # Back-pointer to the conceptual parent of this task. The label's
    # leading indent (`"  ↳ "`, `"  ↪ "`) is purely cosmetic — the
    # task-detail modal needs an actual structural link so it can show
    # "sub-tasks of X" without parsing label whitespace. None for
    # top-level tasks; populated at task-creation time by workers
    # (e.g. `_poll_run` sets each job sub-task's parent to the run
    # task; `commit_worker` sets each `↳ sync` row's parent to the
    # push task).
    parent: Optional["Task"] = None


@dataclass
class TaskMetadata:
    """Auxiliary per-task metadata used by the task-detail modal. Only
    populated for tasks that represent a `gh run` (parent run task,
    job sub-tasks, or chained then-run placeholders) — plain
    bookkeeping rows like "git add -A" don't get a metadata entry.

    Stored on Tasks as a side dict keyed by `id(task)` so we can keep
    Task itself slim — most rows in the panel never need any of this."""
    repo: Optional[Repo] = None
    slug: Optional[str] = None  # github "owner/name" for gh CLI calls
    run_id: Optional[int] = None  # gh databaseId for the run
    workflow_name: Optional[str] = None
    job_id: Optional[int] = None  # set on job sub-tasks only
    run_url: Optional[str] = None  # html_url for "Open in browser"
    latest_view: Optional[dict] = None  # most recent gh run view JSON
    # Then-run placeholders only — parent workflow being awaited and
    # the chained workflow that'll fire on success.
    pending_after_workflow: Optional[str] = None
    pending_target: Optional[str] = None


class Tasks:
    """Thread-safe list of background-work entries. Worker threads add and
    update; the draw loop snapshots and prunes."""

    def __init__(self) -> None:
        self.items: List[Task] = []
        # Side metadata keyed by the Task object itself via a weak ref
        # so an entry is automatically dropped when the only reference
        # to its task is released (defence in depth alongside the
        # explicit pops in remove/prune_*). Task is `@dataclass(eq=False)`
        # which keeps identity-based hashing — two task rows are only
        # the same key if they're the same object.
        self._meta: "weakref.WeakKeyDictionary[Task, TaskMetadata]" = (
            weakref.WeakKeyDictionary())
        self.lock = threading.Lock()

    def add(self, label: str, parent: Optional[Task] = None) -> Task:
        with self.lock:
            t = Task(label=label, parent=parent)
            self.items.append(t)
            return t

    def children_of(self, task: Task) -> List[Task]:
        """Every task whose `parent is task`. Used by the task-detail
        modal to enumerate sub-tasks of a workflow run / push step."""
        with self.lock:
            return [t for t in self.items if t.parent is task]

    def set_meta(self, task: Task, **fields) -> TaskMetadata:
        """Create-or-update the metadata entry for `task`. Pass only the
        fields that have changed; others on an existing entry are kept."""
        with self.lock:
            meta = self._meta.get(task)
            if meta is None:
                meta = TaskMetadata()
                self._meta[task] = meta
            for k, v in fields.items():
                setattr(meta, k, v)
            return meta

    def get_meta(self, task: Task) -> Optional[TaskMetadata]:
        """Return the metadata entry attached to `task`, or None when
        the task has none (most plain bookkeeping rows)."""
        with self.lock:
            return self._meta.get(task)

    def update(self, task: Task, status: str, message: str = "") -> None:
        with self.lock:
            was_terminal = task.status in _TERMINAL_STATUSES
            task.status = status
            if message:
                task.message = message
            # Stamp finished_at on the first terminal transition so the
            # auto-remove window starts ticking from the right moment.
            if not was_terminal and status in _TERMINAL_STATUSES:
                task.finished_at = _monotonic()

    def set_label(self, task: Task, label: str) -> None:
        """Mutate the label in place — used by long-running workers (e.g.
        a workflow run) that want to refresh the displayed step name as
        they go without spawning a new task row."""
        with self.lock:
            task.label = label

    def snapshot(self) -> List[Task]:
        with self.lock:
            return list(self.items)

    def prune_completed(self) -> None:
        with self.lock:
            kept = [t for t in self.items if t.status == "running"]
            self._drop_meta_outside(kept)
            self.items = kept

    def remove(self, task: Task) -> bool:
        """Remove a specific task from the list. No-op (returns False) if
        the task isn't present — workers may have already pruned it via
        prune_aged or another concurrent removal."""
        with self.lock:
            try:
                self.items.remove(task)
                self._meta.pop(task, None)
                return True
            except ValueError:
                return False

    def prune_aged(self, max_age_seconds: float) -> int:
        """Remove every completed task whose finished_at is older than
        `max_age_seconds`. A negative value is a no-op (the legacy "never
        auto-remove" mode). Returns the number pruned."""
        if max_age_seconds < 0:
            return 0
        now = _monotonic()
        with self.lock:
            before = len(self.items)
            kept = [
                t for t in self.items
                if t.status == "running"
                or t.finished_at is None
                or (now - t.finished_at) < max_age_seconds
            ]
            self._drop_meta_outside(kept)
            self.items = kept
            return before - len(self.items)

    def _drop_meta_outside(self, kept: List[Task]) -> None:
        """Remove `_meta` entries for any task not in `kept`. Caller
        must hold `self.lock`. The WeakKeyDictionary would eventually
        drop them once the strong ref in `self.items` goes away, but
        explicit removal makes the cleanup deterministic and visible
        in tests."""
        kept_set = {id(t) for t in kept}
        # Iterate over a snapshot of keys so we can mutate while walking.
        for t in list(self._meta.keys()):
            if id(t) not in kept_set:
                del self._meta[t]

    def has_running(self) -> bool:
        # "pending" counts here too — chained then-run placeholders sit
        # in pending state waiting on a parent run, and we still want
        # the spinner / redraw loop to tick so their relative-time
        # tags update and the panel stays animated.
        with self.lock:
            return any(t.status in ("running", "pending")
                       for t in self.items)

    def has_pending_auto_remove(self, max_age_seconds: float) -> bool:
        """True when at least one task is finished but still inside its
        auto-remove window (so the draw loop knows to keep ticking)."""
        if max_age_seconds < 0:
            return False
        now = _monotonic()
        with self.lock:
            for t in self.items:
                if (t.status in _TERMINAL_STATUSES
                        and t.finished_at is not None
                        and (now - t.finished_at) < max_age_seconds):
                    return True
            return False


# ---------- Review screen + suggestion helpers ----------------------------


@dataclass
class LFSCandidate:
    repo: Repo
    path: str
    size_str: str
    line_index: int = -1
    track: bool = False


@dataclass
class WorkflowToggle:
    """Per-workflow track-this-run toggle on the review screen. The "live"
    state actually lives in `repo.track_workflow[name]` so the commit
    pipeline can read it after a push. This dataclass is a thin focusable
    record: when the cursor lands on it and Space is pressed, we mutate
    the dict in place."""
    repo: Repo
    workflow_name: str
    line_index: int = -1


@dataclass
class ThenRunSelector:
    """A 'then run' chain selector on the review screen. `after_workflow`
    is the tracked workflow whose successful completion this dispatch
    follows; an empty string means the selector applies to the repo's
    push action itself (root-level "then run after push"). The chosen
    workflow name is read/written through `repo.then_run_after_push` /
    `repo.then_run_after_workflow[after_workflow]`. Left/right cycle
    through the repo's dispatchable+active workflows; '(none)' is the
    no-op default."""
    repo: Repo
    after_workflow: str  # "" = after-push action; else tracked workflow name
    line_index: int = -1


@dataclass
class FileChange:
    """One file change in the working tree, classified for commit-message
    suggestion. `weight` is the sort key (higher = more 'interesting')."""
    path: str
    kind: str  # "added" / "modified" / "deleted"
    weight: float = 0.0


# ---------- Action menu + sub-modals --------------------------------------


@dataclass
class ActionMenuItem:
    """One row in the Tab-context-menu. `enabled=False` greys it out and
    Enter is a no-op; `reason` (if any) appears next to the label."""
    id: str
    label: str
    enabled: bool = True
    reason: str = ""


@dataclass
class FileEntry:
    """One row in the action-menu's working-tree tab. `x`/`y` are the
    porcelain status codes (index/worktree); `inserted`/`deleted` come
    from `git diff --numstat HEAD` and are 0 when not applicable."""
    path: str
    x: str = " "
    y: str = " "
    inserted: int = 0
    deleted: int = 0
    untracked: bool = False


@dataclass
class CommitEntry:
    """One row in the action-menu's commits tab — short hash, subject,
    relative date string. Plain strings so the draw layer can render
    without re-running git."""
    sha: str
    subject: str
    relative: str = ""


@dataclass
class ActionMenu:
    """Modal opened with Tab on a repo / submodule child row. Carries the
    pre-computed metadata (branch, upstream, ahead/behind…) plus a
    tabbed bottom pane (working tree / recent commits) populated when
    the modal opens; commits page in lazily as the user scrolls."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    branch: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    state_label: str = "clean"
    state_pair: int = 0
    items: List[ActionMenuItem] = field(default_factory=list)
    selected: int = 0
    # Bottom pane — focus + tab state.
    pane_focus: bool = False  # True: arrow keys drive the pane, not items.
    pane_tab: str = "tree"    # "tree" | "commits"
    # Working-tree tab.
    tree_files: List[FileEntry] = field(default_factory=list)
    tree_filter: str = ""
    tree_selected: int = 0    # 0 = filter row; >=1 indexes into tree_files
    tree_scroll: int = 0
    # Commits tab — `commits_full` is the loaded prefix; lazy paging
    # extends it. `commits_exhausted` flips True when git log returns
    # fewer rows than asked → we've walked back to the root commit.
    commits_full: List[CommitEntry] = field(default_factory=list)
    commits_filter: str = ""
    commits_selected: int = 0
    commits_scroll: int = 0
    commits_loading: bool = False
    commits_exhausted: bool = False
    # Set by the modal-close handler so any in-flight commits-page
    # worker can short-circuit before mutating a menu the user has
    # already moved on from.
    cancel_event: threading.Event = field(default_factory=threading.Event)


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
class WorkflowPicker:
    """Sub-modal triggered from the action menu's "run a workflow…" item.
    Lists the dispatchable GitHub Actions workflows for the focused repo;
    Enter triggers `gh workflow run <name> --ref <branch>` and starts the
    same tracking pipeline used after a push."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    workflows: List[WorkflowInfo] = field(default_factory=list)
    branch: str = ""  # ref the dispatch will run against
    selected: int = 0
    scroll: int = 0


@dataclass
class SmartSyncCheckout:
    """One checkout of a canonical submodule, captured during the planning
    phase of smart-sync. `parent is None` means the top-level checkout
    (the canonical itself); otherwise the path lives inside `parent`.

    The alignment planner needs more than just (path, branch, label) —
    it needs to know which checkout is "ahead" of upstream (winner
    candidate), which are dirty (auto-stage candidate), and where each
    one's HEAD points (to detect already-converged groups)."""
    canonical: Repo
    parent: Optional[Repo]
    path: Path
    branch: str
    label: str
    head: str = ""              # full sha of HEAD; empty if rev-parse failed
    dirty: bool = False         # `git status --porcelain` non-empty
    ahead: int = 0              # commits in HEAD not in @{u}
    behind: int = 0             # commits in @{u} not in HEAD
    upstream: Optional[str] = None
    # Working-tree signature (sorted (path, blob-hash) pairs of every
    # modified or untracked-not-ignored file). Kept around for the audit
    # task message; no longer load-bearing for safety.
    signature: Tuple[Tuple[str, str], ...] = ()
    sig_mtime: float = 0.0


@dataclass
class AlignHeadsPrompt:
    """Modal opened by smart-sync when align_heads is on and the chosen
    winner is on a detached HEAD — we can't push without first switching
    to a branch, and only the user can decide which one. The worker that
    triggered the modal blocks on `result_event` until the user picks
    (sets `chosen_branch`) or cancels (sets `chosen_branch = ""`)."""
    canonical_label: str
    winner_label: str
    winner_sha: str
    branches: List[str] = field(default_factory=list)
    selected: int = 0
    chosen_branch: Optional[str] = None    # None → still waiting; "" → cancelled
    result_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class TaskActionMenuItem:
    """One row in the task-detail modal's action list. Mirrors
    `ActionMenuItem` but for task-scoped controls."""
    id: str
    label: str
    enabled: bool = True
    reason: str = ""


@dataclass
class TaskActionMenu:
    """Tab-on-task modal — shows the focused task's detail (status,
    durations, run id/url, sub-tasks) and offers controls to cancel a
    running run, change/clear a chained then-run, open the run in a
    browser, or remove a finished task from the panel.

    `task` is the focused row; rich metadata (run_id, slug, etc.)
    comes from `Tasks.get_meta(task)` at draw time so the modal stays
    current as the run polls."""
    task: Task
    items: List[TaskActionMenuItem] = field(default_factory=list)
    selected: int = 0
    # `change_then_run` opens an inline workflow picker — track its
    # state right here rather than spawning a parallel modal.
    sub_picker_open: bool = False
    sub_picker_options: List[str] = field(default_factory=list)
    sub_picker_selected: int = 0
    scroll: int = 0  # body scroll for long sub-task lists


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


# ---------- Top-level UI state --------------------------------------------


# These defaults are duplicated from config.py to avoid an import cycle
# (config imports SubtreeSpec from this file). Override at construction
# time from the loaded Config.
_DEFAULT_SUGGEST = 3
_DEFAULT_LFS_WARN_BYTES = 100 * 1024 * 1024
_DEFAULT_BRANCH_DISPLAY_MAX = 12
_DEFAULT_NAME_DISPLAY_MAX = 40
_DEFAULT_TASK_NAME_DISPLAY_MAX = 16
_DEFAULT_TRUNCATION_MODE = "middle"
_DEFAULT_MAX_VISIBLE_REPO_ROWS = 0
_DEFAULT_TRACK_ACTIONS = True
_DEFAULT_ACTIONS_POLL_SECONDS = 5.0
_DEFAULT_AUTO_REMOVE_COMPLETED_AFTER = -1.0


@dataclass
class State:
    repos: List[Repo]
    workspace_name: str
    suggest_added: int = _DEFAULT_SUGGEST
    suggest_updated: int = _DEFAULT_SUGGEST
    suggest_deleted: int = _DEFAULT_SUGGEST
    lfs_warn_bytes: int = _DEFAULT_LFS_WARN_BYTES
    branch_display_max: int = _DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = _DEFAULT_NAME_DISPLAY_MAX
    task_name_display_max: int = _DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation: str = _DEFAULT_TRUNCATION_MODE
    branch_truncation: str = _DEFAULT_TRUNCATION_MODE
    task_name_truncation: str = _DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows: int = _DEFAULT_MAX_VISIBLE_REPO_ROWS
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    track_actions_default: bool = _DEFAULT_TRACK_ACTIONS
    actions_poll_seconds: float = _DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after: float = _DEFAULT_AUTO_REMOVE_COMPLETED_AFTER
    selected: int = 3  # 0,1,2 = toggles; 3..N+2 = repos
    body_scroll: int = 0  # how many body rows are scrolled past the top
    auto_stage: bool = True
    auto_push: bool = True
    # Smart-sync: when align_heads is True, smart-sync also tries to pull
    # detached-HEAD checkouts of the same canonical onto the winner's
    # branch (after popping a modal that asks which branch to push the
    # winner's commits to). When False, smart-sync only aligns checkouts
    # already on the same branch as the winner — detached / divergent
    # checkouts get warn-skipped. The non-destructive policies
    # (`merge --ff-only`, refusal on conflict) apply in both modes.
    align_heads: bool = True
    tasks: Tasks = field(default_factory=Tasks)
    spinner_frame: int = 0
    field_cursor: int = 0
    # Active panel for navigation: "repos" (main list, default) or
    # "tasks" (the right-hand task panel). Shift+Tab toggles. While
    # focused on tasks, ↑/↓ moves task_selected, Enter removes a
    # finished task, Esc returns focus to "repos".
    focused_panel: str = "repos"
    task_selected: int = 0
    task_scroll: int = 0
    action_menu: Optional[ActionMenu] = None
    branch_picker: Optional[BranchPicker] = None
    reset_prompt: Optional[ResetPrompt] = None
    workflow_picker: Optional[WorkflowPicker] = None
    align_heads_prompt: Optional[AlignHeadsPrompt] = None
    task_action_menu: Optional[TaskActionMenu] = None

    def selectable_rows(self) -> List[Tuple]:
        """Flat selectable list: ('toggle', 0|1|2, None), ('repo', repo, None),
        ('child', parent_repo, child_ref). Toggles are first, then each repo
        followed by its children (interleaved, in display order)."""
        rows: List[Tuple] = [
            ("toggle", 0, None),
            ("toggle", 1, None),
            ("toggle", 2, None),
        ]
        for repo in self.repos:
            rows.append(("repo", repo, None))
            for child in repo.children:
                rows.append(("child", repo, child))
        return rows

    @property
    def total_rows(self) -> int:
        return 3 + sum(1 + len(r.children) for r in self.repos)

    @property
    def on_toggle(self) -> bool:
        return self.selected < 3

    def task_repo_label(self, repo: Optional["Repo"]) -> str:
        """Repo display name truncated to fit comfortably inside a sidebar
        task label. The sidebar panel is narrow and a long display name
        (e.g. "Upskill.Health.Domain.Models") otherwise crowds out the
        actual task description ("commit at top-level Upskill.Health…").

        Inlined trim instead of importing `ui.geometry.truncate` so this
        layer stays free of UI dependencies; behaviour matches the same
        start / middle / end mode set."""
        if repo is None:
            return ""
        text = repo.display_name
        max_len = self.task_name_display_max
        mode = self.task_name_truncation
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
