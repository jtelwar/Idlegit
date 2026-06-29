"""State-owned review screen projection records."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .repos import ChildRef, Repo


@dataclass
class LFSCandidate:
    """One large file candidate shown in a review block."""

    repo: Repo
    path: str
    size_str: str
    line_index: int = -1
    track: bool = False


@dataclass
class WorkflowToggle:
    """Stable focusable projection for one workflow tracking row."""

    repo: Repo
    workflow_name: str
    draft_id: str = ""
    line_index: int = -1


@dataclass
class ThenRunSelector:
    """A 'then run' chain selector on the review screen."""

    repo: Repo
    after_workflow: str
    draft_id: str = ""
    line_index: int = -1


@dataclass
class FileChange:
    """One classified working-tree change used for commit-message suggestion."""

    path: str
    kind: str
    weight: float = 0.0


@dataclass
class ReviewBlock:
    """One commit target on the two-panel review screen."""

    label: str
    branch: str
    target_path: Path
    draft_id: str = ""
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    merging: bool = False
    conflict_paths: list[str] = field(default_factory=list)
    has_origin: bool = False
    upstream: Optional[str] = None
    siblings_summary: str = ""
    auto_stage: bool = True
    is_child: bool = False
    threshold_mb: int = 0
    lfs_candidates: list[LFSCandidate] = field(default_factory=list)
    file_selected: int = 0
    file_scroll: int = 0
    toolbar_focus: int = -1
