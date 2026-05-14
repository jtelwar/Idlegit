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
class WorkflowInput:
    """One `workflow_dispatch.inputs` entry parsed from a workflow's
    YAML — surfaced in the review screen as an inline text-field
    parameter and forwarded to `gh workflow run -F <name>=<value>`
    when the dispatch fires. Only the fields we render today are
    captured; type / required / options can be added later without
    a schema change since we keep value handling generic (string)."""
    name: str
    description: str = ""
    default: str = ""


@dataclass
class WorkflowInfo:
    """A GitHub Actions workflow advertised by `gh workflow list`. Cached on
    Repo so the review screen and the action menu can list them without
    re-hitting the API on every redraw.

    `triggers_push` / `push_branches` / `push_branches_ignore` /
    `push_tags` / `push_tags_ignore` are derived from the YAML's `on:`
    block so we can predict whether the workflow will fire on a push
    to the repo's current branch. `tags`-only filtering matters
    because `on: push: tags: [...]` (with no `branches:`) means the
    workflow fires *only* on tag push, not on branch push — review-
    screen tracking and after-push then-run wiring needs to skip
    those workflows when the user is pushing a regular commit.

    `inputs` holds the `workflow_dispatch.inputs` entries parsed from
    the same YAML; when the user picks this workflow as a then-run
    target, the review screen renders one inline param row per
    input and the dispatch site forwards typed values via -F.

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
    push_tags: List[str] = field(default_factory=list)
    push_tags_ignore: List[str] = field(default_factory=list)
    inputs: List[WorkflowInput] = field(default_factory=list)


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
    # Generic per-action parameter buffers paired with the targets
    # in the then-run dicts above. Outer key is the parent's slot
    # (after-push uses the "" key for `then_run_params_after_push`,
    # after-workflow uses the workflow name); inner dict maps a
    # parameter name (e.g. "tag" for the __add_tag__ sentinel,
    # workflow_dispatch input names for real workflows) to its
    # buffered value. The review screen's inline param_input rows
    # read/write through these dicts; dispatch sites pop the
    # matching slot when the parent task fires.
    then_run_params_after_push: "dict[str, str]" = field(
        default_factory=dict)
    then_run_params_after_workflow: "dict[str, dict[str, str]]" = field(
        default_factory=dict)

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
    # True while an action / refresh is in flight against this child —
    # the row's state dot renders as the global spinner glyph instead
    # of the dirty/clean/etc. colour, so it's obvious the row's
    # "current" state is in transition. Same role as `Repo.refreshing`.
    refreshing: bool = False
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


@dataclass
class Workspace:
    """One [workspace.<name>] section from idlegit.workspaces — a named
    bundle of folders to scan plus optional per-workspace overrides on
    settings normally read from idlegit.conf.

    `overrides` is keyed by Config-field name (e.g. "default_auto_stage",
    "name_truncation", "suggest_added"). Values are already type-coerced
    by the loader. Unrecognized keys are dropped on read so future schema
    changes don't crash on stale files. `subtrees` are workspace-scoped;
    when a workspace doesn't declare any its loader inherits the global
    list from idlegit.conf so existing setups keep working.

    `cached_repos` is the (refreshed) Repo list discovered for this
    workspace. Populated at startup so workspace switching is instant
    — switch_workspace just reassigns `state.repos` to this list and
    skips re-discovery. NOT persisted to idlegit.workspaces."""
    name: str
    folders: List[Path]
    overrides: dict = field(default_factory=dict)
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    cached_repos: list = field(default_factory=list)


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
        """Drop terminal tasks (ok / fail / warn) only. `running` and
        `pending` rows stay — `pending` in particular is a non-terminal
        "waiting on parent" placeholder (e.g. a chained then-run sitting
        under its parent workflow run), and dropping it on Ctrl+R would
        wipe the user's queued follow-up before the parent has had a
        chance to fire it."""
        with self.lock:
            kept = [t for t in self.items
                    if t.status in ("running", "pending")]
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


@dataclass
class DiffViewer:
    """Modal popped from the review screen's right pane when the user
    presses Enter on a file row. Loads `git diff HEAD -- <path>` (or
    the raw file contents for an untracked addition) in a background
    thread, then renders the result in a scrollable pane. Enter or
    Esc closes the modal."""
    file_path: str          # repo-relative path, used for the title + git command
    target_path: Path       # repo working-tree dir the path is rooted in
    label: str              # block label rendered in the modal title
    untracked: bool = False
    # Optional: when set, the loader runs `git show <sha> -- <path>`
    # instead of `git diff HEAD -- <path>` so the commit-view modal
    # can reuse the diff viewer scoped to a single commit.
    commit_sha: str = ""
    # ---- Tabbed UI state -----------------------------------------
    # Three tabs: diff (default, the original view), log (commits
    # touching this file), blame (line-by-line attribution). ←/→
    # switches the active tab; ↑/↓ scrolls the active tab. Each
    # tab carries its own lines / loading flag / scroll position so
    # switching back lands where you left.
    active_tab: str = "diff"
    # Diff tab — keeps the original short field names so external
    # animation hooks (`state.diff_viewer.loading`) keep working.
    lines: "list[str]" = field(default_factory=list)
    loading: bool = True
    scroll: int = 0
    # Log tab.
    log_lines: "list[str]" = field(default_factory=list)
    log_loading: bool = True
    log_scroll: int = 0
    # Blame tab.
    blame_lines: "list[str]" = field(default_factory=list)
    blame_loading: bool = True
    blame_scroll: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class ReviewBlock:
    """One commit target on the two-panel review screen — a top-level
    repo OR a nested submodule child. Each block owns its own LFS
    warnings, workflow toggles, and then-run selectors (the same
    focusables the old single-list review surfaced), grouped under a
    repo-specific header so the user can navigate target-by-target.

    The right pane reads `files` (populated asynchronously after the
    review opens — `files_loading` stays True until the first
    `query_working_tree` finishes for this block). `cancel_event`
    lets a worker short-circuit if the user dismisses the review
    before the load completes."""
    label: str
    branch: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional["ChildRef"] = None
    message: str = ""
    merging: bool = False
    conflict_paths: "list[str]" = field(default_factory=list)
    has_origin: bool = False
    upstream: Optional[str] = None
    siblings_summary: str = ""
    push_summary: str = ""    # rendered "push: yes (sets upstream …)" line
    auto_stage: bool = True
    auto_push: bool = True
    is_child: bool = False    # True for submodule child blocks
    threshold_mb: int = 0     # LFS threshold for the warning header
    lfs_candidates: "list[LFSCandidate]" = field(default_factory=list)
    workflow_toggles: "list[WorkflowToggle]" = field(default_factory=list)
    then_run_items: "list[ThenRunSelector]" = field(default_factory=list)
    # Right-pane working-tree files. Populated by the async loader the
    # review screen kicks off when the block is built. `files_loading`
    # is True while the worker is in flight so the pane shows a
    # spinner instead of "(no changes)".
    files: "list[FileEntry]" = field(default_factory=list)
    files_loading: bool = True
    # Per-block right-pane cursor — preserved across panel-focus
    # toggles so the user's place in the file list isn't lost when
    # they Shift+Tab back to the left side.
    file_selected: int = 0
    file_scroll: int = 0
    # Right-pane toolbar focus. -1 = focus is in the file list (the
    # default — landing in the right pane lands on a file row); 0 =
    # "stage all" button; 1 = "unstage all" button. Up from the first
    # file lifts focus to the toolbar; Down from the toolbar drops it
    # back to file 0.
    toolbar_focus: int = -1
    # When True, the commit step uses `git commit --amend -m <msg>`
    # instead of a fresh commit so the staged changes (if any) and
    # the new message replace the latest unpushed commit. Toggled by
    # the right-pane toolbar's `[X] amend` checkbox; the toolbar
    # offers it only when `ahead > 0` so we never amend a published
    # commit (the cardinal rule's no-rewrite-of-shared-history line).
    amend: bool = False
    # Per-file "should this end up in the index for the commit?" flag.
    # Populated by the file loader once `files` lands. Defaults derive
    # from `auto_stage`: True → every change checked; False → only
    # already-staged paths (x != " ") checked. Space in the right
    # pane toggles this; the commit pipeline reads this dict to drive
    # `git add` / `git restore --staged` calls per path.
    staged_paths: "dict[str, bool]" = field(default_factory=dict)
    # True while a re-suggest worker is in flight for this block —
    # the message line in the left pane shows a spinner-prefixed
    # "generating…" cue, mirroring the main-screen suggest indicator.
    suggesting: bool = False
    # Set by the review's Esc / Enter handler so the file-loader
    # worker drops its result on the floor instead of mutating a
    # block the user has already moved on from.
    cancel_event: threading.Event = field(default_factory=threading.Event)


# ---------- Action menu + sub-modals --------------------------------------


@dataclass
class ActionMenuItem:
    """One row in the Tab-context-menu. `enabled=False` greys it out and
    Enter is a no-op; `reason` (if any) appears next to the label.

    `has_submenu` flags items that open a nested action list — the
    renderer paints a right-pointing chevron in the focus-arrow
    column (always visible, dimmed when not focused). `is_back` is
    the synthetic top-of-submenu "back" row that pops the submenu
    stack. `is_separator` renders a dim divider line and is skipped
    by the navigation cursor."""
    id: str
    label: str
    enabled: bool = True
    reason: str = ""
    has_submenu: bool = False
    is_back: bool = False
    is_separator: bool = False


@dataclass
class ActionSubmenuFrame:
    """One level of submenu navigation in the action menu. Pushed onto
    the menu's submenu_stack when the user enters; popped when the
    "back" row or Left arrow fires. `name` is an internal id (used by
    handlers to dispatch dynamically-built submenus); `label` is the
    user-facing breadcrumb segment."""
    name: str
    label: str
    items: List["ActionMenuItem"] = field(default_factory=list)
    selected: int = 0


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
    # Submenu navigation as a stack of frames — empty = on the main
    # menu, top-of-stack = currently visible submenu. Push a frame
    # when entering (Right / Enter on a has_submenu opener); pop on
    # Left or "back". Each frame keeps its own selected index so
    # returning lands where you left off. Supports arbitrary depth
    # (main → branch, main → stashes → stash@{0}, …).
    submenu_stack: List[ActionSubmenuFrame] = field(default_factory=list)
    # Cached branch_meta dict from the last state-load — used by
    # dynamic submenu builders (re-build when loader refreshes, or
    # when entering a submenu mid-session). Not user-facing.
    cached_meta: dict = field(default_factory=dict)
    # Stash count rendered in the main-menu "stashes (N)" opener
    # label. Queried once at open time; stays stale until reopen
    # since `git stash list` would be wasteful to re-run on every
    # state refresh.
    stash_count: int = 0
    # Cached stash list for the Stashes submenu. Each entry is
    # (ref, message) — ref is a stable form like "stash@{0}", message
    # is the human "On main: WIP foo" line returned by `git stash
    # list`. Loaded when the user enters the submenu.
    stashes: "list[tuple[str, str]]" = field(default_factory=list)
    # Cached remotes list for the Remotes submenu. Each entry is
    # (name, url). Loaded when the user enters the submenu and
    # whenever a remote is renamed/added/removed via the inline
    # editor so the rebuilt frame reflects the new state.
    remotes_list: "list[tuple[str, str]]" = field(default_factory=list)
    remote_count: int = 0
    # ---- Inline edit state ----------------------------------------
    # Set when an item activates an inline editable field — Enter on
    # a remote row enters rename mode, "new remote" runs through
    # name → url → confirm. While `edit_field` is non-empty,
    # keystrokes go to the buffer instead of nav. `edit_target_id`
    # is the item id that was activated (e.g. "remote:origin"); the
    # handler reads it to know what to apply on confirm.
    edit_field: str = ""        # "" / "rename_remote" / "add_remote_name"
                                # / "add_remote_url"
    edit_typed: str = ""
    edit_cursor: int = 0
    edit_pre_value: str = ""
    edit_target_id: str = ""
    edit_extra: "dict[str, str]" = field(default_factory=dict)
    # ---- Confirm prompt overlay ----------------------------------
    # Set when an inline edit completes and the user needs to
    # confirm before the worker fires. Renders as a y/N strip
    # below the items. `confirm_action` is one of "rename_remote",
    # "remove_remote", "add_remote"; `confirm_args` carries the
    # args needed to dispatch.
    confirm_message: str = ""
    confirm_action: str = ""
    confirm_args: "dict[str, str]" = field(default_factory=dict)
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
    # Loading flags for the three async populators kicked off when the
    # modal opens. The keypress handler installs the menu instantly with
    # cached values; these flags drive a spinner badge on the affected
    # pane until the fresh `git` query lands. anim_running picks them up
    # so the main loop ticks at 100ms while loading and the user sees
    # the spinner animate.
    state_loading: bool = False
    tree_loading: bool = False
    # Set by the modal-close handler so any in-flight commits-page
    # worker can short-circuit before mutating a menu the user has
    # already moved on from.
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class BranchPicker:
    """Sub-modal triggered from the action menu's "switch branch" or
    "merge in branch" items. `mode` is "switch" (default) or "merge"
    — same picker, two effects: switch_branch dispatches `switch_branch`
    on Enter; merge dispatches `ff_merge`. The current branch is shown
    as a "(current)" marker; in merge mode it's also disabled (you
    can't merge a branch into itself)."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    branches: List[str] = field(default_factory=list)
    current: str = ""
    # selected = -1 is the "Create new branch" input row at the top
    # (switch mode only); 0..len(branches)-1 picks an existing branch.
    selected: int = 0
    scroll: int = 0
    mode: str = "switch"
    # Buffer for the "Create new branch" input row. Populated as the
    # user types while focused on selected = -1.
    create_typed: str = ""


