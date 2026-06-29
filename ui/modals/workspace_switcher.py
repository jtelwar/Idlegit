"""Workspace switcher — scrollable list of configured workspaces.

Opened with Enter on the main-screen workspace row (the name between
the title and the repo list). Enter on a row switches to that
workspace and closes the dialog; Esc closes without switching."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.workspaces import WorkspaceSwitcher
from features.workspace_switcher.actions import (
    handle_workspace_switcher_key as handle_workspace_switcher_key_action,
)
from features.workspace_switcher.projection import workspace_switcher_hint_specs

from .app_menu import _draw_workspace_row
from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr,
)
from ..hints import Hint, render_hints

_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70
_BODY_TARGET_ROWS = 14


def _hints(state: State, switcher: WorkspaceSwitcher) -> list:
    return [Hint(keys, action)
            for keys, action in workspace_switcher_hint_specs(state, switcher)]


def draw_workspace_switcher(stdscr, state: State, sidebar_x: int) -> None:
    switcher = state.workspace_switcher
    if switcher is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    n_ws = len(state.workspaces)

    # Layout mirrors the app menu: title, scroll-↑ spacer, body,
    # scroll-↓ spacer, hints — then derive how many workspace rows
    # actually fit once `modal_geometry` has capped height on a
    # small terminal.
    title_rows = 1
    blank_after_title = 1
    blank_before_hints = 1
    hint_rows = 1
    desired_body = min(_BODY_TARGET_ROWS, max(1, n_ws))
    desired_h = (
        _PAD_TOP + title_rows + blank_after_title + desired_body
        + blank_before_hints + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    fixed_rows = (
        _PAD_TOP + title_rows + blank_after_title
        + blank_before_hints + hint_rows + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)
    if n_ws > 0:
        visible_rows = min(visible_rows, n_ws)

    list_y = y + _PAD_TOP + title_rows + blank_after_title
    hint_y = y + h - _PAD_BOTTOM - hint_rows
    spacer_down_y = list_y + visible_rows

    safe_addstr(stdscr, y + _PAD_TOP, inner_x,
                end_truncate("Workspaces", inner_w),
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))

    if n_ws == 0:
        safe_addstr(stdscr, list_y, inner_x,
                    end_truncate("  (no workspaces configured)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, hint_y, inner_x, inner_w,
                     _hints(state, switcher), attr=sb | curses.A_DIM)
        return

    scroll_up_y = y + _PAD_TOP + title_rows
    switcher.scroll = clamp_scroll(
        switcher.selected, switcher.scroll, n_ws, visible_rows)

    if switcher.scroll > 0:
        draw_scroll_overflow(stdscr, scroll_up_y, inner_x, inner_w,
                             switcher.scroll, "up", sb | curses.A_DIM)

    for slot in range(visible_rows):
        idx = switcher.scroll + slot
        if idx >= n_ws:
            break
        row_y = list_y + slot
        focused = (idx == switcher.selected)
        _draw_workspace_row(stdscr, row_y, inner_x, inner_w,
                            state, idx, focused, sb)

    if switcher.scroll + visible_rows < n_ws:
        below = n_ws - (switcher.scroll + visible_rows)
        draw_scroll_overflow(stdscr, spacer_down_y, inner_x, inner_w,
                             below, "down", sb | curses.A_DIM)

    render_hints(stdscr, hint_y, inner_x, inner_w,
                 _hints(state, switcher), attr=sb | curses.A_DIM)


def handle_workspace_switcher_key(state: State, key: int) -> str | None:
    """Returns ``switch-workspace`` when Enter lands on a new workspace."""
    return handle_workspace_switcher_key_action(state, key)
