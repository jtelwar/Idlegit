"""Clone modal session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.clone import CloneModal

from .projection import FIELD_URL


def open_clone_modal(state: State) -> None:
    workspace = state.active_workspace
    if workspace is None:
        return
    state.clone_modal = CloneModal(
        workspace_name=workspace.name,
        workspace_folders=list(workspace.folders),
        selected=FIELD_URL,
    )


def close_clone_modal(state: State) -> None:
    state.clone_modal = None

