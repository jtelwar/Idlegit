"""Workspace creator session lifecycle and path-check scheduling."""
from __future__ import annotations

from core.state.app import State
from core.state.workspaces import WorkspaceCreator, WorkspaceDraft
from core.workers import kick_off_workspace_path_check


def open_workspace_creator(
        state: State,
        *,
        title: str = "Set up workspaces",
        intro: str = "",
) -> None:
    if not intro:
        intro = (
            "Add folders to scan for git repos. Each becomes a "
            "workspace named after the folder."
        )
    state.workspace_creator = WorkspaceCreator(
        drafts=[WorkspaceDraft()],
        title=title,
        intro=intro,
    )


def close_workspace_creator(state: State) -> None:
    state.workspace_creator = None


def kick_off_check(state: State, draft: WorkspaceDraft) -> None:
    kick_off_workspace_path_check(
        state,
        draft,
        kind="workspace-path-check",
    )


def tick_creator_checks(state: State) -> bool:
    creator = state.workspace_creator
    if creator is None:
        return False
    any_checking = False
    for draft in creator.drafts:
        if draft.path_text != draft.last_checked and not draft.checking:
            kick_off_check(state, draft)
        if draft.checking:
            any_checking = True
    return any_checking
