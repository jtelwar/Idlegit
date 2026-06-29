"""Workspace switcher session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.workspaces import WorkspaceSwitcher

from .projection import clamped_active_workspace_index


def open_workspace_switcher(state: State) -> None:
    if not state.workspaces:
        return
    state.workspace_switcher = WorkspaceSwitcher(
        selected=clamped_active_workspace_index(state))


def close_workspace_switcher(state: State) -> None:
    state.workspace_switcher = None
