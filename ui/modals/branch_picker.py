"""Branch-switch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from models import BranchPicker, State
from git_ops import list_branches
from workers import kick_off_action

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr


def open_branch_picker(state: State) -> None:
    menu = state.action_menu
    if menu is None:
        return
    branches, current = list_branches(menu.target_path)
    initial = 0
    for i, b in enumerate(branches):
        if b == current:
            initial = i
            break
    state.branch_picker = BranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        branches=branches,
        current=current,
        selected=initial,
    )


def draw_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    body_h = max(3, min(15, len(picker.branches)))
    content_h = 1 + 1 + body_h + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 50, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x,
                f"Switch branch — {picker.target_label}"[: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    if not picker.branches:
        safe_addstr(stdscr, y + 2, inner_x,
                    "(no branches found)", sb | curses.A_DIM)
        safe_addstr(stdscr, y + h - 1, inner_x,
                    "Esc back", sb | curses.A_DIM)
        return

    if picker.selected < picker.scroll:
        picker.scroll = picker.selected
    elif picker.selected >= picker.scroll + body_h:
        picker.scroll = picker.selected - body_h + 1
    picker.scroll = max(0, min(picker.scroll,
                               max(0, len(picker.branches) - body_h)))

    for i in range(body_h):
        idx = picker.scroll + i
        if idx >= len(picker.branches):
            break
        name = picker.branches[idx]
        focused = (idx == picker.selected)
        is_current = (name == picker.current)
        marker = "* " if is_current else "  "
        prefix = "→ " if focused else marker
        text = (prefix + name).ljust(w - 4)
        attr = sb | curses.A_REVERSE if focused else sb
        if is_current and not focused:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, y + 2 + i, inner_x, text, attr)

    if picker.scroll > 0:
        safe_addstr(stdscr, y + 1, inner_x,
                    f"↑ {picker.scroll} more above", sb | curses.A_DIM)
    if picker.scroll + body_h < len(picker.branches):
        below = len(picker.branches) - (picker.scroll + body_h)
        safe_addstr(stdscr, y + 2 + body_h, inner_x,
                    f"↓ {below} more below", sb | curses.A_DIM)

    safe_addstr(stdscr, y + h - 1, inner_x,
                "↑/↓ select · Enter checkout · Esc back",
                sb | curses.A_DIM)


def handle_branch_picker_key(state: State, key: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    if key == 27:
        state.branch_picker = None
        return
    if not picker.branches:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        branch = picker.branches[picker.selected]
        kick_off_action(
            state, "switch_branch",
            target_label=picker.target_label,
            target_path=picker.target_path,
            target_repo=picker.target_repo,
            target_parent=picker.target_parent,
            branch_arg=branch,
        )
        state.branch_picker = None
        state.action_menu = None
