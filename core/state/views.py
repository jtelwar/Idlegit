"""State-owned async view loader records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..runtime.tasks import Task


@dataclass
class ViewLoadRecord:
    """State-owned async load result for a modal/view fragment."""

    load_id: str
    lines: List[str] = field(default_factory=list)
    loading: bool = True
    error: str = ""
    details: Dict[str, str] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class ViewLoadRegistry:
    """Authoritative async view-loader state keyed by durable load id."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, ViewLoadRecord] = {}
        self._cancelled: Set[str] = set()
        self.on_change = on_change

    def create(self, load_id: str) -> ViewLoadRecord:
        with self._lock:
            self._cancelled.discard(load_id)
            record = ViewLoadRecord(load_id=load_id)
            self._records[load_id] = record
        self._notify_change()
        return record

    def get(self, load_id: str) -> Optional[ViewLoadRecord]:
        with self._lock:
            return self._records.get(load_id)

    def get_or_create(self, load_id: str) -> ViewLoadRecord:
        record = self.get(load_id)
        if record is not None:
            return record
        return self.create(load_id)

    def finish(
            self,
            load_id: str,
            lines: List[str],
            error: str = "",
            details: Optional[Dict[str, str]] = None,
    ) -> ViewLoadRecord:
        with self._lock:
            if load_id in self._cancelled:
                record = ViewLoadRecord(load_id=load_id, loading=False)
                record.cancel_event.set()
                return record
            record = self._records.get(load_id)
            if record is None:
                record = ViewLoadRecord(load_id=load_id)
                self._records[load_id] = record
            record.lines = list(lines)
            record.error = error
            if details is not None:
                record.details = dict(details)
            record.loading = False
        self._notify_change()
        return record

    def fail(self, load_id: str, message: str) -> ViewLoadRecord:
        return self.finish(load_id, [message], error=message)

    def cancel(self, load_id: str) -> None:
        with self._lock:
            self._cancelled.add(load_id)
            record = self._records.get(load_id)
            if record is None:
                return
            record.cancel_event.set()
            record.loading = False
        self._notify_change()

    def snapshot(self, load_id: str) -> Tuple[List[str], bool, str]:
        with self._lock:
            record = self._records.get(load_id)
            if record is None:
                return [], True, ""
            return list(record.lines), record.loading, record.error

    def details(self, load_id: str) -> Dict[str, str]:
        with self._lock:
            record = self._records.get(load_id)
            if record is None:
                return {}
            return dict(record.details)

    def is_cancelled(self, load_id: str) -> bool:
        with self._lock:
            record = self._records.get(load_id)
            return bool(
                load_id in self._cancelled
                or (record is not None and record.cancel_event.is_set())
            )

    def any_loading(self, load_ids: List[str]) -> bool:
        with self._lock:
            for load_id in load_ids:
                record = self._records.get(load_id)
                if record is not None and record.loading:
                    return True
            return False

    def remove_many(self, load_ids: List[str]) -> None:
        changed = False
        with self._lock:
            for load_id in load_ids:
                record = self._records.pop(load_id, None)
                self._cancelled.add(load_id)
                if record is not None:
                    record.cancel_event.set()
                    changed = True
        if changed:
            self._notify_change()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()


@dataclass
class DiffViewer:
    """State for a scrollable diff/log/blame viewer."""

    file_path: str
    target_path: Path
    label: str
    untracked: bool = False
    commit_sha: str = ""
    active_tab: str = "diff"
    diff_load_id: str = ""
    log_load_id: str = ""
    blame_load_id: str = ""
    scroll: int = 0
    log_scroll: int = 0
    blame_scroll: int = 0


@dataclass
class TaskLogViewer:
    """Scrollable read-only viewer for a GitHub Actions run or job log."""

    task: Task
    slug: str
    run_id: int
    load_id: str
    job_id: Optional[int] = None
    workflow_name: str = ""
    only_failed: bool = False
    scroll: int = 0


@dataclass
class CommitViewModal:
    """State for the action-menu commit detail sub-view."""

    target_label: str
    target_path: Path
    sha: str
    subject: str = ""
    body: str = ""
    author: str = ""
    date: str = ""
    tags: List[str] = field(default_factory=list)
    files: List[object] = field(default_factory=list)
    tags_load_id: str = ""
    details_load_id: str = ""
    files_load_id: str = ""
    reflog_entries: List[str] = field(default_factory=list)
    reflog_load_id: str = ""
    section: str = "actions"
    action_selected: int = 0
    active_tab: str = "changes"
    file_selected: int = 0
    file_scroll: int = 0
    reflog_selected: int = 0
    reflog_scroll: int = 0
    edit_field: str = ""
    edit_typed: str = ""
    confirm_message: str = ""
    confirm_action: str = ""
    confirm_args: Dict[str, str] = field(default_factory=dict)


@dataclass
class HelpPage:
    """One bundled help page."""

    title: str
    filename: str
    body: str


@dataclass
class HelpScreen:
    """In-app help browser state."""

    pages: List[HelpPage]
    selected_page: int = 0
    content_scroll: int = 0
    focused_pane: str = "list"