@dataclass
class RemoteRow:
    """One row in the RemotesModal — represents either an existing remote
    (loaded via `list_remotes`) or a pending-add row created in the
    session. Edits accumulate in `name` / `url` and are diffed against
    `original_*` on close to compute the actual git operations to run.

    `to_delete` flags an existing remote for removal on apply; pressing
    D on a freshly-added row removes it from the list outright (no
    pending-delete state to track since nothing exists on disk yet)."""
    original_name: str = ""   # "" iff this is a session-added row
    original_url: str = ""
    name: str = ""
    url: str = ""
    to_delete: bool = False
    is_new: bool = False


@dataclass
class RemotesModal:
    """Modal for managing the focused repo's remotes (action menu →
    "remotes…"). Edits stay local until the user closes the modal,
    at which point a confirmation prompt summarises pending changes
    and the apply pipeline runs them as a single batched task."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    rows: List[RemoteRow] = field(default_factory=list)
    # 0..len(rows)-1 = remote rows; len(rows) = "+ Add new remote"
    # placeholder; -1 left unused (no top sentinel here).
    selected: int = 0
    scroll: int = 0
    # Edit mode: "" = nav mode; "name" or "url" = the field on the
    # focused row that's currently being typed into. Pre-edit value is
    # stashed so Esc can revert.
    edit_field: str = ""
    edit_pre_value: str = ""
    # Confirmation overlay state — True while the "Apply N change(s)?"
    # prompt is up. The prompt eats keys until Y/N/Esc.
    confirming: bool = False


@dataclass
class CloneModal:
    """Modal for cloning a remote into the active workspace. Opened
    from the workspace menu's "+ Clone repository…" row. Tracks all
    four field buffers and the focused row; the path field gets a
    discover_repos-style live check (idle while the user types, fires
    once 250ms after the last edit) so the user sees whether the
    target dir already has a repo before pressing Enter."""
    workspace_name: str
    # Folders configured for the active workspace — used to build the
    # default destination path when the user types just a name.
    workspace_folders: List[Path] = field(default_factory=list)
    url: str = ""
    dest_text: str = ""
    branch: str = ""
    recurse_submodules: bool = True
    # Focused field index: 0 = url, 1 = dest, 2 = branch, 3 = recurse,
    # 4 = "Clone" button row.
    selected: int = 0
    edit_field: str = ""   # "" / "url" / "dest" / "branch"
    edit_pre_value: str = ""
    # Last-clicked Clone status — set by the worker so the modal can
    # show "(cloning…)" / "(failed: …)" inline. None = idle.
    cloning: bool = False
    error: str = ""


@dataclass
class BranchNamePrompt:
    """Sub-modal for typing a branch name — both for the action menu's
    "Save HEAD to new branch…" recovery flow (mode "save_head", the
    detached-HEAD case) and for "Rename branch…" (mode "rename", the
    on-a-branch case). Cardinal-rule safe in both modes: branch
    creation and `git branch -m` only touch refs."""
    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    typed: str = ""           # user's typed text (empty → use default_name)
    default_name: str = ""    # placeholder shown when typed is empty
    head_sha: str = ""        # short sha rendered in the subtitle
    # "save_head" → create new branch at HEAD (detached recovery);
    # "rename"   → rename the current branch.
    mode: str = "save_head"
    current_branch: str = ""  # rendered in the subtitle for "rename"


@dataclass
class DetachedRecoveryPrompt:
    """Modal popped by the smart-sync / commit pipelines when they
    encounter a detached HEAD that needs to be parked on a branch
    before they can continue. Cardinal-rule safety is decided BEFORE
    the modal opens — `can_ff` is True iff `target_branch` is already
    an ancestor of HEAD, in which case `git checkout -B target HEAD`
    fast-forwards the branch without orphaning any commits.

    The worker thread that triggered the modal blocks on
    `result_event` until the user picks (sets `chosen_action` to
    "ff" or "cancel")."""
    target_label: str       # full repo display name, no truncation
    head_sha: str           # full sha; rendered as :8 in the modal
    target_branch: str      # the branch we'd FF (e.g. "master")
    n_extra: int            # commits HEAD has that target_branch doesn't
    can_ff: bool            # True iff target_branch is ancestor of HEAD
    chosen_action: Optional[str] = None  # "ff" / "cancel"
    result_event: threading.Event = field(default_factory=threading.Event)


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
    (sets `chosen_branch`) or cancels (sets `chosen_branch = ""`).

    `canonical_name` and `winner_parent_name` are the full
    `Repo.display_name` strings — the modal lays them out itself with
    `wrap_label_value` rather than receiving a pre-truncated string.
    `winner_parent_name` is empty when the winner is the canonical's
    own top-level checkout (no parent)."""
    canonical_name: str
    winner_parent_name: str
    winner_sha: str
    branches: List[str] = field(default_factory=list)
    selected: int = 0
    # First-visible-branch index when the list overflows the modal's
    # available height. The draw routine clamps + adjusts so the
    # selected row stays in view even on tiny terminals.
    scroll: int = 0
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


