"""Remote-branch-picker key handling and action dispatch."""
from __future__ import annotations

import curses

from core.git_ops import is_safe_ref_arg
from core.state.app import State
from core.state.pickers import RemoteBranchPicker
from core.workers import kick_off_action

from .projection import picker_refs
from .session import close_remote_branch_picker


def handle_remote_branch_picker_key(state: State, key: int) -> None:
    picker = state.remote_branch_picker
    if picker is None:
        return
    if key == 27:
        close_remote_branch_picker(state)
        return
    refs, loading = picker_refs(state, picker)
    if loading or not refs:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(refs) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(refs) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_remote_branch(state, picker, refs[picker.selected])


def submit_remote_branch(
        state: State,
        picker: RemoteBranchPicker,
        ref: str,
) -> None:
    if not is_safe_ref_arg(ref) or "/" not in ref:
        return
    kick_off_action(
        state,
        "checkout_remote_branch",
        target_label=picker.target_label,
        target_path=picker.target_path,
        target_repo=picker.target_repo,
        target_parent=picker.target_parent,
        branch_arg=ref,
    )
    state.view_loads.remove_many([picker.load_id])
    state.remote_branch_picker = None
    state.action_menu = None
