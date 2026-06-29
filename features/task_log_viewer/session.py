"""Task log viewer session lifecycle."""
from __future__ import annotations

from core.runtime.tasks import Task
from core.state.app import State
from core.state.views import TaskLogViewer
from core.workers import kick_off_task_log_load


def open_task_log_viewer(state: State, task: Task) -> None:
    run_record = state.workflow_runs.record_for_task(task)
    if run_record is None or run_record.run_id is None or not run_record.slug:
        return
    viewer = TaskLogViewer(
        task=task,
        slug=run_record.slug,
        run_id=run_record.run_id,
        load_id=(
            f"task-log:{id(state)}:{id(task)}:"
            f"{run_record.run_id}:{run_record.job_id or 'run'}"
        ),
        job_id=run_record.job_id,
        workflow_name=run_record.workflow_name or "",
        only_failed=task.status == "fail",
    )
    state.task_log_viewer = viewer
    state.view_loads.create(viewer.load_id)
    kick_off_task_log_load(
        state,
        viewer,
        label=f"log {run_record.workflow_name or run_record.slug}",
    )


def close_task_log_viewer(state: State) -> None:
    viewer = state.task_log_viewer
    if viewer is None:
        return
    state.view_loads.remove_many([viewer.load_id])
    state.task_log_viewer = None