@dataclass
class AppMenuRow:
    """One row of the global app menu. `kind` drives input handling
    and rendering:
      - "header"           — section divider (e.g. "APPLICATION")
      - "app_info"         — non-interactive informational row
                             (version label, update-check result)
      - "app_action"       — action button; `attr_name` is the action
                             id ("check_for_updates", "update_now")
      - "workspace"        — one configured workspace; `attr_name`
                             holds the workspace index as a string
      - "create_workspace" — trailing "+ Create new workspace…"
                             sentinel that hands off to the creator
    `label` is the human-readable text shown to the user; the
    renderer composes any extra metadata (folder count, "active",
    paths) on top from the live state."""
    label: str
    attr_name: str
    kind: str


@dataclass
class AppMenu:
    """Global app menu, opened with Tab on the title row. Two
    sections:
      - APPLICATION: app name + version, Check for updates button,
        and (after a check) latest-release info plus an Update now
        button when the installed version is behind.
      - WORKSPACES: the existing workspaces picker (active workspace
        marked, "+ Create new workspace…" trailing sentinel). Enter
        on a workspace switches the active index; Enter on the
        create sentinel hands off to WorkspaceCreator.

    Rows are dynamically rebuilt whenever the update-check state
    flips (`update_check` ↔ `update_check_rendered`) so a worker
    completing in the background surfaces immediately.

    `update_check` lifecycle:
      - "idle"         — nothing fetched yet, button is offered
      - "checking"     — async worker is in flight, spinner shows
      - "done"         — `latest_version` populated; Update now
                         appears when behind
      - "no_releases"  — repo exists but has no published releases
                         yet (GitHub returns 404 from /releases/
                         latest in that case — distinct from a
                         real failure)
      - "failed"       — `update_check_error` carries a short
                         reason
    `update_check_rendered` is what the row list was last rebuilt
    against; the main-loop tick rebuilds rows when it diverges."""
    rows: List[AppMenuRow] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    update_check: str = "idle"
    latest_version: str = ""
    update_check_error: str = ""
    update_check_rendered: str = "idle"


