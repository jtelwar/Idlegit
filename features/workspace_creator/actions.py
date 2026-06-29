"""Workspace creator key handling and commit conversion."""
from __future__ import annotations

import curses
from pathlib import Path

from core.state.app import State
from core.state.workspaces import Workspace, WorkspaceCreator, WorkspaceDraft

from .session import close_workspace_creator


def drafts_to_workspaces(drafts: list[WorkspaceDraft]) -> list[Workspace]:
    out: list[Workspace] = []
    seen_names: dict[str, int] = {}
    for draft in drafts:
        text = draft.path_text.strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        base = resolved.name or str(resolved) or "workspace"
        name = base
        count = seen_names.get(base, 0)
        if count:
            name = f"{base} ({count + 1})"
        seen_names[base] = count + 1
        out.append(Workspace(name=name, folders=[resolved]))
    return out


def commit_workspace_creator(state: State) -> None:
    creator = state.workspace_creator
    if creator is None:
        return
    creator.result = drafts_to_workspaces(creator.drafts)


def handle_workspace_creator_key(state: State, key: int) -> None:
    creator = state.workspace_creator
    if creator is None:
        return

    if key == 27:
        creator.result = []
        close_workspace_creator(state)
        return

    if key == curses.KEY_UP:
        move_to_field(creator, max(0, creator.selected - 1))
        return
    if key == curses.KEY_DOWN:
        max_index = len(creator.drafts)
        if (creator.selected < len(creator.drafts)
                and not creator.drafts[creator.selected].path_text):
            move_to_field(creator, max_index)
            return
        move_to_field(creator, min(max_index, creator.selected + 1))
        return

    if on_done_row(creator):
        if key in (10, 13, curses.KEY_ENTER):
            nonempty = sum(1 for draft in creator.drafts
                           if draft.path_text.strip())
            if nonempty == 0:
                move_to_field(creator, 0)
                return
            commit_workspace_creator(state)
        return

    draft = focused_draft(creator)
    if draft is None:
        return

    text = draft.path_text
    cursor = max(0, min(creator.field_cursor, len(text)))

    if key in (10, 13, curses.KEY_ENTER, 9):
        if not text.strip():
            move_to_field(creator, len(creator.drafts))
            return
        ensure_trailing_empty(creator)
        move_to_field(creator, creator.selected + 1)
        return

    if key == curses.KEY_LEFT:
        creator.field_cursor = max(0, cursor - 1)
        return
    if key == curses.KEY_RIGHT:
        creator.field_cursor = min(len(text), cursor + 1)
        return
    if key in (curses.KEY_HOME, 1):
        creator.field_cursor = 0
        return
    if key in (curses.KEY_END, 5):
        creator.field_cursor = len(text)
        return

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cursor > 0:
            draft.path_text = text[: cursor - 1] + text[cursor:]
            creator.field_cursor = cursor - 1
            draft.last_checked = ""
        return
    if key == curses.KEY_DC:
        if cursor < len(text):
            draft.path_text = text[:cursor] + text[cursor + 1:]
            draft.last_checked = ""
        return
    if 32 <= key < 127:
        draft.path_text = text[:cursor] + chr(key) + text[cursor:]
        creator.field_cursor = cursor + 1
        draft.last_checked = ""
        ensure_trailing_empty(creator)


def focused_draft(creator: WorkspaceCreator) -> WorkspaceDraft | None:
    if 0 <= creator.selected < len(creator.drafts):
        return creator.drafts[creator.selected]
    return None


def on_done_row(creator: WorkspaceCreator) -> bool:
    return creator.selected == len(creator.drafts)


def ensure_trailing_empty(creator: WorkspaceCreator) -> None:
    if not creator.drafts or creator.drafts[-1].path_text:
        creator.drafts.append(WorkspaceDraft())


def move_to_field(creator: WorkspaceCreator, index: int) -> None:
    if index < 0 or index > len(creator.drafts):
        return
    creator.selected = index
    if index < len(creator.drafts):
        creator.field_cursor = len(creator.drafts[index].path_text)
    else:
        creator.field_cursor = 0
