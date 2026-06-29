"""Workspace menu session lifecycle and path-check dispatch."""
from __future__ import annotations

from core.state.app import State
from core.state.workspaces import WorkspaceDraft, WorkspaceMenu
from core.workers import kick_off_workspace_path_check

from .projection import build_rows


def open_workspace_menu(state: State) -> None:
    if not state.workspaces:
        return
    ws = state.active_workspace
    if ws is None:
        return
    rows = build_rows(ws)
    drafts = [WorkspaceDraft(path_text=str(path)) for path in ws.folders]
    selected = 0
    for i, row in enumerate(rows):
        if row.kind != "header":
            selected = i
            break
    state.workspace_menu = WorkspaceMenu(
        rows=rows,
        selected=selected,
        scroll=0,
        path_drafts=drafts,
    )
    for draft in drafts:
        kick_off_path_check(state, draft)


def kick_off_path_check(state: State, draft: WorkspaceDraft) -> None:
    kick_off_workspace_path_check(
        state,
        draft,
        kind="workspace-menu-path-check",
    )


def tick_menu_path_checks(state: State) -> bool:
    menu = state.workspace_menu
    if menu is None:
        return False
    any_checking = False
    for draft in menu.path_drafts:
        if draft.path_text != draft.last_checked and not draft.checking:
            kick_off_path_check(state, draft)
        if draft.checking:
            any_checking = True
    return any_checking
