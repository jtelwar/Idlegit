"""Workspace menu key handling and worker-facing mutations."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from pathlib import Path

from core.config import TRUNCATION_MODES
from core.state.app import State
from core.state.workspaces import WorkspaceDraft, WorkspaceMenu, WorkspaceMenuRow
from core.workers import kick_off_workspace_settings_save

from .projection import (
    apply_base_value,
    draft_for_row,
    focused_row,
    is_focusable,
    read_value,
    rebuild_rows,
)
from .session import kick_off_path_check


@dataclass(frozen=True)
class WorkspaceMenuEffect:
    kind: str = "none"


NO_EFFECT = WorkspaceMenuEffect()
OPEN_CLONE_EFFECT = WorkspaceMenuEffect("open_clone")


def write_value(state: State, row: WorkspaceMenuRow, value) -> None:
    from .projection import state_attr_for

    setattr(state, state_attr_for(row), value)
    ws = state.active_workspace
    if ws is None:
        return
    ws.overrides[row.attr_name] = value
    persist(state)


def clear_override(state: State, row: WorkspaceMenuRow) -> None:
    ws = state.active_workspace
    if ws is None or row.attr_name not in ws.overrides:
        return
    del ws.overrides[row.attr_name]
    apply_base_value(state, row)
    persist(state)


def persist(state: State) -> None:
    kick_off_workspace_settings_save(state)


def save_ephemeral_workspace(state: State) -> None:
    ws = state.active_workspace
    if ws is None or not ws.ephemeral:
        return
    ws.ephemeral = False
    state.workspace_name = ws.display_name

    def on_failure(message: str) -> None:
        ws.ephemeral = True
        state.workspace_name = ws.display_name
        rebuild_rows(state)

    kick_off_workspace_settings_save(
        state,
        label=f"save workspace: {ws.name}",
        success_message="added to idlegit.workspaces",
        on_failure=on_failure,
    )
    rebuild_rows(state)


def commit_folder_edit(state: State, idx: int, raw: str) -> bool:
    ws = state.active_workspace
    if ws is None:
        return False
    text = raw.strip()
    if not text:
        return False
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = path.resolve()
    except (OSError, RuntimeError):
        return False
    if idx == len(ws.folders):
        ws.folders.append(path)
    else:
        ws.folders[idx] = path
    persist(state)
    return True


def commit_ignore_pattern_edit(state: State, idx: int, raw: str) -> bool:
    ws = state.active_workspace
    if ws is None:
        return False
    text = raw.strip()
    if not text:
        return False
    if idx == len(ws.fs_watch_ignore):
        ws.fs_watch_ignore.append(text)
    else:
        ws.fs_watch_ignore[idx] = text
    state.fs_watch_ignore = list(ws.fs_watch_ignore)
    persist(state)
    return True


def remove_ignore_pattern(state: State, idx: int) -> bool:
    ws = state.active_workspace
    if ws is None or idx < 0 or idx >= len(ws.fs_watch_ignore):
        return False
    del ws.fs_watch_ignore[idx]
    state.fs_watch_ignore = list(ws.fs_watch_ignore)
    persist(state)
    return True


def remove_folder(state: State, idx: int) -> bool:
    ws = state.active_workspace
    if ws is None or idx < 0 or idx >= len(ws.folders):
        return False
    if len(ws.folders) <= 1:
        return False
    del ws.folders[idx]
    persist(state)
    return True


def enter_edit_mode(menu: WorkspaceMenu, initial_text: str) -> None:
    menu.editing = True
    menu.edit_buffer = initial_text
    menu.edit_cursor = len(initial_text)


def exit_edit_mode(menu: WorkspaceMenu) -> None:
    menu.editing = False
    menu.edit_buffer = ""
    menu.edit_cursor = 0


def cycle_trunc(value: str, direction: int) -> str:
    if value not in TRUNCATION_MODES:
        return TRUNCATION_MODES[0]
    index = TRUNCATION_MODES.index(value)
    return TRUNCATION_MODES[(index + direction) % len(TRUNCATION_MODES)]


def bump_int(row: WorkspaceMenuRow, value: int, direction: int) -> int:
    try:
        current = int(value)
    except (TypeError, ValueError):
        current = row.min_value
    next_value = current + direction * row.step
    return max(row.min_value, min(row.max_value, next_value))


def adjust(state: State, row: WorkspaceMenuRow, direction: int) -> None:
    current = read_value(state, row)
    if row.kind == "bool":
        return
    if row.kind == "trunc_mode":
        next_value = cycle_trunc(str(current or ""), direction)
    elif row.kind == "int":
        base = current if current is not None else row.min_value
        next_value = bump_int(row, base, direction)
    else:
        return
    if next_value != current:
        write_value(state, row, next_value)


def toggle_bool(state: State, row: WorkspaceMenuRow) -> None:
    if row.kind != "bool":
        return
    current = read_value(state, row)
    write_value(state, row, not bool(current))


def move_selected(menu: WorkspaceMenu, direction: int) -> None:
    if not menu.rows:
        return
    count = len(menu.rows)
    new_selected = menu.selected
    while True:
        new_selected += direction
        if new_selected < 0 or new_selected >= count:
            return
        if is_focusable(menu.rows[new_selected]):
            menu.selected = new_selected
            return


def handle_workspace_menu_key(state: State, key: int) -> WorkspaceMenuEffect:
    menu = state.workspace_menu
    if menu is None:
        return NO_EFFECT

    if menu.editing:
        handle_edit_key(state, menu, key)
        return NO_EFFECT

    if key in (27, 9):
        state.workspace_menu = None
        return NO_EFFECT
    if not menu.rows:
        return NO_EFFECT

    if key == curses.KEY_UP:
        move_selected(menu, -1)
        return NO_EFFECT
    if key == curses.KEY_DOWN:
        move_selected(menu, +1)
        return NO_EFFECT
    if key == curses.KEY_PPAGE:
        for _ in range(5):
            move_selected(menu, -1)
        return NO_EFFECT
    if key == curses.KEY_NPAGE:
        for _ in range(5):
            move_selected(menu, +1)
        return NO_EFFECT
    if key == curses.KEY_HOME:
        for i, row in enumerate(menu.rows):
            if is_focusable(row):
                menu.selected = i
                break
        return NO_EFFECT
    if key == curses.KEY_END:
        for i in range(len(menu.rows) - 1, -1, -1):
            if is_focusable(menu.rows[i]):
                menu.selected = i
                break
        return NO_EFFECT

    row = focused_row(menu)
    if row is None:
        return NO_EFFECT
    return handle_focused_row_key(state, menu, row, key)


def handle_focused_row_key(
    state: State,
    menu: WorkspaceMenu,
    row: WorkspaceMenuRow,
    key: int,
) -> WorkspaceMenuEffect:
    if row.kind == "folder":
        if key in (10, 13, curses.KEY_ENTER):
            draft = draft_for_row(menu, row)
            enter_edit_mode(menu, draft.path_text if draft else "")
            return NO_EFFECT
        if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
            try:
                idx = int(row.attr_name)
            except ValueError:
                return NO_EFFECT
            if remove_folder(state, idx):
                if 0 <= idx < len(menu.path_drafts):
                    del menu.path_drafts[idx]
                rebuild_rows(state)
            return NO_EFFECT
        return NO_EFFECT

    if row.kind == "add_folder":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            enter_edit_mode(menu, "")
        return NO_EFFECT

    if row.kind == "ignore_pattern":
        if key in (10, 13, curses.KEY_ENTER):
            ws = state.active_workspace
            if ws is None:
                return NO_EFFECT
            try:
                idx = int(row.attr_name)
            except ValueError:
                return NO_EFFECT
            current = (ws.fs_watch_ignore[idx]
                       if 0 <= idx < len(ws.fs_watch_ignore) else "")
            enter_edit_mode(menu, current)
            return NO_EFFECT
        if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
            try:
                idx = int(row.attr_name)
            except ValueError:
                return NO_EFFECT
            if remove_ignore_pattern(state, idx):
                rebuild_rows(state)
            return NO_EFFECT
        return NO_EFFECT

    if row.kind == "add_ignore_pattern":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            enter_edit_mode(menu, "")
        return NO_EFFECT

    if row.kind == "clone":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            return OPEN_CLONE_EFFECT
        return NO_EFFECT

    if row.kind == "save_ephemeral":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            save_ephemeral_workspace(state)
        return NO_EFFECT

    if key == curses.KEY_LEFT:
        adjust(state, row, -1)
        return NO_EFFECT
    if key == curses.KEY_RIGHT:
        adjust(state, row, +1)
        return NO_EFFECT
    if key == ord(" "):
        toggle_bool(state, row)
        return NO_EFFECT
    if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
        clear_override(state, row)
        return NO_EFFECT
    if key in (10, 13, curses.KEY_ENTER):
        if row.kind == "bool":
            toggle_bool(state, row)
        else:
            adjust(state, row, +1)
        return NO_EFFECT
    return NO_EFFECT


def handle_edit_key(state: State, menu: WorkspaceMenu, key: int) -> None:
    if key == 27:
        exit_edit_mode(menu)
        return

    text = menu.edit_buffer
    cursor = max(0, min(menu.edit_cursor, len(text)))

    if key in (10, 13, curses.KEY_ENTER):
        commit_edit_key(state, menu, text)
        return

    if key == curses.KEY_LEFT:
        menu.edit_cursor = max(0, cursor - 1)
        return
    if key == curses.KEY_RIGHT:
        menu.edit_cursor = min(len(text), cursor + 1)
        return
    if key in (curses.KEY_HOME, 1):
        menu.edit_cursor = 0
        return
    if key in (curses.KEY_END, 5):
        menu.edit_cursor = len(text)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cursor > 0:
            menu.edit_buffer = text[: cursor - 1] + text[cursor:]
            menu.edit_cursor = cursor - 1
        return
    if key == curses.KEY_DC:
        if cursor < len(text):
            menu.edit_buffer = text[:cursor] + text[cursor + 1:]
        return
    if 32 <= key < 127:
        menu.edit_buffer = text[:cursor] + chr(key) + text[cursor:]
        menu.edit_cursor = cursor + 1


def commit_edit_key(state: State, menu: WorkspaceMenu, text: str) -> None:
    row = focused_row(menu)
    if row is None:
        exit_edit_mode(menu)
        return
    ws = state.active_workspace
    if ws is None:
        exit_edit_mode(menu)
        return

    if row.kind == "folder":
        try:
            idx = int(row.attr_name)
        except ValueError:
            exit_edit_mode(menu)
            return
    elif row.kind == "add_folder":
        idx = len(ws.folders)
    elif row.kind == "ignore_pattern":
        try:
            idx = int(row.attr_name)
        except ValueError:
            exit_edit_mode(menu)
            return
        commit_pattern_and_rebuild(state, menu, idx, text)
        return
    elif row.kind == "add_ignore_pattern":
        commit_pattern_and_rebuild(state, menu, len(ws.fs_watch_ignore), text)
        return
    else:
        exit_edit_mode(menu)
        return

    if not commit_folder_edit(state, idx, text):
        exit_edit_mode(menu)
        return
    if idx == len(menu.path_drafts):
        menu.path_drafts.append(WorkspaceDraft(path_text=text))
    else:
        menu.path_drafts[idx] = WorkspaceDraft(path_text=text)
    kick_off_path_check(state, menu.path_drafts[idx])
    exit_edit_mode(menu)
    rebuild_rows(state)


def commit_pattern_and_rebuild(
    state: State,
    menu: WorkspaceMenu,
    idx: int,
    text: str,
) -> None:
    if not commit_ignore_pattern_edit(state, idx, text):
        exit_edit_mode(menu)
        return
    exit_edit_mode(menu)
    rebuild_rows(state)
