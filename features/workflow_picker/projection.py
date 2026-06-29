"""Workflow picker projection helpers."""
from __future__ import annotations

from core.state.repos import WorkflowInfo
from core.state.pickers import WorkflowPicker

KEY_ENTER_LABEL = "Enter"
KEY_ESC_LABEL = "Esc"
KEY_UP_DOWN_LABEL = "↑/↓"


def workflow_row_status(workflow: WorkflowInfo) -> tuple[bool, str]:
    if workflow.state.startswith("disabled"):
        return False, f"({workflow.state.replace('_', ' ')})"
    if not workflow.dispatchable:
        return False, "(no workflow_dispatch trigger)"
    return True, ""


def first_runnable_workflow_index(workflows: list[WorkflowInfo]) -> int:
    for index, workflow in enumerate(workflows):
        runnable, _reason = workflow_row_status(workflow)
        if runnable:
            return index
    return 0


def selected_workflow(picker: WorkflowPicker) -> WorkflowInfo | None:
    if not picker.workflows:
        return None
    return picker.workflows[picker.selected]


def workflow_picker_hint_specs(picker: WorkflowPicker) -> list[tuple[str, str]]:
    if not picker.workflows:
        return [(KEY_ESC_LABEL, "back")]
    hints = [(KEY_UP_DOWN_LABEL, "select")]
    workflow = picker.workflows[picker.selected]
    runnable, reason = workflow_row_status(workflow)
    if runnable:
        hints.append((KEY_ENTER_LABEL, f"run on {picker.branch}"))
    else:
        hints.append((KEY_ENTER_LABEL, f"unavailable {reason}".rstrip()))
    hints.append((KEY_ESC_LABEL, "back"))
    return hints
