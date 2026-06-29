"""State-owned action-menu records."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .repos import ChildRef, Repo


@dataclass
class ActionMenuItem:
    """One row in the repo action menu."""

    id: str
    label: str
    enabled: bool = True
    reason: str = ""
    has_submenu: bool = False
    is_back: bool = False
    is_separator: bool = False


@dataclass
class ActionSubmenuFrame:
    """One level of submenu navigation in the action menu."""

    name: str
    label: str
    items: List[ActionMenuItem] = field(default_factory=list)
    selected: int = 0


@dataclass
class FileEntry:
    """One working-tree or commit-file row."""

    path: str
    x: str = " "
    y: str = " "
    inserted: int = 0
    deleted: int = 0
    untracked: bool = False


@dataclass
class CommitEntry:
    """One commit row in the action-menu history pane."""

    sha: str
    subject: str
    relative: str = ""


@dataclass
class ActionMenu:
    """Modal state opened with Tab on a repo or child row."""

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
    submenu_stack: List[ActionSubmenuFrame] = field(default_factory=list)
    cached_meta: dict = field(default_factory=dict)
    stash_count: int = 0
    stashes: list[tuple[str, str]] = field(default_factory=list)
    remotes_list: list[tuple[str, str]] = field(default_factory=list)
    remote_count: int = 0
    edit_field: str = ""
    edit_typed: str = ""
    edit_cursor: int = 0
    edit_pre_value: str = ""
    edit_target_id: str = ""
    edit_extra: dict[str, str] = field(default_factory=dict)
    confirm_message: str = ""
    confirm_action: str = ""
    confirm_args: dict[str, str] = field(default_factory=dict)
    pane_focus: bool = False
    pane_tab: str = "tree"
    tree_files: List[FileEntry] = field(default_factory=list)
    tree_filter: str = ""
    tree_selected: int = 0
    tree_scroll: int = 0
    commits_full: List[CommitEntry] = field(default_factory=list)
    commits_filter: str = ""
    commits_selected: int = 0
    commits_scroll: int = 0
    commits_exhausted: bool = False
    state_load_id: str = ""
    inventory_load_id: str = ""
    tree_load_id: str = ""
    commits_load_id: str = ""