@dataclass
class WorkspaceDraft:
    """One row in the WorkspaceCreator modal — a path the user is
    typing plus the latest async repo-discovery result for it.

    The check is debounced + run on a worker thread; while in-flight
    `last_checked` lags `path_text` and the row shows a spinner. Once
    the worker finishes it stamps `repo_count` (>=0) or `error` (a
    one-line "(no such directory)" / "(permission denied)" hint) and
    sets `last_checked` to the text it actually checked, suppressing
    re-runs until the user edits again."""
    path_text: str = ""
    last_checked: str = ""
    repo_count: int = -1  # -1 = pending / unchecked; >= 0 = checked count
    error: str = ""
    checking: bool = False  # True while a discovery thread is in flight


@dataclass
class WorkspaceCreator:
    """First-run / new-workspace dialogue. Lets the user list one or
    more folder paths; each becomes a workspace named after the
    folder's basename. Real-time repo counts surface a tick next to
    every path that resolves to a folder containing tracked repos.

    `drafts` always has at least one trailing empty row so the user
    can add another path without first navigating to a "+ new" item;
    the bottom-most "Done" pseudo-row finalises the dialogue and is
    selected when `selected == len(drafts)`."""
    drafts: List[WorkspaceDraft] = field(default_factory=list)
    selected: int = 0  # 0..len(drafts) — last value selects the Done row
    field_cursor: int = 0
    title: str = ""
    intro: str = ""
    # Set when the modal commits and we want the main loop to swap the
    # active workspace list. Carries the list of Workspaces produced
    # from the non-empty drafts; remains None until commit.
    result: Optional[List["Workspace"]] = None


