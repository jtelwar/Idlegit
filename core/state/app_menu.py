"""State-owned records for the global app menu."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class AppMenuRow:
    """One row of the global app menu."""

    label: str
    attr_name: str
    kind: str


@dataclass
class AppMenu:
    """Global application menu state."""

    rows: List[AppMenuRow] = field(default_factory=list)
    selected: int = 0
    scroll: int = 0
    update_check: str = "idle"
    latest_version: str = ""
    update_check_error: str = ""
    update_check_rendered: str = "idle"
    ssh_status: str = "checking"
    ssh_keys: str = "checking"
    ssh_tools_missing: List[str] = field(default_factory=list)
    ssh_status_checking: bool = False
    task_log_size: str = "checking"
    task_log_path_status: str = ""
    task_log_checking: bool = False
