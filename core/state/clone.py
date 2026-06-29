"""State-owned records for the clone modal."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class CloneModal:
    """Modal state for cloning a remote into the active workspace."""

    workspace_name: str
    workspace_folders: List[Path] = field(default_factory=list)
    url: str = ""
    dest_text: str = ""
    branch: str = ""
    recurse_submodules: bool = True
    selected: int = 0
    edit_field: str = ""
    edit_pre_value: str = ""
    cloning: bool = False
    error: str = ""
