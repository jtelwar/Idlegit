"""Branch-picker session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.pickers import BranchPicker
from core.workers import kick_off_branch_picker_load


def open_branch_picker(state: State, mode: str = "switch") -> None:
    menu = state.action_menu
    if menu is None:
        return
    picker = BranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        mode=mode,
        load_id=f"branch-picker:{id(state)}:{id(menu.target_path)}:{mode}",
    )
    state.branch_picker = picker
    kick_off_branch_picker_load(state, picker)


def close_branch_picker(state: State) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    state.view_loads.remove_many([picker.load_id])
    state.branch_picker = None
