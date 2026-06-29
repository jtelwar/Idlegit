"""State-owned action target snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TargetState:
    """Snapshot of a working tree's state when action-menu actions open."""

    branch: str
    upstream: Optional[str]
    ahead: int
    behind: int
    has_origin: bool
    merging: bool
    dirty: bool
    recent_commits: List[str]
