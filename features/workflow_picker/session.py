"""Workflow picker session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.pickers import WorkflowPicker

from .projection import first_runnable_workflow_index


def open_workflow_picker(state: State) -> None:
    menu = state.action_menu
    if menu is None:
        return
    target_repo = menu.target_repo
    if target_repo is None and menu.target_child is not None:
        target_repo = menu.target_child.repo
    if target_repo is None:
        return
    workflows = list(target_repo.workflows)
    if not workflows:
        return
    state.workflow_picker = WorkflowPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        workflows=workflows,
        branch=menu.branch or target_repo.branch,
        selected=first_runnable_workflow_index(workflows),
    )


def close_workflow_picker(state: State) -> None:
    state.workflow_picker = None
