"""Soft-reset count prompt — sub-modal of the action menu."""
from __future__ import annotations

import curses

from models import ResetPrompt, State
from workers import kick_off_action

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import KEY_BACKSPACE, KEY_ENTER, KEY_ESC, Hint, render_hints

_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 60


def _hints(prompt: ResetPrompt) -> list:
    """Footer hints for the soft-reset count prompt. Enter's effect
    differs based on whether the user has typed digits yet — describe
    each case explicitly so it's clear what happens on submit."""
    hints = []
    if prompt.typed:
        try:
            n = int(prompt.typed)
        except ValueError:
            n = 0
        hints.append(Hint("0-9", "edit count"))
        hints.append(Hint(KEY_BACKSPACE, "delete digit"))
        if n == 0:
            hints.append(Hint(KEY_ENTER, "wipe ALL unpushed"))
        else:
            plural = "s" if n != 1 else ""
            hints.append(Hint(KEY_ENTER, f"reset {n} commit{plural}"))
    else:
        hints.append(Hint("0-9", "type count"))
        hints.append(Hint(KEY_ENTER, "type 0 to wipe all"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


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


def _title_lines(prompt: ResetPrompt, inner_w: int) -> "list[str]":
    return wrap_label_value("Soft reset", prompt.target_label, inner_w)


def draw_reset_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_SB_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(prompt, target_inner_w)

    # Body: "unpushed commits…" + "Number to reset…" + count field +
    # blank above the hint.
    body_rows = 1 + 1 + 1 + 1
    blank_after_title = 1
    hint_rows = 1
    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title + body_rows
        + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = _title_lines(prompt, inner_w)

    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    safe_addstr(stdscr, line, inner_x,
                end_truncate(
                    f"unpushed commits on this branch: {prompt.ahead}",
                    inner_w),
                sb | curses.A_DIM)
    line += 1
    safe_addstr(stdscr, line, inner_x,
                end_truncate(
                    "Number to reset:  (type 0 to wipe ALL unpushed)",
                    inner_w),
                sb | curses.A_DIM)
    line += 1

    visible = prompt.typed if prompt.typed else ""
    field_text = f" {visible} "
    safe_addstr(stdscr, line, inner_x, field_text.ljust(inner_w),
                sb | curses.A_REVERSE)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(prompt), attr=sb | curses.A_DIM)


def handle_reset_prompt_key(state: State, key: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    if key == 27:
        state.reset_prompt = None
        return
    if key in (10, 13, curses.KEY_ENTER):
        if not prompt.typed:
            return
        try:
            n = int(prompt.typed)
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
