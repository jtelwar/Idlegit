"""Remote-tracking branch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.pickers import RemoteBranchPicker
from features.remote_branch_picker.actions import (
    handle_remote_branch_picker_key as handle_remote_branch_picker_key_action,
)
from features.remote_branch_picker.projection import (
    picker_refs,
    title_label,
    tracking_label,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
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
_MODAL_W = 64


def _spinner_glyph(state: State) -> str:
    from ..sidebar import SPINNER_FRAMES
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _hints(state: State, picker: RemoteBranchPicker) -> list:
    refs, loading = picker_refs(state, picker)
    if loading:
        return [Hint(KEY_ESC, "back")]
    if not refs:
        return [
            Hint("(no remote branches — fetch first)", ""),
            Hint(KEY_ESC, "back"),
        ]
    ref = refs[picker.selected]
    hints = [Hint(KEY_UP_DOWN, "select")]
    hints.append(Hint(KEY_ENTER, tracking_label(ref)))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _title_lines(picker: RemoteBranchPicker, inner_w: int) -> "list[str]":
    return wrap_label_value(title_label(picker), picker.target_label, inner_w)


def draw_remote_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.remote_branch_picker
    if picker is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    refs, loading = picker_refs(state, picker)

    n_rows = 1 if loading or not refs else len(refs)
    blank_after_title = 1
    blank_after_list = 1
    hint_rows = 1
    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title + n_rows
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

    if loading:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(
                        f"  {_spinner_glyph(state)} loading remote branches…",
                        inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(state, picker), attr=sb | curses.A_DIM)
        return

    if not refs:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no remote branches found)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(state, picker), attr=sb | curses.A_DIM)
        return

    n = len(refs)
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
        name = refs[idx]
        focused = (idx == picker.selected)
        prefix = "→ " if focused else "  "
        text = end_truncate(prefix + name, inner_w).ljust(inner_w)
        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(state, picker), attr=sb | curses.A_DIM)


def handle_remote_branch_picker_key(state: State, key: int) -> None:
    handle_remote_branch_picker_key_action(state, key)
