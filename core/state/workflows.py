"""State-owned workflow run and follow-up registries."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from ..runtime.tasks import Task


@dataclass
class WorkflowRunRecord:
    """Authoritative workflow-run data linked to one or more task rows."""

    record_id: str = ""
    repo: object | None = None
    slug: str = ""
    run_id: Optional[int] = None
    workflow_name: str = ""
    job_id: Optional[int] = None
    run_url: str = ""
    latest_view: Optional[dict] = None


def _repo_subject_id(repo: object | None) -> str:
    if repo is None:
        return "unknown"
    return str(getattr(repo, "path", "unknown"))


def _workflow_run_record_id(
        slug: str = "",
        run_id: Optional[int] = None,
        job_id: Optional[int] = None,
        **_unused: object) -> str:
    run_part = str(run_id) if run_id is not None else "pending"
    if job_id is None:
        return f"workflow-run:{slug}:{run_part}:run"
    return f"workflow-run:{slug}:{run_part}:job:{job_id}"


class WorkflowRunRegistry:
    """Workflow-run records keyed by durable presentation subject ids."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, WorkflowRunRecord] = {}
        self.on_change = on_change

    def create_for_task(self, task: Task, **fields) -> WorkflowRunRecord:
        record_id = _workflow_run_record_id(**fields)
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                record = WorkflowRunRecord(record_id=record_id)
                self._records[record_id] = record
            for key, value in fields.items():
                setattr(record, key, value)
            task.subject_kind = "workflow-run"
            task.subject_id = record_id
        self._notify_change()
        return record

    def update(self, record_id: str, **fields) -> Optional[WorkflowRunRecord]:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None
            for key, value in fields.items():
                setattr(record, key, value)
        self._notify_change()
        return record

    def get(self, record_id: str) -> Optional[WorkflowRunRecord]:
        with self._lock:
            return self._records.get(record_id)

    def record_for_task(self, task: Task) -> Optional[WorkflowRunRecord]:
        if task.subject_kind != "workflow-run" or not task.subject_id:
            return None
        return self.get(task.subject_id)

    def remove(self, record_id: str) -> None:
        removed = False
        with self._lock:
            if record_id in self._records:
                del self._records[record_id]
                removed = True
        if removed:
            self._notify_change()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class WorkflowFollowupRecord:
    """Authoritative pending then-run data linked to a presentation row."""

    record_id: str = ""
    repo: object | None = None
    parent_workflow: str = ""
    target: str = ""


def _workflow_followup_record_id(
        repo: object | None = None,
        parent_workflow: str = "",
        **_unused: object) -> str:
    return f"workflow-followup:{_repo_subject_id(repo)}:{parent_workflow}"


class WorkflowFollowupRegistry:
    """Pending workflow follow-up records keyed by durable subject ids."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, WorkflowFollowupRecord] = {}
        self.on_change = on_change

    def create_for_task(self, task: Task, **fields) -> WorkflowFollowupRecord:
        record_id = _workflow_followup_record_id(**fields)
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                record = WorkflowFollowupRecord(record_id=record_id)
                self._records[record_id] = record
            for key, value in fields.items():
                setattr(record, key, value)
            task.subject_kind = "workflow-followup"
            task.subject_id = record_id
        self._notify_change()
        return record

    def update(
            self, record_id: str,
            **fields) -> Optional[WorkflowFollowupRecord]:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None
            for key, value in fields.items():
                setattr(record, key, value)
        self._notify_change()
        return record

    def get(self, record_id: str) -> Optional[WorkflowFollowupRecord]:
        with self._lock:
            return self._records.get(record_id)

    def record_for_task(self, task: Task) -> Optional[WorkflowFollowupRecord]:
        if task.subject_kind != "workflow-followup" or not task.subject_id:
            return None
        return self.get(task.subject_id)

    def remove(self, record_id: str) -> None:
        removed = False
        with self._lock:
            if record_id in self._records:
                del self._records[record_id]
                removed = True
        if removed:
            self._notify_change()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass
