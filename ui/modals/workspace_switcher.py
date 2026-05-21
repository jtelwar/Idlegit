"""Workspace switcher — scrollable list of configured workspaces.

Opened with Enter on the main-screen workspace row (the name between
the title and the repo list). Enter on a row switches to that
workspace and closes the dialog; Esc closes without switching."""
from __future__ import annotations

import curses
from typing import Optional

from core.models import State, WorkspaceSwitcher
from core.workers import switch_workspace

from .app_menu import _draw_workspace_row
from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr,
)
from ..hints import KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints

_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70
_BODY_TARGET_ROWS = 14


def open_workspace_switcher(state: State) -> None:
    """Install the picker with the cursor on the active workspace."""
    if not state.workspaces:
        return
    idx = max(0, min(state.active_workspace_index, len(state.workspaces) - 1))
    state.workspace_switcher = WorkspaceSwitcher(selected=idx)


def _hints(state: State, switcher: WorkspaceSwitcher) -> list:
    hints = [Hint(KEY_UP_DOWN, "select")]
    n = len(state.workspaces)
    if 0 <= switcher.selected < n:
        ws = state.workspaces[switcher.selected]
        if switcher.selected == state.active_workspace_index:
            hints.append(Hint(KEY_ENTER, "stay (already active)"))
        else:
            hints.append(Hint(KEY_ENTER, f"switch to {ws.display_name}"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


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


def handle_workspace_switcher_key(state: State, key: int) -> Optional[str]:
    """Returns ``switch-workspace`` when Enter lands on a new workspace."""
    switcher = state.workspace_switcher
    if switcher is None:
        return None
    if key == 27:  # Esc
        state.workspace_switcher = None
        return None

    n = len(state.workspaces)
    if n == 0:
        state.workspace_switcher = None
        return None

    if key == curses.KEY_UP:
        switcher.selected = max(0, switcher.selected - 1)
        return None
    if key == curses.KEY_DOWN:
        switcher.selected = min(n - 1, switcher.selected + 1)
        return None
    if key == curses.KEY_PPAGE:
        switcher.selected = max(0, switcher.selected - 10)
        return None
    if key == curses.KEY_NPAGE:
        switcher.selected = min(n - 1, switcher.selected + 10)
        return None
    if key == curses.KEY_HOME:
        switcher.selected = 0
        return None
    if key == curses.KEY_END:
        switcher.selected = n - 1
        return None

    if key in (10, 13, curses.KEY_ENTER):
        target = switcher.selected
        state.workspace_switcher = None
        if target != state.active_workspace_index:
            switch_workspace(state, target)
            return "switch-workspace"
        return None
    return None
