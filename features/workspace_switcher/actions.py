"""Workspace switcher key handling and switch dispatch."""
from __future__ import annotations

import curses

from core.state.app import State
from core.workers import switch_workspace

from .session import close_workspace_switcher


def handle_workspace_switcher_key(state: State, key: int) -> str | None:
    switcher = state.workspace_switcher
    if switcher is None:
        return None
    if key == 27:
        close_workspace_switcher(state)
        return None

    workspace_count = len(state.workspaces)
    if workspace_count == 0:
        close_workspace_switcher(state)
        return None

    if key == curses.KEY_UP:
        switcher.selected = max(0, switcher.selected - 1)
        return None
    if key == curses.KEY_DOWN:
        switcher.selected = min(workspace_count - 1, switcher.selected + 1)
        return None
    if key == curses.KEY_PPAGE:
        switcher.selected = max(0, switcher.selected - 10)
        return None
    if key == curses.KEY_NPAGE:
        switcher.selected = min(workspace_count - 1, switcher.selected + 10)
        return None
    if key == curses.KEY_HOME:
        switcher.selected = 0
        return None
    if key == curses.KEY_END:
        switcher.selected = workspace_count - 1
        return None

    if key in (10, 13, curses.KEY_ENTER):
        return submit_workspace_switcher(state)
    return None


def submit_workspace_switcher(state: State) -> str | None:
    switcher = state.workspace_switcher
    if switcher is None:
        return None
    target = switcher.selected
    close_workspace_switcher(state)
    if target != state.active_workspace_index:
        switch_workspace(state, target)
        return "switch-workspace"
    return None
