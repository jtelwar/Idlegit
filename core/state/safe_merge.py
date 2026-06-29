"""State-owned safe-merge conflict resolution records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .repos import ChildRef, Repo
from ..runtime.tasks import Task
from .workspaces import SubtreeSpec


@dataclass
class MergeSide:
    """Rich description of one side of a merge."""

    role: str = "ours"
    branch: str = ""
    short_sha: str = ""
    subject: str = ""
    remote: str = ""


@dataclass
class ConflictHunk:
    """One conflict marker region inside a conflicted text file."""

    ours: List[str] = field(default_factory=list)
    theirs: List[str] = field(default_factory=list)
    base: List[str] = field(default_factory=list)
    choice: str = ""


@dataclass
class ConflictFile:
    """A single conflicted path plus the plan for rebuilding it."""

    path: str = ""
    kind: str = "text"
    parts: List[Tuple[str, object]] = field(default_factory=list)
    hunks: List[ConflictHunk] = field(default_factory=list)
    whole_choice: str = ""
    ours_present: bool = True
    theirs_present: bool = True
    note: str = ""


@dataclass
class SafeMergeScreen:
    """State for the full-screen safe-merge conflict resolver."""

    target_label: str = ""
    target_path: Path = field(default_factory=Path)
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    merge_ref: str = ""
    ours: MergeSide = field(default_factory=MergeSide)
    theirs: MergeSide = field(default_factory=MergeSide)
    files: List[ConflictFile] = field(default_factory=list)
    decisions: List[Tuple[int, int]] = field(default_factory=list)
    focus: int = 0
    scroll: int = 0
    backup_stash_name: str = ""
    phase: str = "preparing"
    status_note: str = ""
    error: str = ""
    commit_sha: str = ""
    commit_subject: str = ""
    is_tracked_submodule: bool = False
    confirm_push: bool = True
    confirm_remove_stash: bool = False
    confirm_focus: int = 0
    header_task: Optional[Task] = None
    header_terminal: bool = False
    repo_locked: bool = False
    child_locked: bool = False
    repo_refresh_claim: Optional[object] = None
    child_refresh_claim: Optional[object] = None
    snapshot_repos: List[Repo] = field(default_factory=list)
    snapshot_subtrees: List[SubtreeSpec] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
