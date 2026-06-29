"""Root application state composition."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..runtime.jobs import JobRegistry
from ..runtime.leases import LeaseManager
from ..ui_events import UiEvents
from .action_menu import ActionMenu
from .app_menu import AppMenu
from .clone import CloneModal
from .edit_buffers import CommitMsgEditor
from .pickers import BranchPicker, RemoteBranchPicker, WorkflowPicker
from .prompts import (
    AlignHeadsPrompt,
    BranchNamePrompt,
    DetachedRecoveryPrompt,
    ResetPrompt,
)
from .remotes import RemotesModal
from .repos import ChildRef, Repo
from .review_drafts import ReviewDraftRegistry
from .safe_merge import SafeMergeScreen
from .ssh_keygen import SshKeygenModal
from .store import StateStore
from .task_detail import TaskActionMenu
from ..runtime.tasks import Tasks
from .views import (
    CommitViewModal,
    DiffViewer,
    HelpScreen,
    TaskLogViewer,
    ViewLoadRegistry,
)
from .workspaces import (
    SubtreeSpec,
    Workspace,
    WorkspaceCreator,
    WorkspaceMenu,
    WorkspaceSwitcher,
)
from .workflows import WorkflowFollowupRegistry, WorkflowRunRegistry


_DEFAULT_SUGGEST = 5
_DEFAULT_LFS_WARN_BYTES = 100 * 1024 * 1024
_DEFAULT_BRANCH_DISPLAY_MAX = 12
_DEFAULT_NAME_DISPLAY_MAX = 40
_DEFAULT_CHILD_NAME_DISPLAY_MAX = -1
_DEFAULT_TASK_NAME_DISPLAY_MAX = 16
_DEFAULT_TRUNCATION_MODE = "middle"
_DEFAULT_MAX_VISIBLE_REPO_ROWS = 0
_DEFAULT_TASKS_MIN_WIDTH_PERCENT = 0.2
_DEFAULT_TASKS_MAX_WIDTH_PERCENT = 0.5
_DEFAULT_TRACK_ACTIONS = True
_DEFAULT_ACTIONS_POLL_SECONDS = 5.0
_DEFAULT_AUTO_REMOVE_COMPLETED_AFTER = 6.0
_DEFAULT_PERIODIC_REFRESH_SECONDS = 60.0
_DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW = 480


@dataclass
class State:
    """Root application state composition.

    Phase 1A moves the root owner out of ``core.models``. Later Phase 1 slices
    remove the remaining mirrored ``repos`` surface and make ``StateStore`` the
    only repo/child membership and lifecycle source.
    """

    repos: List[Repo]
    workspace_name: str
    suggest_added: int = _DEFAULT_SUGGEST
    suggest_updated: int = _DEFAULT_SUGGEST
    suggest_deleted: int = _DEFAULT_SUGGEST
    lfs_warn_bytes: int = _DEFAULT_LFS_WARN_BYTES
    branch_display_max: int = _DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = _DEFAULT_NAME_DISPLAY_MAX
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
    max_commit_message_length_in_review: int = (
        _DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW)
    workspaces: List[Workspace] = field(default_factory=list)
    active_workspace_index: int = 0
    base_config: Optional[object] = None
    selected: int = 0
    body_scroll: int = 0
    auto_stage: bool = True
    auto_push: bool = True
    align_heads: bool = True
    auto_ff: bool = True
    prompt_for_branch: bool = True
    prevent_smart_sync_silent_merge: bool = False
    auto_push_submodule_parent: bool = True
    auto_remove_backup_stash_after_merge: bool = False
    task_log_enabled: bool = False
    task_log_path: Path = field(default_factory=lambda: Path("tasks.log"))
    task_log_max_lines: int = 0
    job_registry: JobRegistry = field(default_factory=JobRegistry)
    workflow_runs: WorkflowRunRegistry = field(default_factory=WorkflowRunRegistry)
    workflow_followups: WorkflowFollowupRegistry = field(
        default_factory=WorkflowFollowupRegistry)
    review_drafts: ReviewDraftRegistry = field(default_factory=ReviewDraftRegistry)
    view_loads: ViewLoadRegistry = field(default_factory=ViewLoadRegistry)
    leases: LeaseManager = field(default_factory=LeaseManager)
    store: StateStore = field(default_factory=StateStore)
    ui_events: UiEvents = field(default_factory=UiEvents)
    auto_refresh_on_fs_change: bool = False
    auto_refresh_debounce_ms: int = 400
    periodic_refresh_seconds: float = _DEFAULT_PERIODIC_REFRESH_SECONDS
    fetch_on_manual_refresh: bool = False
    fs_watch_ignore: List[str] = field(default_factory=list)
    in_review: bool = False
    in_safe_merge: bool = False
    safe_merge: Optional[SafeMergeScreen] = None
    tasks: Tasks = field(default_factory=Tasks)
    spinner_frame: int = 0
    field_cursor: int = 0
    focused_panel: str = "repos"
    task_selected: int = 0
    task_scroll: int = 0
    action_menu: Optional[ActionMenu] = None
    branch_picker: Optional[BranchPicker] = None
    remote_branch_picker: Optional[RemoteBranchPicker] = None
    branch_name_prompt: Optional[BranchNamePrompt] = None
    reset_prompt: Optional[ResetPrompt] = None
    workflow_picker: Optional[WorkflowPicker] = None
    align_heads_prompt: Optional[AlignHeadsPrompt] = None
    detached_recovery_prompt: Optional[DetachedRecoveryPrompt] = None
    commit_msg_editor: Optional[CommitMsgEditor] = None
    help_screen: Optional[HelpScreen] = None
    diff_viewer: Optional[DiffViewer] = None
    task_action_menu: Optional[TaskActionMenu] = None
    task_log_viewer: Optional[TaskLogViewer] = None
    workspace_menu: Optional[WorkspaceMenu] = None
    workspace_switcher: Optional[WorkspaceSwitcher] = None
    workspace_creator: Optional[WorkspaceCreator] = None
    app_menu: Optional[AppMenu] = None
    remotes_modal: Optional[RemotesModal] = None
    clone_modal: Optional[CloneModal] = None
    ssh_keygen_modal: Optional[SshKeygenModal] = None
    auto_start_ssh_agent: bool = True
    commit_view_modal: Optional[CommitViewModal] = None

    def __post_init__(self) -> None:
        self.tasks.on_change = self.ui_events.notify
        self.job_registry.on_change = self.ui_events.notify
        self.workflow_runs.on_change = self.ui_events.notify
        self.workflow_followups.on_change = self.ui_events.notify
        self.review_drafts.on_change = self.ui_events.notify
        self.view_loads.on_change = self.ui_events.notify
        self.leases.on_change = self.ui_events.notify
        self.store.on_change = self.ui_events.notify
        self.store.replace_workspace(
            name=self.workspace_name,
            folders=self.active_folders,
            repos=self.repos,
            notify=False,
        )

    @property
    def active_workspace(self) -> Optional[Workspace]:
        if not self.workspaces:
            return None
        idx = max(0, min(self.active_workspace_index, len(self.workspaces) - 1))
        return self.workspaces[idx]

    @property
    def active_folders(self) -> List[Path]:
        ws = self.active_workspace
        if ws is None:
            return []
        return list(ws.folders)

    @property
    def on_workspace_row(self) -> bool:
        return self.selected == -1

    @property
    def on_title_row(self) -> bool:
        return self.selected == -2

    def selectable_rows(self) -> List[Tuple]:
        from .selectors import selectable_body_rows
        return selectable_body_rows(self)

    def replace_repos(
            self,
            repos: List[Repo],
            *,
            workspace: Optional[Workspace] = None,
            notify: bool = True,
    ) -> None:
        self.repos = repos
        ws = workspace if workspace is not None else self.active_workspace
        name = ws.name if ws is not None else self.workspace_name
        folders = ws.folders if ws is not None else self.active_folders
        self.store.replace_workspace(
            name=name,
            folders=folders,
            repos=repos,
            notify=notify,
        )

    def body_focus_key(self) -> Optional[Tuple]:
        if self.selected < 0:
            return None
        rows = self.selectable_rows()
        if self.selected >= len(rows):
            return None
        kind, parent, child = rows[self.selected]
        if kind == "repo":
            return ("repo", parent.path)
        return ("child", parent.path, child.nested_path.resolve())

    def restore_body_focus(self, key: Optional[Tuple]) -> None:
        if key is None:
            return
        rows = self.selectable_rows()
        for i, row in enumerate(rows):
            kind, parent, child = row
            if key[0] == "repo" and kind == "repo" and parent.path == key[1]:
                self.selected = i
                return
            if (key[0] == "child" and kind == "child"
                    and parent.path == key[1]
                    and child.nested_path.resolve() == key[2]):
                self.selected = i
                return
        if rows:
            self.selected = max(0, min(self.selected, len(rows) - 1))
        else:
            self.selected = 0

    @property
    def total_rows(self) -> int:
        from .selectors import total_body_rows
        return total_body_rows(self)

    def task_repo_label(self, repo: Optional[Repo]) -> str:
        if repo is None:
            return ""
        text = repo.display_name
        max_len = self.task_name_display_max
        mode = self.task_name_truncation
        if max_len <= 0 or len(text) <= max_len:
            return text
        if max_len == 1:
            return "..."
        keep = max_len - 1
        if mode == "start":
            return "..." + text[-keep:]
        if mode == "end":
            return text[:keep] + "..."
        head = (keep + 1) // 2
        tail = keep - head
        return text[:head] + "..." + text[-tail:]

    @property
    def current_repo(self) -> Optional[Repo]:
        from .selectors import focused_repo
        return focused_repo(self)

    @property
    def current_child(self) -> Optional[Tuple[Repo, ChildRef]]:
        from .selectors import focused_child
        return focused_child(self)

    @property
    def has_messages(self) -> bool:
        from .selectors import has_commit_messages
        return has_commit_messages(self)
