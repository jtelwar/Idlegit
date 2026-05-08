"""GitHub Actions workflow_dispatch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses
from typing import Tuple

from models import State, WorkflowPicker
from workers import kick_off_manual_dispatch

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr, wrap_label_value,
)
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70


def _hints(picker) -> list:
    """Footer hints for the workflow_dispatch picker. Enter is gated
    on the focused workflow being runnable — disabled / non-
    dispatchable rows show why instead of pretending Enter will fire."""
    if not picker.workflows:
        return [Hint(KEY_ESC, "back")]
    hints = [Hint(KEY_UP_DOWN, "select")]
    wf = picker.workflows[picker.selected]
    runnable, reason = _workflow_row_status(wf)
    if runnable:
        hints.append(Hint(KEY_ENTER, f"run on {picker.branch}"))
    else:
        hints.append(Hint(KEY_ENTER, f"unavailable {reason}".rstrip()))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _workflow_row_status(wf) -> Tuple[bool, str]:
    """Return (runnable, reason) for a workflow row in the picker.
    `runnable` gates Enter; `reason` is a short trailing tag shown after
    the name for any unrunnable row (empty for runnable rows)."""
    if wf.state.startswith("disabled"):
        return False, f"({wf.state.replace('_', ' ')})"
    if not wf.dispatchable:
        return False, "(no workflow_dispatch trigger)"
    return True, ""


def open_workflow_picker(state: State) -> None:
    """Open the workflow picker on the focused row's repo. Lists every
    workflow we discovered locally (including non-dispatchable + remotely
    disabled ones) so the user can see *why* a workflow can't be run from
    here. Enter on a runnable row triggers the dispatch + tracks the
    resulting run via kick_off_manual_dispatch; on disabled rows it's a
    no-op. Defaults the cursor to the first runnable row, falling back
    to row 0."""
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
    initial = 0
    for i, wf in enumerate(workflows):
        if wf.dispatchable and not wf.state.startswith("disabled"):
            initial = i
            break
    state.workflow_picker = WorkflowPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        workflows=workflows,
        branch=menu.branch or target_repo.branch,
        selected=initial,
    )


def _title_lines(picker: WorkflowPicker, inner_w: int) -> "list[str]":
    """Title rows: "Run workflow on <branch>" first, then the repo
    name on its own line via wrap_label_value (so long display names
    don't get middle-truncated, which used to be the only option when
    everything was crammed onto one line)."""
    rows = [end_truncate(f"Run workflow on {picker.branch}", inner_w)]
    rows.extend(wrap_label_value("Repo", picker.target_label, inner_w))
    return rows


def draw_workflow_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.workflow_picker
    if picker is None:
        return

    sb = curses.color_pair(PAIR_SB_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    n_workflows = max(1, len(picker.workflows))
    blank_after_title = 1
    blank_after_list = 1
    hint_rows = 1
    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title + n_workflows
        + blank_after_list + hint_rows + _PAD_BOTTOM
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

    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    if not picker.workflows:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no workflows in this repo)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(picker), attr=sb | curses.A_DIM)
        return

    n = len(picker.workflows)
    picker.scroll = clamp_scroll(
        picker.selected, picker.scroll, n, visible_rows)

    end = min(n, picker.scroll + visible_rows)
    for slot in range(visible_rows):
        idx = picker.scroll + slot
        if idx >= n:
            break
        row_y = line + slot

        if slot == 0 and picker.scroll > 0:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 picker.scroll, "up", sb | curses.A_DIM)
            continue
        if slot == visible_rows - 1 and end < n:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 n - end + 1, "down", sb | curses.A_DIM)
            continue

        wf = picker.workflows[idx]
        focused = (idx == picker.selected)
        runnable, reason = _workflow_row_status(wf)
        prefix = "→ " if focused else "  "
        label = wf.name if runnable else f"{wf.name}  {reason}"
        text = end_truncate(prefix + label, inner_w).ljust(inner_w)
        if focused and runnable:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not runnable:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(picker), attr=sb | curses.A_DIM)


def handle_workflow_picker_key(state: State, key: int) -> None:
    picker = state.workflow_picker
    if picker is None:
        return
    if key == 27:
        state.workflow_picker = None
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
        wf = picker.workflows[picker.selected]
        runnable, _ = _workflow_row_status(wf)
        if not runnable:
            return  # silently no-op on disabled / non-dispatchable rows
        if picker.target_repo is not None:
            kick_off_manual_dispatch(
                state, picker.target_repo, wf.name, picker.branch)
        state.workflow_picker = None
        state.action_menu = None
