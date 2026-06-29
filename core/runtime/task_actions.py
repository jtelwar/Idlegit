"""Runtime-owned task action projection queries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .jobs import Job
from .tasks import Task


_TERMINAL_TASK_STATUSES = frozenset({"ok", "fail", "warn"})


@dataclass(frozen=True)
class TaskActionProjection:
    """Action availability for one task row, derived from runtime owners."""

    task: Task
    job: Optional[Job]
    run_record: object | None
    followup_record: object | None
    pending_followup_task: Optional[Task]
    pending_workflow_name: str
    can_cancel_run: bool
    cancel_run_disabled: bool
    cancel_run_reason: str
    can_cancel_pipeline: bool
    can_change_then_run: bool
    can_view_log: bool
    can_open_in_browser: bool
    can_remove: bool


def task_action_projection(state: object, task: Task) -> TaskActionProjection:
    """Return runtime-derived actions for a task projection row."""
    job = state.job_registry.job_for_task(task)
    run_record = state.workflow_runs.record_for_task(task)
    followup_record = state.workflow_followups.record_for_task(task)
    terminal = task_runtime_terminal(job, task)

    can_cancel_run = _run_has_cancel_target(run_record) and not terminal
    cancel_run_disabled = (
        run_record is not None
        and getattr(run_record, "run_id", None) is not None
        and not can_cancel_run
    )
    cancel_run_reason = ""
    if cancel_run_disabled:
        cancel_run_reason = "already finished" if terminal else "no run id"

    pending_followup_task = _pending_followup_task(state, task, run_record, followup_record)
    can_change_then_run = pending_followup_task is not None and (
        followup_record is not None or run_record is not None)
    pending_workflow_name = ""
    if followup_record is not None:
        pending_workflow_name = str(
            getattr(followup_record, "parent_workflow", "") or "")
    elif run_record is not None:
        pending_workflow_name = str(getattr(run_record, "workflow_name", "") or "")

    return TaskActionProjection(
        task=task,
        job=job,
        run_record=run_record,
        followup_record=followup_record,
        pending_followup_task=pending_followup_task,
        pending_workflow_name=pending_workflow_name,
        can_cancel_run=can_cancel_run,
        cancel_run_disabled=cancel_run_disabled,
        cancel_run_reason=cancel_run_reason,
        can_cancel_pipeline=job is not None and not job.terminal,
        can_change_then_run=can_change_then_run,
        can_view_log=_run_has_log(run_record),
        can_open_in_browser=bool(getattr(run_record, "run_url", "")),
        can_remove=task_can_remove(state, task),
    )


def task_runtime_terminal(job: Optional[Job], task: Task) -> bool:
    """Return whether the runtime owner considers the task no longer active."""
    if job is not None:
        return job.terminal
    return task.status in _TERMINAL_TASK_STATUSES


def task_can_remove(state: object, task: Task) -> bool:
    """Return whether a task projection row can be manually removed."""
    job = state.job_registry.job_for_task(task)
    if job is not None:
        return job.terminal
    return task.status in _TERMINAL_TASK_STATUSES


def _run_has_cancel_target(run_record: object | None) -> bool:
    if run_record is None:
        return False
    return (
        getattr(run_record, "run_id", None) is not None
        and bool(getattr(run_record, "slug", ""))
    )


def _run_has_log(run_record: object | None) -> bool:
    if run_record is None:
        return False
    return (
        getattr(run_record, "run_id", None) is not None
        and bool(getattr(run_record, "slug", ""))
    )


def _pending_followup_task(
        state: object,
        task: Task,
        run_record: object | None,
        followup_record: object | None,
) -> Optional[Task]:
    if _followup_has_target(followup_record):
        return task
    if run_record is None:
        return None
    workflow_name = getattr(run_record, "workflow_name", "") or ""
    repo = getattr(run_record, "repo", None)
    if not workflow_name:
        return None
    for candidate in state.tasks.snapshot():
        candidate_followup = state.workflow_followups.record_for_task(candidate)
        if not _followup_has_target(candidate_followup):
            continue
        if getattr(candidate_followup, "parent_workflow", "") != workflow_name:
            continue
        if getattr(candidate_followup, "repo", None) is not repo:
            continue
        return candidate
    return None


def _followup_has_target(followup_record: object | None) -> bool:
    return followup_record is not None and bool(
        getattr(followup_record, "target", ""))
