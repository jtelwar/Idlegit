"""State-owned blocking prompt records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .repos import ChildRef, Repo


@dataclass
class BranchNamePrompt:
    """Sub-modal for typing a safe branch name."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    typed: str = ""
    default_name: str = ""
    head_sha: str = ""
    mode: str = "save_head"
    current_branch: str = ""


@dataclass
class DetachedRecoveryPrompt:
    """Worker-blocking prompt for safely parking a detached HEAD."""

    target_label: str
    head_sha: str
    target_branch: str
    n_extra: int
    can_ff: bool
    chosen_action: Optional[str] = None
    result_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class ResetPrompt:
    """Sub-modal for choosing a soft-reset count."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    ahead: int = 0
    count: int = 0
    typed: str = ""


@dataclass
class AlignHeadsPrompt:
    """Worker-blocking prompt for choosing the branch to push an aligned HEAD."""

    canonical_name: str
    winner_parent_name: str
    winner_sha: str
    branches: List[str] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    chosen_branch: Optional[str] = None
    result_event: threading.Event = field(default_factory=threading.Event)
