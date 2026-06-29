"""Workflow picker key handling and worker dispatch."""
from __future__ import annotations

import curses

from core.state.app import State
from core.workers import kick_off_manual_dispatch

from .projection import selected_workflow, workflow_row_status
from .session import close_workflow_picker


def handle_workflow_picker_key(state: State, key: int) -> None:
    picker = state.workflow_picker
    if picker is None:
        return
    if key == 27:
        close_workflow_picker(state)
        return
    if not picker.workflows:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.workflows) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.workflows) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_workflow_picker(state)


def submit_workflow_picker(state: State) -> None:
    picker = state.workflow_picker
    if picker is None:
        return
    workflow = selected_workflow(picker)
    if workflow is None:
        return
    runnable, _reason = workflow_row_status(workflow)
    if not runnable:
        return
    if picker.target_repo is not None:
        kick_off_manual_dispatch(
            state, picker.target_repo, workflow.name, picker.branch)
    state.workflow_picker = None
    state.action_menu = None
