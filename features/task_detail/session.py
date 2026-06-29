"""Task-detail menu session lifecycle."""
from __future__ import annotations

from core.runtime.task_actions import task_action_projection
from core.state.app import State
from core.state.task_detail import TaskActionMenu, TaskActionMenuItem
from core.runtime.tasks import Task


def open_task_action_menu(state: State, task: Task) -> None:
    projection = task_action_projection(state, task)
    items: list[TaskActionMenuItem] = []

    if projection.can_cancel_run:
        items.append(TaskActionMenuItem(
            id="cancel_run", label="Cancel this run"))
    elif projection.cancel_run_disabled:
        items.append(TaskActionMenuItem(
            id="cancel_run", label="Cancel this run",
            enabled=False,
            reason=projection.cancel_run_reason))

    if projection.can_cancel_pipeline:
        items.append(TaskActionMenuItem(
            id="cancel_pipeline", label="Cancel task"))

    if projection.can_change_then_run:
        items.append(TaskActionMenuItem(
            id="change_then_run", label="Change then-run target"))
        items.append(TaskActionMenuItem(
            id="clear_then_run", label="Cancel then-run"))

    if projection.can_view_log:
        items.append(TaskActionMenuItem(id="view_log", label="View log"))

    if projection.can_open_in_browser:
        items.append(TaskActionMenuItem(
            id="open_in_browser", label="Open run in browser"))

    if projection.can_remove:
        items.append(TaskActionMenuItem(id="remove", label="Remove from list"))

    items.append(TaskActionMenuItem(id="close", label="Close"))

    initial = 0
    for i, item in enumerate(items):
        if item.enabled:
            initial = i
            break

    state.task_action_menu = TaskActionMenu(
        task=task,
        items=items,
        selected=initial,
        pending_child=projection.pending_followup_task,
        pending_workflow=projection.pending_workflow_name or None,
    )
