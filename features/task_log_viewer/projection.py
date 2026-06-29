"""Task log viewer projection helpers."""
from __future__ import annotations

from core.runtime.tasks import Task

KEY_ESC_LABEL = "Esc"
KEY_UP_DOWN_LABEL = "↑/↓"


def task_log_viewer_hint_specs() -> list[tuple[str, str]]:
    return [
        (KEY_UP_DOWN_LABEL, "scroll"),
        (KEY_ESC_LABEL, "close"),
    ]


def task_status_label(task: Task) -> str:
    if task.status == "running":
        return "running"
    if task.status == "pending":
        return "pending"
    if task.status == "ok":
        return "✓ ok"
    if task.status == "fail":
        return "✗ failed"
    return "⚠ warn"
