"""Remote-tracking branch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses
import threading

from core.git_ops import is_safe_ref_arg, list_remote_tracking_refs
from core.models import RemoteBranchPicker, State
from core.workers import kick_off_action

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


def _hints(picker: RemoteBranchPicker) -> list:
    if picker.loading:
        return [Hint(KEY_ESC, "back")]
    if not picker.refs:
        return [
            Hint("(no remote branches — fetch first)", ""),
            Hint(KEY_ESC, "back"),
        ]
    ref = picker.refs[picker.selected]
    hints = [Hint(KEY_UP_DOWN, "select")]
    if "/" in ref:
        short = ref.split("/", 1)[1]
        hints.append(Hint(KEY_ENTER, f"checkout {short} (track {ref})"))
    else:
        hints.append(Hint(KEY_ENTER, f"checkout {ref}"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _kick_off_refs_load(picker: RemoteBranchPicker) -> None:
    """Populate `picker.refs` in a daemon thread."""
    path = picker.target_path

    def worker() -> None:
        try:
            if picker.cancel_event.is_set():
                return
            refs = list_remote_tracking_refs(path)
            if picker.cancel_event.is_set():
                return
            picker.refs = refs
            if refs:
                picker.selected = 0
        finally:
            picker.loading = False

    threading.Thread(target=worker, daemon=True).start()


def open_remote_branch_picker(state: State) -> None:
    menu = state.action_menu
    if menu is None:
        return
    picker = RemoteBranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
    )
    state.remote_branch_picker = picker
    _kick_off_refs_load(picker)


def _title_lines(picker: RemoteBranchPicker, inner_w: int) -> "list[str]":
    return wrap_label_value("Checkout remote branch", picker.target_label,
                            inner_w)


def draw_remote_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.remote_branch_picker
    if picker is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    n_rows = 1 if picker.loading or not picker.refs else len(picker.refs)
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

    if picker.loading:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(
                        f"  {_spinner_glyph(state)} loading remote branches…",
                        inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(picker), attr=sb | curses.A_DIM)
        return

    if not picker.refs:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no remote branches found)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(picker), attr=sb | curses.A_DIM)
        return

    n = len(picker.refs)
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
        name = picker.refs[idx]
        focused = (idx == picker.selected)
        prefix = "→ " if focused else "  "
        text = end_truncate(prefix + name, inner_w).ljust(inner_w)
        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(picker), attr=sb | curses.A_DIM)


def handle_remote_branch_picker_key(state: State, key: int) -> None:
    picker = state.remote_branch_picker
    if picker is None:
        return
    if key == 27:
        picker.cancel_event.set()
        state.remote_branch_picker = None
        return
    if picker.loading or not picker.refs:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.refs) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.refs) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        ref = picker.refs[picker.selected]
        if not is_safe_ref_arg(ref) or "/" not in ref:
            return
        kick_off_action(
            state, "checkout_remote_branch",
            target_label=picker.target_label,
            target_path=picker.target_path,
            target_repo=picker.target_repo,
            target_parent=picker.target_parent,
            branch_arg=ref,
        )
        picker.cancel_event.set()
        state.remote_branch_picker = None
        state.action_menu = None
