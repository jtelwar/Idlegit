"""Temporary compatibility exports for runtime task projections.

Task projection ownership lives in :mod:`core.runtime.tasks`. This shell exists
only while Phase 2 moves old imports onto the runtime package.
"""
from __future__ import annotations

from core.runtime.tasks import TASK_AUTO_REMOVE_PROGRESS_SECONDS, Task, Tasks

__all__ = [
    "TASK_AUTO_REMOVE_PROGRESS_SECONDS",
    "Task",
    "Tasks",
]
