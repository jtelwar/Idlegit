"""Soft-reset count prompt — sub-modal of the action menu."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.prompts import ResetPrompt
from features.reset_prompt.actions import (
    handle_reset_prompt_key as handle_reset_prompt_key_action,
)
from features.reset_prompt.projection import (
    reset_prompt_hint_specs,
    reset_prompt_title,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import Hint, render_hints

_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 60


def _hints(prompt: ResetPrompt) -> list:
    """Footer hints for the soft-reset count prompt. Enter's effect
    differs based on whether the user has typed digits yet — describe
    each case explicitly so it's clear what happens on submit."""
    return [Hint(keys, action)
            for keys, action in reset_prompt_hint_specs(prompt)]


def _title_lines(prompt: ResetPrompt, inner_w: int) -> "list[str]":
    return wrap_label_value(reset_prompt_title(prompt), prompt.target_label,
                            inner_w)


def draw_reset_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
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
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
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
    handle_reset_prompt_key_action(state, key)
