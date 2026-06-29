"""Task-detail selectors and labels."""
from __future__ import annotations

from urllib.parse import urlparse

from core.runtime.tasks import Task


def dispatchable_targets(repo) -> list[str]:
    if repo is None:
        return []
    return [
        workflow.name
        for workflow in repo.workflows
        if workflow.dispatchable and not workflow.state.startswith("disabled")
    ]


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m {secs}s"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def is_terminal(status: str) -> bool:
    return status in ("ok", "fail", "warn")


def status_label(task: Task) -> str:
    if task.status == "running":
        return "running"
    if task.status == "pending":
        return "pending"
    if task.status == "ok":
        return "✓ ok"
    if task.status == "fail":
        return "✗ failed"
    return "⚠ warn"


def is_safe_browser_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
