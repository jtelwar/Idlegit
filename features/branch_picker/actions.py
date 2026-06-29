"""Branch-picker key handling and action dispatch."""
from __future__ import annotations

import curses

from core.git_ops import is_fast_forward_merge, is_safe_ref_arg
from core.state.app import State
from core.state.pickers import BranchPicker
from core.workers import kick_off_action, kick_off_safe_merge

from .projection import VALID_NAME_CHARS, has_create_row, picker_branches
from .session import close_branch_picker


def handle_branch_picker_key(state: State, key: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    if key == 27:
        close_branch_picker(state)
        return
    branches, current, loading = picker_branches(state, picker)
    if loading:
        return

    has_create = has_create_row(picker)
    on_create = has_create and picker.selected == -1

    if on_create:
        handle_create_row_key(state, picker, branches, key)
        return

    if not branches:
        return
    if key == curses.KEY_UP:
        if picker.selected == 0 and has_create:
            picker.selected = -1
        else:
            picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(branches) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(branches) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_selected_branch(state, picker, branches[picker.selected], current)


def handle_create_row_key(
        state: State,
        picker: BranchPicker,
        branches: list[str],
        key: int,
) -> None:
    if key == curses.KEY_DOWN:
        if branches:
            picker.selected = 0
        return
    if key == curses.KEY_UP:
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_create_branch(state, picker)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        picker.create_typed = picker.create_typed[:-1]
        return
    if 32 <= key < 127:
        ch = chr(key)
        if not picker.create_typed and ch == "-":
            return
        if ch in VALID_NAME_CHARS:
            picker.create_typed += ch


def submit_create_branch(state: State, picker: BranchPicker) -> None:
    name = picker.create_typed.strip()
    if not name or not is_safe_ref_arg(name):
        return
    kick_off_action(
        state,
        "create_branch",
        target_label=picker.target_label,
        target_path=picker.target_path,
        target_repo=picker.target_repo,
        target_parent=picker.target_parent,
        branch_arg=name,
    )
    state.branch_picker = None
    state.action_menu = None


def submit_selected_branch(
        state: State,
        picker: BranchPicker,
        branch: str,
        current: str,
) -> None:
    if picker.mode in ("merge", "safe_merge"):
        if branch == current:
            return
        ff = (
            picker.mode == "merge"
            and is_fast_forward_merge(picker.target_path, branch)
        )
        if not ff:
            kick_off_safe_merge(
                state,
                target_label=picker.target_label,
                target_path=picker.target_path,
                target_repo=picker.target_repo,
                target_parent=picker.target_parent,
                target_child=picker.target_child,
                merge_ref=branch,
            )
            state.branch_picker = None
            state.action_menu = None
            return
        action_id = "ff_merge"
    elif picker.mode == "set_upstream":
        if not current:
            return
        action_id = "set_upstream"
    else:
        action_id = "switch_branch"
    kick_off_action(
        state,
        action_id,
        target_label=picker.target_label,
        target_path=picker.target_path,
        target_repo=picker.target_repo,
        target_parent=picker.target_parent,
        branch_arg=branch,
    )
    state.branch_picker = None
    state.action_menu = None
