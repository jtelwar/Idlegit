"""Soft-reset count prompt — sub-modal of the action menu."""
from __future__ import annotations

import curses

from models import ResetPrompt, State
from workers import kick_off_action

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr


def open_reset_prompt(state: State) -> None:
    menu = state.action_menu
    if menu is None:
        return
    state.reset_prompt = ResetPrompt(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        ahead=menu.ahead,
    )


def draw_reset_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    content_h = 7
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 56, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y, inner_x,
                f"Soft reset — {prompt.target_label}"[: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    safe_addstr(stdscr, y + 2, inner_x,
                f"unpushed commits on this branch: {prompt.ahead}",
                sb | curses.A_DIM)
    safe_addstr(stdscr, y + 3, inner_x,
                "Number to reset:  (Enter on 0 wipes ALL unpushed)",
                sb | curses.A_DIM)

    visible = prompt.typed if prompt.typed else "0"
    field_text = f" {visible} "
    safe_addstr(stdscr, y + 4, inner_x, field_text.ljust(w - 4),
                sb | curses.A_REVERSE)

    safe_addstr(stdscr, y + h - 1, inner_x,
                "type a number · Enter run · Esc back",
                sb | curses.A_DIM)


def handle_reset_prompt_key(state: State, key: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    if key == 27:
        state.reset_prompt = None
        return
    if key in (10, 13, curses.KEY_ENTER):
        try:
            n = int(prompt.typed) if prompt.typed else 0
        except ValueError:
            n = 0
        kick_off_action(
            state, "soft_reset",
            target_label=prompt.target_label,
            target_path=prompt.target_path,
            target_repo=prompt.target_repo,
            target_parent=prompt.target_parent,
            reset_count=max(0, n),
        )
        state.reset_prompt = None
        state.action_menu = None
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        prompt.typed = prompt.typed[:-1]
        return
    if 48 <= key <= 57:
        prompt.typed += chr(key)