@dataclass
class WorkspaceMenuRow:
    """One row of the global app menu modal. `kind` drives input
    handling and rendering:
      - "header"     — section divider (e.g. "APPLICATION", "FOLDERS")
      - "app_info"   — non-interactive informational row (version,
                       update-check result)
      - "app_action" — action button (e.g. "Check for updates",
                       "Update now"); `attr_name` is the action id
      - "folder"     — workspace folder path (editable)
      - "add_folder" — "+ Add folder…" sentinel
      - "clone"      — "+ Clone repository…" launcher
      - "bool"       — toggled with Space
      - "trunc_mode" — cycled (start/middle/end) with ←/→
      - "int"        — adjusted with ←/→ at `step`, bounded by
                       `min_value`/`max_value`

    `label` is the human-readable setting name shown on the left;
    `attr_name` names the State attribute the row drives (or, for
    app_action rows, the action id). `hint_text` is the muted
    one-line explanation shown above the hints footer when the row
    is focused — empty for rows that don't need an explainer."""
    label: str
    attr_name: str
    kind: str
    min_value: int = 0
    max_value: int = 999
    step: int = 1
    hint_text: str = ""


@dataclass
class WorkspaceMenu:
    """Per-workspace settings modal, opened with Tab on the workspace
    selector row. Lets the user override per-workspace settings
    against the global idlegit.conf defaults AND edit the workspace's
    folder list.

    The rows are dynamically rebuilt on open so the modal reflects the
    current `ws.folders` list — folder rows interleave with override
    rows. Inline editing kicks in when the user presses Enter on a
    folder / "+ add folder" row: `editing` flips True, `edit_buffer`
    holds the in-flight text, and the next set of keystrokes drives
    that buffer rather than navigating between rows. Esc cancels edit
    mode without persisting; Enter commits the edited text back to the
    workspace's folders list and persists via save_workspaces.

    `path_drafts` mirrors the workspace's folder list with one
    WorkspaceDraft per folder so the modal can run live discover_repos
    checks against typed paths the same way the creator wizard does —
    the tick / repo count appears next to each folder row."""
    rows: List[WorkspaceMenuRow] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    editing: bool = False
    edit_buffer: str = ""
    edit_cursor: int = 0
    path_drafts: List["WorkspaceDraft"] = field(default_factory=list)


