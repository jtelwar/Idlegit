"""State-owned records for the task detail modal projection."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..runtime.tasks import Task


@dataclass
class TaskActionMenuItem:
    """One row in the task-detail modal action list."""

    id: str
    label: str
    enabled: bool = True
    reason: str = ""


@dataclass
class TaskActionMenu:
    """Task detail modal state and action projection."""

    task: Task
    items: List[TaskActionMenuItem] = field(default_factory=list)
    selected: int = 0
    sub_picker_open: bool = False
    sub_picker_options: List[str] = field(default_factory=list)
    sub_picker_selected: int = 0
    scroll: int = 0
    pending_child: Task | None = None
    pending_workflow: str | None = None
