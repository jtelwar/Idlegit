"""Soft-reset prompt key handling and dispatch."""
from __future__ import annotations

import curses

from core.state.app import State
from core.workers import kick_off_action

from .projection import reset_count_from_typed
from .session import close_reset_prompt


def handle_reset_prompt_key(state: State, key: int) -> None:
    prompt = state.reset_prompt
    if prompt is None:
        return
    if key == 27:
        close_reset_prompt(state)
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_reset_prompt(state)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        prompt.typed = prompt.typed[:-1]
        return
    if 48 <= key <= 57:
        prompt.typed += chr(key)


def submit_reset_prompt(state: State) -> None:
    prompt = state.reset_prompt
    if prompt is None or not prompt.typed:
        return
    kick_off_action(
        state,
        "soft_reset",
        target_label=prompt.target_label,
        target_path=prompt.target_path,
        target_repo=prompt.target_repo,
        target_parent=prompt.target_parent,
        reset_count=reset_count_from_typed(prompt.typed),
    )
    state.reset_prompt = None
    state.action_menu = None