# ---------- Top-level UI state --------------------------------------------


# These defaults are duplicated from config.py to avoid an import cycle
# (config imports SubtreeSpec from this file). Override at construction
# time from the loaded Config.
_DEFAULT_SUGGEST = 3
_DEFAULT_LFS_WARN_BYTES = 100 * 1024 * 1024
_DEFAULT_BRANCH_DISPLAY_MAX = 12
_DEFAULT_NAME_DISPLAY_MAX = 40
_DEFAULT_CHILD_NAME_DISPLAY_MAX = -1  # -1 = inherit from name_display_max
_DEFAULT_TASK_NAME_DISPLAY_MAX = 16
_DEFAULT_TRUNCATION_MODE = "middle"
_DEFAULT_MAX_VISIBLE_REPO_ROWS = 0
_DEFAULT_TASKS_MIN_WIDTH_PERCENT = 0.2
_DEFAULT_TASKS_MAX_WIDTH_PERCENT = 0.5
_DEFAULT_TRACK_ACTIONS = True
_DEFAULT_ACTIONS_POLL_SECONDS = 5.0
_DEFAULT_AUTO_REMOVE_COMPLETED_AFTER = -1.0
_DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW = 480


@dataclass
class CommitViewModal:
    """Sub-modal of the action menu — opened with Tab on a focused
    row in the recent-commits pane. Shows the commit's metadata
    (author, date, message), tag badges, an action list (currently
    just `+ add tag`), and a tabbed lower pane with `Changes` and
    `Reflog` tabs. The Changes tab reuses the review pane's file
    row renderer; Tab on a focused file row pops the diff viewer
    scoped to this commit (`git show <sha> -- <path>`). The Reflog
    tab lists HEAD reflog entries that mention this commit's sha
    so the user can see when the commit was current (checkout,
    reset, rebase, …).

    Section navigation: action items at the top, tabbed pane
    below; Down past the last action drops focus into the pane,
    Home returns to the actions. While `section == "actions"`, Tab
    closes the modal back to the action menu's commits pane.
    While `section == "tabs"`, Tab opens the diff viewer for the
    focused file row (Changes only); ←/→ switches active tab; ↑/↓
    scrolls the active tab."""
    target_label: str
    target_path: Path
    sha: str
    subject: str = ""
    body: str = ""
    author: str = ""
    date: str = ""
    tags: List[str] = field(default_factory=list)
    files: List[FileEntry] = field(default_factory=list)
    files_loading: bool = True
    details_loading: bool = True
    tags_loading: bool = True
    # Reflog tab — entries that mention this commit's sha. Loaded
    # alongside the file changes when the modal opens.
    reflog_entries: "list[str]" = field(default_factory=list)
    reflog_loading: bool = True
    # Section + active-tab state. `section` is one of "actions" or
    # "tabs"; while in "tabs", `active_tab` picks "changes" or
    # "reflog". Per-tab cursor / scroll fields below.
    section: str = "actions"
    action_selected: int = 0
    active_tab: str = "changes"
    file_selected: int = 0
    file_scroll: int = 0
    reflog_selected: int = 0
    reflog_scroll: int = 0
    # Inline tag-name edit (Enter on "+ add tag" activates this).
    edit_field: str = ""        # "" / "add_tag"
    edit_typed: str = ""
    # Confirm overlay before applying a destructive-adjacent op
    # (creating a tag is non-destructive but still gets a y/N gate
    # so the user reviews the name before it lands).
    confirm_message: str = ""
    confirm_action: str = ""    # "add_tag"
    confirm_args: "dict[str, str]" = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)


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
    # Submodule + subtree child rows on the main screen. -1 means
    # "use the same cap as parent rows (`name_display_max`)" — the
    # historical behaviour. Set positive to truncate child names
    # tighter without affecting parent rows.
    child_name_display_max: int = _DEFAULT_CHILD_NAME_DISPLAY_MAX
    task_name_display_max: int = _DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation: str = _DEFAULT_TRUNCATION_MODE
    branch_truncation: str = _DEFAULT_TRUNCATION_MODE
    task_name_truncation: str = _DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows: int = _DEFAULT_MAX_VISIBLE_REPO_ROWS
    tasks_min_width_percent: float = _DEFAULT_TASKS_MIN_WIDTH_PERCENT
    tasks_max_width_percent: float = _DEFAULT_TASKS_MAX_WIDTH_PERCENT
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    track_actions_default: bool = _DEFAULT_TRACK_ACTIONS
    actions_poll_seconds: float = _DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after: float = _DEFAULT_AUTO_REMOVE_COMPLETED_AFTER
    # Cap on the commit message rendered on the review screen. The
    # message wraps onto as many rows as needed to fit; only end-
    # truncation kicks in once a single message exceeds this many
    # chars. 0 disables the cap entirely.
    max_commit_message_length_in_review: int = (
        _DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW)
    # Multi-workspace state. `workspaces` is the list loaded from
    # idlegit.workspaces (or a single synthesized entry derived from
    # idlegit.conf when the file doesn't exist); `active_workspace_index`
    # picks which one is live. Both default to empty so plain
    # `State(repos=[])` keeps working in tests that don't care about
    # workspaces. The header row's left/right cycle mutates the index
    # and triggers a workspace switch (re-discover + re-link siblings).
    workspaces: List[Workspace] = field(default_factory=list)
    active_workspace_index: int = 0
    # Snapshot of the loaded base config, kept around so the workspace
    # overrides modal can clear an override (revert to inherited) and
    # know what value to restore. Optional because legacy callers (and
    # most tests) instantiate State without going through load_config.
    base_config: Optional[object] = None
    # Two pseudo-rows above the body:
    #   selected = -2 — Idlegit title row (Tab opens workspaces picker).
    #   selected = -1 — workspace switcher (←/→ cycles workspaces;
    #                   Tab opens the workspace settings menu).
    #   0..total_rows-1 — repos + submodule/subtree children body.
    # Up from 0 lands on -1; Up from -1 lands on -2; Up from -2 wraps
    # to the bottom of the body.
    selected: int = 0
    body_scroll: int = 0  # how many body rows are scrolled past the top
    # Commit-pipeline toggles. Configured exclusively via the workspace
    # menu's COMMIT section now — the main panel no longer carries
    # them as toggle rows.
    auto_stage: bool = True
    auto_push: bool = True
    # Smart-sync settings. align_heads: pull detached-HEAD checkouts of
    # the same canonical onto the winner's branch (when False, detached
    # checkouts warn-skip). auto_ff: automatically fast-forward losers
    # (when False, loser alignment is skipped — winner still commits +
    # pushes). prompt_for_branch: open a modal asking which branch the
    # detached winner should push to (when False, we resolve
    # origin/HEAD and use that branch unattended).
    # prevent_smart_sync_silent_merge: when True, same-branch loser
    # alignment uses `merge --ff-only` only (no automatic merge commits).
    # When False (default), after FF fails we run `merge --no-edit` when
    # histories diverged.
    align_heads: bool = True
    auto_ff: bool = True
    prompt_for_branch: bool = True
    prevent_smart_sync_silent_merge: bool = False
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
    branch_name_prompt: Optional[BranchNamePrompt] = None
    reset_prompt: Optional[ResetPrompt] = None
    workflow_picker: Optional[WorkflowPicker] = None
    align_heads_prompt: Optional[AlignHeadsPrompt] = None
    detached_recovery_prompt: Optional[DetachedRecoveryPrompt] = None
    # Opened from the review screen's right pane when the user presses
    # Enter on a file row — sub-modal of the review's inner loop, not
    # the main loop, so dispatch happens inside handle_confirm rather
    # than alongside the other modals.
    diff_viewer: Optional[DiffViewer] = None
    task_action_menu: Optional[TaskActionMenu] = None
    workspace_menu: Optional["WorkspaceMenu"] = None
    workspace_creator: Optional["WorkspaceCreator"] = None
    app_menu: Optional["AppMenu"] = None
    remotes_modal: Optional[RemotesModal] = None
    clone_modal: Optional[CloneModal] = None
    # Sub-modal of the action menu's commits pane — Tab on a focused
    # commit row opens it. Drawn on top of the action menu; key
    # routing in idlegit.py ensures it gets keys before the action
    # menu does.
    commit_view_modal: Optional[CommitViewModal] = None

    @property
    def active_workspace(self) -> Optional[Workspace]:
        """The currently-active Workspace, or None if no workspaces are
        configured (legacy single-workspace tests construct State without
        going through load_workspaces)."""
        if not self.workspaces:
            return None
        idx = max(0, min(self.active_workspace_index, len(self.workspaces) - 1))
        return self.workspaces[idx]

    @property
    def active_folders(self) -> List[Path]:
        """Folder list driving discovery for the active workspace. Empty
        list means "fall back to legacy single-folder behaviour" (callers
        like kick_off_inline_refresh can derive from state.repos[0])."""
        ws = self.active_workspace
        if ws is None:
            return []
        return list(ws.folders)

    @property
    def on_workspace_row(self) -> bool:
        """True when the workspace switcher row (above the body, below
        the title) is focused. Up from body row 0 lands here; Up from
        here goes to the title row; Down from here returns to body 0."""
        return self.selected == -1

    @property
    def on_title_row(self) -> bool:
        """True when the Idlegit title row is focused — the topmost
        navigation level. Tab here opens the workspaces picker; Up
        wraps to the bottom of the body."""
        return self.selected == -2

    def selectable_rows(self) -> List[Tuple]:
        """Flat selectable body list: ('repo', repo, None) and
        ('child', parent_repo, child_ref) entries, interleaved in
        display order. The workspace title row (selected = -1) is
        navigated separately and isn't part of this list."""
        rows: List[Tuple] = []
        for repo in self.repos:
            rows.append(("repo", repo, None))
            for child in repo.children:
                rows.append(("child", repo, child))
        return rows

    @property
    def total_rows(self) -> int:
        return sum(1 + len(r.children) for r in self.repos)

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
