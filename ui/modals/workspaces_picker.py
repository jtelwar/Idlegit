"""Workspaces picker — Tab-on-workspace-row modal that lists every
configured workspace plus an "+ Create new workspace" sentinel that
hands off to the WorkspaceCreator."""
from __future__ import annotations

import curses

from models import State, WorkspacesPicker

from ..colors import PAIR_BRANCH, PAIR_OK, PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr, truncate
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


def _hints(state) -> list:
    """Footer hints for the workspaces picker. The Enter description
    swings between "switch", "stay" (active), and "create" depending
    on which row the cursor is on — exactly the kind of context the
    static "Enter switch" used to lie about."""
    picker = state.workspaces_picker
    if picker is None:
        return []
    n = len(state.workspaces)
    sel = picker.selected
    hints = [Hint(KEY_UP_DOWN, "select")]
    if sel == n:
        hints.append(Hint(KEY_ENTER, "create new workspace…"))
    elif sel == state.active_workspace_index:
        hints.append(Hint(KEY_ENTER, "stay (already active)"))
    else:
        ws = state.workspaces[sel]
        hints.append(Hint(KEY_ENTER, f"switch to {ws.name}"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


# Modal sizing.
MODAL_W = 70
BODY_TARGET_ROWS = 10  # max rows shown before scroll arrows kick in


# ---------- Open ----------------------------------------------------------


def open_workspaces_picker(state: State) -> None:
    """Install the picker on top of the main UI. Cursor lands on the
    currently-active workspace so the most common action (just looking
    at the list, then Esc) requires zero key movement; Down once gets
    to the next workspace, and the trailing "+ Create" item sits at
    `len(state.workspaces)`."""
    if not state.workspaces:
        # Without any workspaces there's nothing to pick — defer to the
        # creator wizard directly.
        from .workspace_creator import open_workspace_creator
        open_workspace_creator(
            state, title="Add workspace",
            intro="Add folder paths to scan for git repos.")
        return
    state.workspaces_picker = WorkspacesPicker(
        selected=state.active_workspace_index,
        scroll=0,
    )


# ---------- Draw ----------------------------------------------------------


def _row_count(state: State) -> int:
    """N workspaces + 1 trailing '+ Create new workspace…' sentinel."""
    return len(state.workspaces) + 1


def _is_create_row(state: State, idx: int) -> bool:
    return idx == len(state.workspaces)


def draw_workspaces_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.workspaces_picker
    if picker is None:
        return

    n = _row_count(state)
    body_h = max(3, min(BODY_TARGET_ROWS, n))
    # blank-top (1) + title (1) + spacer (1) + body + spacer (1)
    # + footer (1) + blank-bottom (1) + slack (1)
    content_h = 1 + 1 + 1 + body_h + 1 + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    safe_addstr(stdscr, y + 1, inner_x, "Workspaces"[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    if picker.selected < picker.scroll:
        picker.scroll = picker.selected
    elif picker.selected >= picker.scroll + body_h:
        picker.scroll = picker.selected - body_h + 1
    picker.scroll = max(0, min(picker.scroll, max(0, n - body_h)))

    # Reserve a column on the right for the "(active)" tag + folder
    # count, leaving the rest for the workspace name. The path summary
    # is dim and middle-truncated to fit the leftover space.
    name_w = max(12, inner_w // 3)

    for i in range(body_h):
        idx = picker.scroll + i
        if idx >= n:
            break
        line_y = y + 3 + i
        focused = (idx == picker.selected)
        if _is_create_row(state, idx):
            label = "  + Create new workspace…"
            if focused:
                attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD | curses.A_REVERSE
            else:
                attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
            safe_addstr(stdscr, line_y, inner_x,
                        label.ljust(inner_w)[:inner_w], attr)
            continue
        ws = state.workspaces[idx]
        is_active = (idx == state.active_workspace_index)
        prefix = "→ " if focused else ("• " if is_active else "  ")
        name_text = truncate(ws.name, name_w, "end")
        n_folders = len(ws.folders)
        meta = (f"{n_folders} folder" if n_folders == 1
                else f"{n_folders} folders")
        if is_active:
            meta = "active · " + meta
        # Build the row, then overlay the dim path summary on top of
        # whatever room is left.
        path_w = max(0, inner_w - len(prefix) - name_w - 2 - len(meta) - 1)
        first_path = (truncate(str(ws.folders[0]), path_w, "middle")
                      if ws.folders and path_w > 0 else "")
        line = f"{prefix}{name_text.ljust(name_w)}  {first_path.ljust(path_w)} {meta}"
        attr = sb | curses.A_REVERSE if focused else sb
        if not focused and is_active:
            attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
        safe_addstr(stdscr, line_y, inner_x,
                    line.ljust(inner_w)[:inner_w], attr)
        # Overlay the active marker color when not focused.
        if not focused and is_active:
            safe_addstr(stdscr, line_y, inner_x + len(prefix) + name_w + 2 + path_w + 1,
                        meta, curses.color_pair(PAIR_OK))

    if picker.scroll > 0:
        safe_addstr(stdscr, y + 2, inner_x,
                    f"↑ {picker.scroll} more above", sb | curses.A_DIM)
    if picker.scroll + body_h < n:
        below = n - (picker.scroll + body_h)
        safe_addstr(stdscr, y + 3 + body_h, inner_x,
                    f"↓ {below} more below", sb | curses.A_DIM)

    render_hints(stdscr, y + h - 2, inner_x, w - 4, _hints(state),
                 attr=sb | curses.A_DIM)


# ---------- Handle --------------------------------------------------------


def handle_workspaces_picker_key(state: State, key: int) -> None:
    picker = state.workspaces_picker
    if picker is None:
        return

    if key == 27:
        state.workspaces_picker = None
        return

    n = _row_count(state)
    if n == 0:
        return

    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(n - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(n - 1, picker.selected + 10)
        return
    if key == curses.KEY_HOME:
        picker.selected = 0
        return
    if key == curses.KEY_END:
        picker.selected = n - 1
        return

    if key in (10, 13, curses.KEY_ENTER, ord(" ")):
        if _is_create_row(state, picker.selected):
            # Hand off to the creator. The picker stays in state but
            # the modal-active stack will draw the creator on top; once
            # the creator commits, the main loop closes both modals.
            from .workspace_creator import open_workspace_creator
            open_workspace_creator(
                state, title="Add workspace",
                intro="Add folder paths to scan for git repos.")
            return
        # Switch to the chosen workspace and close the picker.
        target = picker.selected
        state.workspaces_picker = None
        if target != state.active_workspace_index:
            from workers import switch_workspace
            switch_workspace(state, target)
        return
