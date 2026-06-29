"""Runtime-owned task projection rows."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


def _monotonic() -> float:
    """Indirected so tests can stub it if needed."""
    return time.monotonic()


_TERMINAL_STATUSES = frozenset({"ok", "fail", "warn"})
TASK_AUTO_REMOVE_PROGRESS_SECONDS = 3.0


@dataclass(eq=False)
class Task:
    """One unit of background work shown in the right-hand sidebar.

    Task rows are presentation. Execution state is owned by runtime
    registries; a task row carries only display state plus durable subject ids
    that UI surfaces can use to look up the authoritative runtime record.
    """

    label: str
    # running / pending / ok / fail / warn. "pending" is non-terminal
    # like running, but signals the task is waiting on something else.
    status: str = "running"
    message: str = ""
    started_at: float = field(default_factory=_monotonic)
    finished_at: Optional[float] = None
    parent: Optional["Task"] = None
    subject_kind: str = ""
    subject_id: str = ""


class Tasks:
    """Thread-safe projection list of background-work task rows."""

    def __init__(self) -> None:
        self.items: List[Task] = []
        self.lock = threading.Lock()
        self.on_finished: Optional[Callable[[Task], None]] = None
        self.on_change: Optional[Callable[[], None]] = None

    def add(self, label: str, parent: Optional[Task] = None) -> Task:
        with self.lock:
            task = Task(label=label, parent=parent)
            self.items.append(task)
        self._notify_change()
        return task

    def children_of(self, task: Task) -> List[Task]:
        """Every task whose `parent is task`."""
        with self.lock:
            return [item for item in self.items if item.parent is task]

    def update(self, task: Task, status: str, message: str = "") -> None:
        fire_finished = False
        with self.lock:
            was_terminal = task.status in _TERMINAL_STATUSES
            task.status = status
            if message:
                task.message = message
            if not was_terminal and status in _TERMINAL_STATUSES:
                task.finished_at = _monotonic()
                fire_finished = True
        if fire_finished and self.on_finished is not None:
            try:
                self.on_finished(task)
            except Exception:  # noqa: BLE001
                pass
        self._notify_change()

    def set_label(self, task: Task, label: str) -> None:
        """Mutate a row label in place for long-running workers."""
        with self.lock:
            task.label = label
        self._notify_change()

    def clear_message(self, task: Task) -> None:
        """Explicitly clear a task's message."""
        with self.lock:
            task.message = ""
        self._notify_change()

    def snapshot(self) -> List[Task]:
        with self.lock:
            return list(self.items)

    def prune_completed(self) -> None:
        """Drop terminal tasks while preserving active and pending rows."""
        with self.lock:
            kept = [
                task for task in self.items
                if task.status in ("running", "pending")
            ]
            changed = len(kept) != len(self.items)
            self.items = kept
        if changed:
            self._notify_change()

    def remove(self, task: Task) -> bool:
        """Remove a specific task row by identity."""
        with self.lock:
            try:
                self.items.remove(task)
                removed = True
            except ValueError:
                removed = False
        if removed:
            self._notify_change()
        return removed

    def prune_aged(self, max_age_seconds: float) -> int:
        """Remove successful tasks after wait and progress windows."""
        if max_age_seconds < 0:
            return 0
        now = _monotonic()
        remove_after = max_age_seconds + TASK_AUTO_REMOVE_PROGRESS_SECONDS
        with self.lock:
            before = len(self.items)
            kept = [
                task for task in self.items
                if task.status == "running"
                or task.status != "ok"
                or task.finished_at is None
                or (now - task.finished_at) < remove_after
            ]
            self.items = kept
            removed = before - len(self.items)
        if removed:
            self._notify_change()
        return removed

    def has_running(self) -> bool:
        return self.has_visible_activity()

    def has_visible_activity(self) -> bool:
        """True when a non-terminal task row should keep the UI ticking."""
        with self.lock:
            return any(
                task.status in ("running", "pending")
                for task in self.items
            )

    def has_pending_followups(self) -> bool:
        """True when at least one task row is pending on another action."""
        with self.lock:
            return any(task.status == "pending" for task in self.items)

    def has_pending_auto_remove(self, max_age_seconds: float) -> bool:
        """True when a successful task is in its removal animation."""
        if max_age_seconds < 0:
            return False
        now = _monotonic()
        remove_after = max_age_seconds + TASK_AUTO_REMOVE_PROGRESS_SECONDS
        with self.lock:
            for task in self.items:
                if task.status != "ok" or task.finished_at is None:
                    continue
                elapsed = now - task.finished_at
                if max_age_seconds <= elapsed < remove_after:
                    return True
            return False

    def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass
