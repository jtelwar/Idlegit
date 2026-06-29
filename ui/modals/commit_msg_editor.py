"""Large multi-line commit-message editor modal rendering."""
from __future__ import annotations

import curses

from core.state.app import State
from features.commit_msg_editor.actions import handle_commit_msg_editor_key as handle_key
from features.commit_msg_editor.projection import (
    BODY_TARGET_ROWS,
    MODAL_W,
    build_display_rows,
    cursor_display_position,
    holder_message,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FIELD
from ..geometry import (
    draw_modal_fill, modal_geometry, safe_addstr, truncate,
)
from ..hints import KEY_ENTER, KEY_ESC, Hint, render_hints


def _hints() -> list:
    return [
        Hint(KEY_ENTER, "close"),
        Hint(KEY_ESC, "close"),
    ]


def handle_commit_msg_editor_key(state: State, key: int) -> None:
    handle_key(state, key)


def draw_commit_msg_editor(stdscr, state: State, sidebar_x: int) -> None:
    editor = state.commit_msg_editor
    if editor is None:
        return

    body_h = max(4, BODY_TARGET_ROWS)
    content_h = 1 + 1 + 1 + body_h + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4
    editor._wrap_width = inner_w

    name_max = max(8, inner_w - 16)
    name_text = truncate(editor.label, name_max, "middle")
    safe_addstr(stdscr, y + 1, inner_x, name_text,
                curses.A_BOLD | sb)
    if editor.branch:
        branch_text = f" [{editor.branch}]"
        branch_x = inner_x + min(len(name_text), name_max)
        remaining = max(0, inner_w - (branch_x - inner_x))
        safe_addstr(stdscr, y + 1, branch_x, branch_text[:remaining],
                    curses.color_pair(PAIR_DLG_CYAN))

    text_y0 = y + 3
    msg = holder_message(state, editor.holder)
    display_rows, _origins = build_display_rows(msg, inner_w)

    cursor_display_row, cursor_col = cursor_display_position(
        msg, editor.cursor, inner_w)
    if cursor_display_row < editor.scroll:
        editor.scroll = cursor_display_row
    elif cursor_display_row >= editor.scroll + body_h:
        editor.scroll = cursor_display_row - body_h + 1
    editor.scroll = max(0, min(editor.scroll,
                               max(0, len(display_rows) - body_h)))

    field_attr = curses.color_pair(PAIR_DLG_FIELD)
    if curses.COLORS < 256:
        field_attr |= curses.A_UNDERLINE
    for i in range(body_h):
        line_y = text_y0 + i
        display_index = editor.scroll + i
        if display_index < len(display_rows):
            line_text = display_rows[display_index]
        else:
            line_text = ""
        safe_addstr(stdscr, line_y, inner_x,
                    line_text[:inner_w].ljust(inner_w), field_attr)

    render_hints(stdscr, y + h - 2, inner_x, inner_w, _hints(),
                 attr=sb | curses.A_DIM)

    if 0 <= cursor_display_row - editor.scroll < body_h:
        editor._cursor_screen_y = text_y0 + (cursor_display_row - editor.scroll)
        editor._cursor_screen_x = inner_x + min(cursor_col, inner_w - 1)
    else:
        editor._cursor_screen_y = -1
        editor._cursor_screen_x = -1


def apply_commit_msg_editor_cursor(stdscr, state: State) -> bool:
    editor = state.commit_msg_editor
    if editor is None:
        return False
    cursor_y = getattr(editor, "_cursor_screen_y", -1)
    cursor_x = getattr(editor, "_cursor_screen_x", -1)
    if cursor_y < 0 or cursor_x < 0:
        return False
    try:
        stdscr.move(cursor_y, cursor_x)
        curses.curs_set(1)
        return True
    except curses.error:
        return False
