"""Branch-name prompt key handling and action dispatch."""
from __future__ import annotations

import curses

from core.state.app import State
from core.workers import kick_off_action
from features.action_menu.session import close_action_menu

from .session import close_branch_name_prompt


VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def handle_branch_name_prompt_key(state: State, key: int) -> None:
    prompt = state.branch_name_prompt
    if prompt is None:
        return
    if key == 27:
        close_branch_name_prompt(state)
        return
    if key in (10, 13, curses.KEY_ENTER):
        submit_branch_name_prompt(state)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        prompt.typed = prompt.typed[:-1]
        return
    if 32 <= key < 127:
        ch = chr(key)
        if not prompt.typed and ch == "-":
            return
        if ch in VALID_NAME_CHARS:
            prompt.typed += ch


def submit_branch_name_prompt(state: State) -> None:
    prompt = state.branch_name_prompt
    if prompt is None:
        return
    name = prompt.typed.strip() or prompt.default_name
    if not name or name.startswith("-"):
        return
    if prompt.mode == "rename":
        if name == prompt.current_branch:
            close_branch_name_prompt(state)
            close_action_menu(state)
            return
        action_id = "rename_branch"
    else:
        action_id = "branch_from_head"
    kick_off_action(
        state,
        action_id,
        target_label=prompt.target_label,
        target_path=prompt.target_path,
        target_repo=prompt.target_repo,
        target_parent=prompt.target_parent,
        branch_arg=name,
    )
    close_branch_name_prompt(state)
    close_action_menu(state)
