"""Diff viewer key handling."""
from __future__ import annotations

import curses

from core.state.app import State

from .projection import TAB_IDS, set_tab_scroll, tab_lines, tab_scroll
from .session import close_diff_viewer


def handle_diff_viewer_key(state: State, key: int) -> None:
    viewer = state.diff_viewer
    if viewer is None:
        return
    if key in (9, 10, 13, curses.KEY_ENTER, 27):
        close_diff_viewer(state)
        return

    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        viewer.active_tab = next_tab(
            viewer.active_tab,
            -1 if key == curses.KEY_LEFT else 1,
        )
        return

    active_tab = viewer.active_tab
    current_scroll = tab_scroll(viewer, active_tab)
    if key == curses.KEY_UP:
        set_tab_scroll(viewer, active_tab, max(0, current_scroll - 1))
        return
    if key == curses.KEY_DOWN:
        set_tab_scroll(viewer, active_tab, current_scroll + 1)
        return
    if key == curses.KEY_PPAGE:
        set_tab_scroll(viewer, active_tab, max(0, current_scroll - 10))
        return
    if key == curses.KEY_NPAGE:
        set_tab_scroll(viewer, active_tab, current_scroll + 10)
        return
    if key == curses.KEY_HOME:
        set_tab_scroll(viewer, active_tab, 0)
        return
    if key == curses.KEY_END:
        set_tab_scroll(viewer, active_tab,
                       len(tab_lines(state, viewer, active_tab)))


def next_tab(active_tab: str, direction: int) -> str:
    try:
        index = TAB_IDS.index(active_tab)
    except ValueError:
        index = 0
    return TAB_IDS[(index + direction) % len(TAB_IDS)]
