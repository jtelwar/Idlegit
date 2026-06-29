"""Commit message editor key handling."""
from __future__ import annotations

import curses

from core.state.app import State

from .projection import (
    cursor_to_row_col,
    holder_message,
    move_display_vertical,
    row_col_to_cursor,
    set_holder_message,
    split_lines,
    wrap_width,
)


def handle_commit_msg_editor_key(state: State, key: int) -> None:
    editor = state.commit_msg_editor
    if editor is None:
        return

    if key in (27, 10, 13, curses.KEY_ENTER):
        state.commit_msg_editor = None
        return

    msg = holder_message(state, editor.holder)
    cursor = max(0, min(editor.cursor, len(msg)))

    if key == curses.KEY_LEFT:
        editor.cursor = max(0, cursor - 1)
        return
    if key == curses.KEY_RIGHT:
        editor.cursor = min(len(msg), cursor + 1)
        return
    if key == curses.KEY_UP:
        editor.cursor = move_display_vertical(
            msg, cursor, -1, wrap_width(editor))
        return
    if key == curses.KEY_DOWN:
        editor.cursor = move_display_vertical(
            msg, cursor, +1, wrap_width(editor))
        return
    if key in (curses.KEY_HOME, 1):
        row, _ = cursor_to_row_col(msg, cursor)
        editor.cursor = row_col_to_cursor(msg, row, 0)
        return
    if key in (curses.KEY_END, 5):
        row, _ = cursor_to_row_col(msg, cursor)
        lines = split_lines(msg)
        editor.cursor = row_col_to_cursor(msg, row, len(lines[row]))
        return

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cursor > 0:
            set_holder_message(
                state, editor.holder, msg[: cursor - 1] + msg[cursor:])
            editor.cursor = cursor - 1
        return
    if key == curses.KEY_DC:
        if cursor < len(msg):
            set_holder_message(
                state, editor.holder, msg[:cursor] + msg[cursor + 1:])
        return

    if 32 <= key < 127:
        char = chr(key)
        set_holder_message(state, editor.holder, msg[:cursor] + char + msg[cursor:])
        editor.cursor = cursor + 1
