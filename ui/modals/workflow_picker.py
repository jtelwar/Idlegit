"""GitHub Actions workflow_dispatch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.pickers import WorkflowPicker
from features.workflow_picker.actions import (
    handle_workflow_picker_key as handle_workflow_picker_key_action,
)
from features.workflow_picker.projection import (
    workflow_picker_hint_specs,
    workflow_row_status,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr, wrap_label_value,
)
from ..hints import Hint, render_hints


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70


def _hints(picker) -> list:
    """Footer hints for the workflow_dispatch picker. Enter is gated
    on the focused workflow being runnable — disabled / non-
    dispatchable rows show why instead of pretending Enter will fire."""
    return [Hint(keys, action)
            for keys, action in workflow_picker_hint_specs(picker)]


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

    sb = curses.color_pair(PAIR_DLG_FG)
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
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
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
        runnable, reason = workflow_row_status(wf)
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
    handle_workflow_picker_key_action(state, key)
