"""State-owned picker modal records."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .repos import ChildRef, Repo, WorkflowInfo


@dataclass
class BranchPicker:
    """Sub-modal for switching branches or choosing a branch to merge."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    load_id: str = ""
    selected: int = 0
    scroll: int = 0
    mode: str = "switch"
    create_typed: str = ""


@dataclass
class RemoteBranchPicker:
    """Sub-modal for checking out a remote-tracking branch."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    load_id: str = ""
    selected: int = 0
    scroll: int = 0


@dataclass
class WorkflowPicker:
    """Sub-modal for dispatching a GitHub Actions workflow."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    workflows: List[WorkflowInfo] = field(default_factory=list)
    branch: str = ""
    selected: int = 0
    scroll: int = 0
