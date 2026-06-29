"""Commit message editor cursor and display projection helpers."""
from __future__ import annotations

from typing import List

from core.state.app import State
from core.state.edit_buffers import CommitMsgEditor


MODAL_W = 84
BODY_TARGET_ROWS = 12


def split_lines(text: str) -> List[str]:
    return text.split("\n")


def cursor_to_row_col(text: str, cursor: int) -> tuple[int, int]:
    cursor = max(0, min(cursor, len(text)))
    before = text[:cursor]
    rows = before.split("\n")
    return len(rows) - 1, len(rows[-1])


def row_col_to_cursor(text: str, row: int, col: int) -> int:
    lines = split_lines(text)
    if not lines:
        return 0
    row = max(0, min(row, len(lines) - 1))
    col = max(0, min(col, len(lines[row])))
    return sum(len(line) + 1 for line in lines[:row]) + col


def holder_message(state: State, holder) -> str:
    return state.store.row_message(holder)


def set_holder_message(state: State, holder, value: str) -> None:
    state.store.set_row_message(holder, value)


def wrap_logical_line(line: str, width: int) -> List[str]:
    if width <= 0:
        return [""]
    if not line:
        return [""]
    rows: List[str] = []
    index = 0
    while index < len(line):
        rows.append(line[index: index + width])
        index += width
    return rows


def build_display_rows(msg: str, width: int) -> tuple[List[str], List[tuple[int, int]]]:
    display_rows: List[str] = []
    origins: List[tuple[int, int]] = []
    logical = split_lines(msg)
    for logical_row, line in enumerate(logical):
        pieces = wrap_logical_line(line, width)
        offset = 0
        for piece in pieces:
            display_rows.append(piece)
            origins.append((logical_row, offset))
            offset += len(piece)
    return display_rows, origins


def cursor_display_position(msg: str, cursor: int, width: int) -> tuple[int, int]:
    row, col = cursor_to_row_col(msg, cursor)
    display_rows, origins = build_display_rows(msg, width)
    target_display_row = 0
    for display_row, (logical_row, offset) in enumerate(origins):
        if logical_row != row:
            continue
        end = offset + len(display_rows[display_row])
        if offset <= col < end:
            return display_row, col - offset
        if col == end:
            target_display_row = display_row
            if (display_row + 1 < len(origins)
                    and origins[display_row + 1][0] == row):
                continue
            return display_row, col - offset
    return target_display_row, col


def wrap_width(editor: CommitMsgEditor) -> int:
    width = getattr(editor, "_wrap_width", 0)
    if width > 0:
        return width
    return max(1, MODAL_W - 4)


def move_display_vertical(
    msg: str,
    cursor: int,
    delta: int,
    width: int,
) -> int:
    current_display_row, current_col = cursor_display_position(
        msg, cursor, width)
    display_rows, origins = build_display_rows(msg, width)
    target_display_row = current_display_row + delta
    if target_display_row < 0:
        return 0
    if target_display_row >= len(display_rows):
        return len(msg)
    logical_row, char_offset = origins[target_display_row]
    new_col_in_row = min(current_col, len(display_rows[target_display_row]))
    return row_col_to_cursor(msg, logical_row, char_offset + new_col_in_row)


_cursor_to_row_col = cursor_to_row_col
_row_col_to_cursor = row_col_to_cursor
