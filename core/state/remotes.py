"""State-owned records for the remotes modal."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .repos import ChildRef, Repo


@dataclass
class RemoteRow:
    """One editable remote row in the remotes modal."""

    original_name: str = ""
    original_url: str = ""
    name: str = ""
    url: str = ""
    to_delete: bool = False
    is_new: bool = False


@dataclass
class RemotesModal:
    """Modal state for managing a focused repo's remotes."""

    target_label: str
    target_path: Path
    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    rows: List[RemoteRow] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    edit_field: str = ""
    edit_pre_value: str = ""
    confirming: bool = False
