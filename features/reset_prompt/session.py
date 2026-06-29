"""Soft-reset prompt session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.prompts import ResetPrompt


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


def close_reset_prompt(state: State) -> None:
    state.reset_prompt = None
