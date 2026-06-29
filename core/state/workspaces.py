"""State-owned workspace records and workspace modal state."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class SubtreeSpec:
    """One configured subtree relation inside a workspace."""

    name: str
    parent: str
    source: str
    prefix: str


@dataclass
class Workspace:
    """One configured workspace and its active runtime cache."""

    name: str
    folders: List[Path]
    overrides: dict = field(default_factory=dict)
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    cached_repos: list = field(default_factory=list)
    fs_watch_ignore: List[str] = field(default_factory=list)
    ephemeral: bool = False

    @property
    def display_name(self) -> str:
        """Name as shown in UI surfaces."""
        return f"[{self.name}]" if self.ephemeral else self.name


@dataclass
class WorkspaceSwitcher:
    """Scrollable workspace picker state."""

    selected: int = 0
    scroll: int = 0


@dataclass
class WorkspaceDraft:
    """One editable workspace folder path and latest async check result."""

    path_text: str = ""
    last_checked: str = ""
    repo_count: int = -1
    error: str = ""
    checking: bool = False


@dataclass
class WorkspaceCreator:
    """First-run / new-workspace dialogue state."""

    drafts: List[WorkspaceDraft] = field(default_factory=list)
    selected: int = 0
    field_cursor: int = 0
    title: str = ""
    intro: str = ""
    result: Optional[List[Workspace]] = None


@dataclass
class WorkspaceMenuRow:
    """One row in the per-workspace settings modal."""

    label: str
    attr_name: str
    kind: str
    min_value: int = 0
    max_value: int = 999
    step: int = 1
    hint_text: str = ""


@dataclass
class WorkspaceMenu:
    """Per-workspace settings modal state."""

    rows: List[WorkspaceMenuRow] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    editing: bool = False
    edit_buffer: str = ""
    edit_cursor: int = 0
    path_drafts: List[WorkspaceDraft] = field(default_factory=list)
