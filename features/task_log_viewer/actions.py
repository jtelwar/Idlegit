"""Task log viewer key handling."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.views import TaskLogViewer

from .session import close_task_log_viewer


def handle_task_log_viewer_key(state: State, key: int) -> None:
    viewer = state.task_log_viewer
    if viewer is None:
        return
    if key in (9, 10, 13, curses.KEY_ENTER, 27):
        close_task_log_viewer(state)
        return
    current_scroll = viewer.scroll
    if key == curses.KEY_UP:
        set_scroll(viewer, max(0, current_scroll - 1))
        return
    if key == curses.KEY_DOWN:
        set_scroll(viewer, current_scroll + 1)
        return
    if key == curses.KEY_PPAGE:
        set_scroll(viewer, max(0, current_scroll - 10))
        return
    if key == curses.KEY_NPAGE:
        set_scroll(viewer, current_scroll + 10)
        return
    if key == curses.KEY_HOME:
        set_scroll(viewer, 0)
        return
    if key == curses.KEY_END:
        lines, _loading, _error = state.view_loads.snapshot(viewer.load_id)
        set_scroll(viewer, len(lines))


def set_scroll(viewer: TaskLogViewer, value: int) -> None:
    viewer.scroll = value
