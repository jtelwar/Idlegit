"""Branch-switch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from models import BranchPicker, State
from git_ops import list_branches
from workers import kick_off_action

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


# Outer padding mirroring align_heads_prompt — uniform across every
# repo-name-bearing modal.
_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 60


def _hints(picker: BranchPicker) -> list:
    """Footer hints in scope for the branch picker. Empty branch list
    leaves only Esc — there's literally no row to navigate to or
    check out."""
    if not picker.branches:
        return [Hint(KEY_ESC, "back")]
    hints = [Hint(KEY_UP_DOWN, "select")]
    selected_branch = picker.branches[picker.selected]
    if picker.mode == "merge":
        if selected_branch == picker.current:
            hints.append(Hint(KEY_ENTER, "can't merge a branch into itself"))
        else:
            hints.append(Hint(
                KEY_ENTER,
                f"merge --ff-only {selected_branch} into {picker.current}"))
    else:
        if selected_branch == picker.current:
            # Enter on the row already-checked-out is a no-op; describe it
            # accurately rather than promising a "checkout" that won't run.
            hints.append(Hint(KEY_ENTER, "stay (already checked out)"))
        else:
            hints.append(Hint(KEY_ENTER, f"checkout {selected_branch}"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def open_branch_picker(state: State, mode: str = "switch") -> None:
    menu = state.action_menu
    if menu is None:
        return
    branches, current = list_branches(menu.target_path)
    initial = 0
    if mode == "merge":
        # Default the cursor to the first branch that ISN'T the current
        # one — Enter on a no-op row would just bounce, so picking a
        # useful initial saves a keystroke.
        for i, b in enumerate(branches):
            if b != current:
                initial = i
                break
    else:
        for i, b in enumerate(branches):
            if b == current:
                initial = i
                break
    state.branch_picker = BranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        branches=branches,
        current=current,
        selected=initial,
        mode=mode,
    )


def _title_lines(picker: BranchPicker, inner_w: int) -> "list[str]":
    """Title-area lines. `target_label` is the full repo display name
    (no `task_repo_label` truncation) — long names get their own
    indented line via wrap_label_value, end-truncated only when even
    that doesn't fit."""
    label = "Merge in branch" if picker.mode == "merge" else "Switch branch"
    return wrap_label_value(label, picker.target_label, inner_w)


def draw_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return

    sb = curses.color_pair(PAIR_SB_FG)

    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    n_branches = max(1, len(picker.branches))
    blank_after_title = 1
    blank_after_list = 1
    hint_rows = 1

    desired_h = (
        _PAD_TOP
        + len(title_rows)
        + blank_after_title
        + n_branches
        + blank_after_list
        + hint_rows
        + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = _title_lines(picker, inner_w)

    fixed_rows = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + blank_after_list + hint_rows + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)

    # Title.
    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    if not picker.branches:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no branches found)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(picker), attr=sb | curses.A_DIM)
        return

    # Scroll math.
    if picker.selected < picker.scroll:
        picker.scroll = picker.selected
    elif picker.selected >= picker.scroll + visible_rows:
        picker.scroll = picker.selected - visible_rows + 1
    picker.scroll = max(0, min(
        picker.scroll, max(0, len(picker.branches) - visible_rows)))

    n = len(picker.branches)
    end = min(n, picker.scroll + visible_rows)
    for slot in range(visible_rows):
        idx = picker.scroll + slot
        if idx >= n:
            break
        row_y = line + slot

        if slot == 0 and picker.scroll > 0:
            msg = f"  ↑ {picker.scroll} more above"
            safe_addstr(stdscr, row_y, inner_x,
                        end_truncate(msg, inner_w), sb | curses.A_DIM)
            continue
        if slot == visible_rows - 1 and end < n:
            below = n - end + 1
            msg = f"  ↓ {below} more below"
            safe_addstr(stdscr, row_y, inner_x,
                        end_truncate(msg, inner_w), sb | curses.A_DIM)
            continue

        name = picker.branches[idx]
        focused = (idx == picker.selected)
        is_current = (name == picker.current)
        marker = "* " if is_current else "  "
        prefix = "→ " if focused else marker
        text = end_truncate(prefix + name, inner_w).ljust(inner_w)
        attr = sb | curses.A_REVERSE if focused else sb
        if is_current and not focused:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(picker), attr=sb | curses.A_DIM)


def handle_branch_picker_key(state: State, key: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    if key == 27:
        state.branch_picker = None
        return
    if not picker.branches:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        branch = picker.branches[picker.selected]
        if picker.mode == "merge":
            # Self-merge is a no-op git would refuse anyway — silently
            # consume the keystroke so a stray Enter on the current row
            # doesn't dismiss the modal without doing anything visible.
            if branch == picker.current:
                return
            action_id = "ff_merge"
        else:
            action_id = "switch_branch"
        kick_off_action(
            state, action_id,
            target_label=picker.target_label,
            target_path=picker.target_path,
            target_repo=picker.target_repo,
            target_parent=picker.target_parent,
            branch_arg=branch,
        )
        state.branch_picker = None
        state.action_menu = None
