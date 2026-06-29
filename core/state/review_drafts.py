"""State-owned review draft records."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ReviewDraftRecord:
    """State-owned draft data for one review target.

    Review UI blocks are render/navigation projections. The asynchronously
    loaded file list, per-file staged selections, message, push/amend toggles,
    and workflow intent live here.
    """

    draft_id: str
    files_load_id: str = ""
    files: List[object] = field(default_factory=list)
    files_loading: bool = True
    staged_paths: Dict[str, bool] = field(default_factory=dict)
    message: str = ""
    suggesting: bool = False
    push: bool = True
    amend: bool = False
    workflow_toggles: List[object] = field(default_factory=list)
    then_run_items: List[object] = field(default_factory=list)
    track_workflow: Dict[str, bool] = field(default_factory=dict)
    then_run_after_push: str = ""
    then_run_after_workflow: Dict[str, str] = field(default_factory=dict)
    then_run_params_after_push: Dict[str, str] = field(default_factory=dict)
    then_run_params_after_workflow: Dict[str, Dict[str, str]] = field(
        default_factory=dict)


class ReviewDraftRegistry:
    """Authoritative review draft file/staging state keyed by draft id."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, ReviewDraftRecord] = {}
        self.on_change = on_change

    def create(
            self,
            draft_id: str,
            *,
            message: str = "",
            push: bool = True,
            amend: bool = False,
    ) -> ReviewDraftRecord:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(
                    draft_id=draft_id, message=message,
                    push=push, amend=amend)
                self._records[draft_id] = record
            else:
                record.message = message
                record.push = push
                record.amend = amend
        self._notify_change()
        return record

    def get(self, draft_id: str) -> Optional[ReviewDraftRecord]:
        with self._lock:
            return self._records.get(draft_id)

    def get_or_create(self, draft_id: str) -> ReviewDraftRecord:
        record = self.get(draft_id)
        if record is not None:
            return record
        return self.create(draft_id)

    def set_files(
            self,
            draft_id: str,
            files: List[object],
            staged_paths: Dict[str, bool],
            *,
            loading: bool = False,
    ) -> ReviewDraftRecord:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.files = list(files)
            record.staged_paths = dict(staged_paths)
            record.files_loading = loading
        self._notify_change()
        return record

    def set_loading(self, draft_id: str, loading: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.files_loading = loading
        self._notify_change()

    def set_file_load_id(
            self, draft_id: str, load_id: str) -> ReviewDraftRecord:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.files_load_id = load_id
        self._notify_change()
        return record

    def set_staged(self, draft_id: str, path: str, staged: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.staged_paths[path] = staged
        self._notify_change()

    def set_all_staged(self, draft_id: str, staged: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            for file_entry in record.files:
                record.staged_paths[file_entry.path] = staged
        self._notify_change()

    def set_push(self, draft_id: str, push: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.push = push
        self._notify_change()

    def set_amend(self, draft_id: str, amend: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.amend = amend
        self._notify_change()

    def set_message(self, draft_id: str, message: str) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.message = message
        self._notify_change()

    def set_suggesting(self, draft_id: str, suggesting: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.suggesting = suggesting
        self._notify_change()

    def set_track_workflow(
            self, draft_id: str, workflow_name: str, tracked: bool) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            record.track_workflow[workflow_name] = tracked
        self._notify_change()

    def set_then_run(
            self, draft_id: str, after_workflow: str, value: str) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            if after_workflow:
                if value:
                    record.then_run_after_workflow[after_workflow] = value
                else:
                    record.then_run_after_workflow.pop(after_workflow, None)
                    record.then_run_params_after_workflow.pop(
                        after_workflow, None)
            else:
                record.then_run_after_push = value
                if not value:
                    record.then_run_params_after_push.clear()
        self._notify_change()

    def set_then_run_param(
            self,
            draft_id: str,
            after_workflow: str,
            param_name: str,
            value: str,
    ) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                record = ReviewDraftRecord(draft_id=draft_id)
                self._records[draft_id] = record
            if after_workflow:
                bucket = record.then_run_params_after_workflow.setdefault(
                    after_workflow, {})
                if value:
                    bucket[param_name] = value
                else:
                    bucket.pop(param_name, None)
                    if not bucket:
                        record.then_run_params_after_workflow.pop(
                            after_workflow, None)
            elif value:
                record.then_run_params_after_push[param_name] = value
            else:
                record.then_run_params_after_push.pop(param_name, None)
        self._notify_change()

    def clear_workflow_intent(self, draft_id: str) -> None:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                return
            record.track_workflow.clear()
            record.then_run_after_push = ""
            record.then_run_after_workflow.clear()
            record.then_run_params_after_push.clear()
            record.then_run_params_after_workflow.clear()
        self._notify_change()

    def snapshot_staged(self, draft_id: str) -> Dict[str, bool]:
        with self._lock:
            record = self._records.get(draft_id)
            if record is None:
                return {}
            return dict(record.staged_paths)

    def remove_many(self, draft_ids: List[str]) -> None:
        changed = False
        with self._lock:
            for draft_id in draft_ids:
                if draft_id in self._records:
                    del self._records[draft_id]
                    changed = True
        if changed:
            self._notify_change()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass
