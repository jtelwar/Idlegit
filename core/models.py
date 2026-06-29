"""Temporary compatibility re-exports for moved state records.

Production code should import from the owning ``core.state`` module directly.
This module no longer defines dataclasses or owns application state.
"""
from __future__ import annotations

from .state.action_menu import (
    ActionMenu,
    ActionMenuItem,
    ActionSubmenuFrame,
    CommitEntry,
    FileEntry,
)
from .state.action_target import TargetState
from .state.app import State
from .state.app_menu import AppMenu, AppMenuRow
from .state.clone import CloneModal
from .state.edit_buffers import CommitMsgEditor
from .state.pickers import BranchPicker, RemoteBranchPicker, WorkflowPicker
from .state.prompts import (
    AlignHeadsPrompt,
    BranchNamePrompt,
    DetachedRecoveryPrompt,
    ResetPrompt,
)
from .state.remotes import RemotesModal, RemoteRow
from .state.repos import ChildRef, Repo, WorkflowInfo, WorkflowInput
from .state.review import (
    FileChange,
    LFSCandidate,
    ReviewBlock,
    ThenRunSelector,
    WorkflowToggle,
)
from .state.review_drafts import ReviewDraftRecord, ReviewDraftRegistry
from .state.safe_merge import (
    ConflictFile,
    ConflictHunk,
    MergeSide,
    SafeMergeScreen,
)
from .state.smart_sync import SmartSyncCheckout
from .state.ssh_keygen import SshKeygenModal
from .state.store import StateStore
from .state.task_detail import TaskActionMenu, TaskActionMenuItem
from .runtime.tasks import TASK_AUTO_REMOVE_PROGRESS_SECONDS, Task, Tasks
from .state.views import (
    CommitViewModal,
    DiffViewer,
    HelpPage,
    HelpScreen,
    TaskLogViewer,
    ViewLoadRecord,
    ViewLoadRegistry,
)
from .state.workspaces import (
    SubtreeSpec,
    Workspace,
    WorkspaceCreator,
    WorkspaceDraft,
    WorkspaceMenu,
    WorkspaceMenuRow,
    WorkspaceSwitcher,
)
from .state.workflows import (
    WorkflowFollowupRecord,
    WorkflowFollowupRegistry,
    WorkflowRunRecord,
    WorkflowRunRegistry,
)

__all__ = [
    "ActionMenu",
    "ActionMenuItem",
    "ActionSubmenuFrame",
    "AlignHeadsPrompt",
    "AppMenu",
    "AppMenuRow",
    "BranchNamePrompt",
    "BranchPicker",
    "ChildRef",
    "CloneModal",
    "CommitEntry",
    "CommitMsgEditor",
    "CommitViewModal",
    "ConflictFile",
    "ConflictHunk",
    "DetachedRecoveryPrompt",
    "DiffViewer",
    "FileChange",
    "FileEntry",
    "HelpPage",
    "HelpScreen",
    "LFSCandidate",
    "MergeSide",
    "RemoteBranchPicker",
    "RemoteRow",
    "RemotesModal",
    "Repo",
    "ResetPrompt",
    "ReviewBlock",
    "ReviewDraftRecord",
    "ReviewDraftRegistry",
    "SafeMergeScreen",
    "SmartSyncCheckout",
    "SshKeygenModal",
    "State",
    "StateStore",
    "SubtreeSpec",
    "TASK_AUTO_REMOVE_PROGRESS_SECONDS",
    "TargetState",
    "Task",
    "TaskActionMenu",
    "TaskActionMenuItem",
    "TaskLogViewer",
    "Tasks",
    "ThenRunSelector",
    "ViewLoadRecord",
    "ViewLoadRegistry",
    "WorkflowFollowupRecord",
    "WorkflowFollowupRegistry",
    "WorkflowInfo",
    "WorkflowInput",
    "WorkflowPicker",
    "WorkflowRunRecord",
    "WorkflowRunRegistry",
    "WorkflowToggle",
    "Workspace",
    "WorkspaceCreator",
    "WorkspaceDraft",
    "WorkspaceMenu",
    "WorkspaceMenuRow",
    "WorkspaceSwitcher",
]
