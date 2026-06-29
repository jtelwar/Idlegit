"""Workspace settings modal rendering."""
from __future__ import annotations

import curses
from typing import Tuple

from core.state.app import State
from core.state.workspaces import WorkspaceDraft, WorkspaceMenu, WorkspaceMenuRow
from features.clone_modal.session import open_clone_modal
from features.workspace_menu.actions import handle_workspace_menu_key as handle_key
from features.workspace_menu.projection import (
    draft_for_row,
    format_value,
    is_overridden,
    read_value,
)

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FG_HINT_TEXT, PAIR_DLG_OK,
    PAIR_DLG_WARN,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, modal_geometry, safe_addstr,
    truncate,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_SPACE,
    KEY_UP_DOWN, Hint, render_hints,
)


MODAL_W = 80
BODY_TARGET_ROWS = 14


def _hints_edit_mode(menu: WorkspaceMenu) -> list:
    return [
        Hint("type", "edit path"),
        Hint(KEY_ENTER, "save path"),
        Hint(KEY_ESC, "cancel edit"),
    ]


def _hints_nav_mode(state: State, menu: WorkspaceMenu) -> list:
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= menu.selected < len(menu.rows):
        row = menu.rows[menu.selected]
        if row.kind == "folder":
            hints.append(Hint(KEY_ENTER, "edit path"))
            ws = state.active_workspace
            count = len(ws.folders) if ws else 0
            if count > 1:
                hints.append(Hint(KEY_BACKSPACE, "remove folder"))
            else:
                hints.append(Hint(KEY_BACKSPACE,
                                  "(can't remove last folder)"))
        elif row.kind == "add_folder":
            hints.append(Hint(KEY_ENTER, "type a new folder path"))
        elif row.kind == "ignore_pattern":
            hints.append(Hint(KEY_ENTER, "edit pattern"))
            hints.append(Hint(KEY_BACKSPACE, "remove pattern"))
        elif row.kind == "add_ignore_pattern":
            hints.append(Hint(KEY_ENTER, "type a new ignore pattern"))
        elif row.kind == "clone":
            hints.append(Hint(KEY_ENTER, "open clone dialog"))
        elif row.kind == "save_ephemeral":
            hints.append(Hint(KEY_ENTER, "persist as workspace"))
        elif row.kind == "bool":
            hints.append(Hint(KEY_SPACE, "toggle"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
        elif row.kind == "trunc_mode":
            hints.append(Hint(KEY_LEFT_RIGHT, "cycle mode"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
        elif row.kind == "int":
            hints.append(Hint(KEY_LEFT_RIGHT, "adjust value"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _draw_menu_hints(stdscr, state: State, menu: WorkspaceMenu, y: int,
                     x: int, w: int, attr: int) -> None:
    hints = (_hints_edit_mode(menu) if menu.editing
             else _hints_nav_mode(state, menu))
    render_hints(stdscr, y, x, w, hints, attr=attr)


def _folder_status_pair(draft: WorkspaceDraft) -> Tuple[str, int]:
    if draft.checking:
        return ("(checking…)", 0)
    text = draft.path_text.strip()
    if not text:
        return ("", 0)
    if draft.error:
        return (draft.error, PAIR_DLG_WARN)
    if draft.repo_count > 0:
        return (f"✓ {draft.repo_count} repo"
                f"{'s' if draft.repo_count != 1 else ''}", PAIR_DLG_OK)
    if draft.repo_count == 0:
        return ("(no repos)", PAIR_DLG_WARN)
    return ("", 0)


def draw_workspace_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.workspace_menu
    if menu is None:
        return
    row_count = len(menu.rows)
    body_h = max(3, min(BODY_TARGET_ROWS, row_count))
    content_h = 1 + 1 + 1 + 1 + body_h + 1 + 1 + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4
    ws = state.active_workspace
    title = (
        f"Workspace settings — {ws.display_name if ws else '(no workspace)'}")
    safe_addstr(stdscr, y + 1, inner_x, title[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))

    if menu.selected < menu.scroll:
        menu.scroll = menu.selected
    elif menu.selected >= menu.scroll + body_h:
        menu.scroll = menu.selected - body_h + 1
    menu.scroll = max(0, min(menu.scroll, max(0, row_count - body_h)))

    value_w = 28
    label_w = max(10, inner_w - value_w - 4)

    for i in range(body_h):
        idx = menu.scroll + i
        if idx >= row_count:
            break
        row = menu.rows[idx]
        line_y = y + 4 + i
        focused = idx == menu.selected
        _draw_row(
            stdscr,
            state,
            menu,
            row,
            line_y,
            inner_x,
            inner_w,
            label_w,
            focused,
            sb,
        )

    if menu.scroll > 0:
        draw_scroll_overflow(stdscr, y + 3, inner_x, inner_w,
                             menu.scroll, "up", sb | curses.A_DIM)
    if menu.scroll + body_h < row_count:
        below = row_count - (menu.scroll + body_h)
        draw_scroll_overflow(stdscr, y + 4 + body_h, inner_x, inner_w,
                             below, "down", sb | curses.A_DIM)

    hint_text = ""
    if 0 <= menu.selected < row_count:
        hint_text = menu.rows[menu.selected].hint_text
    if hint_text:
        safe_addstr(stdscr, y + h - 3, inner_x,
                    truncate(hint_text, inner_w, "end"),
                    curses.color_pair(PAIR_DLG_FG_HINT_TEXT))

    _draw_menu_hints(stdscr, state, menu, y + h - 2, inner_x, inner_w,
                     sb | curses.A_DIM)


def _draw_row(
    stdscr,
    state: State,
    menu: WorkspaceMenu,
    row: WorkspaceMenuRow,
    line_y: int,
    inner_x: int,
    inner_w: int,
    label_w: int,
    focused: bool,
    sb: int,
) -> None:
    if row.kind == "header":
        safe_addstr(stdscr, line_y, inner_x,
                    row.label.ljust(inner_w)[:inner_w],
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
        return
    if row.kind == "folder":
        _draw_folder_row(stdscr, line_y, inner_x, inner_w, label_w,
                         menu, row, focused, sb)
        return
    if row.kind == "ignore_pattern":
        _draw_ignore_pattern_row(stdscr, line_y, inner_x, inner_w,
                                 state, menu, row, focused, sb)
        return
    if row.kind in ("add_folder", "add_ignore_pattern",
                    "clone", "save_ephemeral"):
        _draw_action_row(stdscr, line_y, inner_x, inner_w, row, focused)
        return
    _draw_override_row(stdscr, state, line_y, inner_x, label_w, row,
                       focused, sb)


def _draw_action_row(stdscr, line_y: int, inner_x: int, inner_w: int,
                     row: WorkspaceMenuRow, focused: bool) -> None:
    prefix = "→ " if focused else "  "
    text = (prefix + row.label).ljust(inner_w)[:inner_w]
    attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
            if focused else
            curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
    if focused:
        attr |= curses.A_REVERSE
    safe_addstr(stdscr, line_y, inner_x, text, attr)


def _draw_override_row(
    stdscr,
    state: State,
    line_y: int,
    inner_x: int,
    label_w: int,
    row: WorkspaceMenuRow,
    focused: bool,
    sb: int,
) -> None:
    prefix = "→ " if focused else "  "
    label = (prefix + row.label).ljust(label_w)
    value_text = format_value(state, row)
    overridden = is_overridden(state, row)
    hint = "" if overridden else " (default)"
    attr = sb | curses.A_REVERSE if focused else sb
    safe_addstr(stdscr, line_y, inner_x, label, attr)

    value_x = inner_x + label_w + 2
    if row.kind == "bool":
        val_attr = (curses.color_pair(PAIR_DLG_OK) if read_value(state, row)
                    else sb | curses.A_DIM)
    elif overridden:
        val_attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
    else:
        val_attr = sb
    if focused:
        val_attr |= curses.A_REVERSE
    safe_addstr(stdscr, line_y, value_x, value_text, val_attr)
    if hint:
        safe_addstr(stdscr, line_y,
                    value_x + len(value_text), hint,
                    sb | curses.A_DIM)


def _draw_ignore_pattern_row(stdscr, line_y: int, inner_x: int,
                             inner_w: int, state: State,
                             menu: WorkspaceMenu, row: WorkspaceMenuRow,
                             focused: bool, sb: int) -> None:
    ws = state.active_workspace
    try:
        idx = int(row.attr_name)
    except ValueError:
        idx = -1
    current = (ws.fs_watch_ignore[idx]
               if ws is not None and 0 <= idx < len(ws.fs_watch_ignore)
               else "")
    prefix = "→ " if focused else "  "
    field_w = max(20, inner_w - len(prefix))

    if focused and menu.editing:
        _draw_edit_buffer(stdscr, line_y, inner_x, prefix, field_w, menu)
        return

    visible = truncate(current, field_w, "end")
    body = visible.ljust(field_w)
    attr = sb | curses.A_REVERSE if focused else sb
    safe_addstr(stdscr, line_y, inner_x,
                (prefix + body).ljust(inner_w)[:inner_w], attr)


def _draw_folder_row(stdscr, line_y: int, inner_x: int, inner_w: int,
                     label_w: int, menu: WorkspaceMenu,
                     row: WorkspaceMenuRow, focused: bool, sb: int) -> None:
    draft = draft_for_row(menu, row)
    prefix = "→ " if focused else "  "
    badge_w = 16
    field_w = max(20, inner_w - len(prefix) - badge_w - 1)

    if focused and menu.editing:
        _draw_edit_buffer(stdscr, line_y, inner_x, prefix, field_w, menu)
    else:
        text = draft.path_text if draft is not None else ""
        visible = truncate(text, field_w, "middle")
        body = visible.ljust(field_w)
        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, line_y, inner_x,
                    (prefix + body).ljust(inner_w)[:inner_w], attr)

    if draft is not None:
        status_text, status_pair = _folder_status_pair(draft)
        if status_text:
            badge_x = inner_x + len(prefix) + field_w + 1
            badge_attr = (curses.color_pair(status_pair) if status_pair
                          else sb | curses.A_DIM)
            if focused and not menu.editing:
                badge_attr |= curses.A_REVERSE
            safe_addstr(stdscr, line_y, badge_x,
                        status_text[:badge_w], badge_attr)


def _draw_edit_buffer(stdscr, line_y: int, inner_x: int, prefix: str,
                      field_w: int, menu: WorkspaceMenu) -> None:
    text = menu.edit_buffer
    cursor = max(0, min(menu.edit_cursor, len(text)))
    if len(text) <= field_w - 1:
        visible = text
        cursor_offset = cursor
    else:
        half = (field_w - 1) // 2
        start = max(0, min(cursor - half, len(text) - (field_w - 1)))
        visible = text[start:start + field_w - 1]
        cursor_offset = cursor - start
    body = visible.ljust(field_w)
    attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
            | curses.A_UNDERLINE)
    safe_addstr(stdscr, line_y, inner_x, prefix, attr)
    safe_addstr(stdscr, line_y, inner_x + len(prefix), body, attr)
    try:
        stdscr.move(line_y, inner_x + len(prefix) + cursor_offset)
        curses.curs_set(2)
    except curses.error:
        pass


def handle_workspace_menu_key(state: State, key: int) -> None:
    effect = handle_key(state, key)
    if effect.kind == "open_clone":
        open_clone_modal(state)
