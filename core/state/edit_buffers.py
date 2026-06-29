"""State-owned edit buffer records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .repos import Repo


@dataclass
class CommitMsgEditor:
    """Large commit-message editor state for the focused row."""

    holder: object
    parent: Optional[Repo]
    label: str
    branch: str
    cursor: int = 0
    scroll: int = 0
