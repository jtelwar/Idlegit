"""Clone modal key handling and worker dispatch."""
from __future__ import annotations

import curses
from pathlib import Path

from core.state.app import State
from core.state.clone import CloneModal
from core.workers import kick_off_clone

from .projection import (
    BRANCH_CHARS,
    FIELD_BRANCH,
    FIELD_BUTTON,
    FIELD_DEST,
    FIELD_RECURSE,
    FIELD_URL,
    N_FIELDS,
    can_clone,
    default_dest,
)
from .session import close_clone_modal


def handle_clone_modal_key(state: State, key: int) -> None:
    modal = state.clone_modal
    if modal is None:
        return

    if modal.edit_field:
        if key in (10, 13, curses.KEY_ENTER):
            commit_edit(modal)
            return
        if key == 27:
            cancel_edit(modal)
            return
        handle_typing(modal, key)
        return

    if key in (27, 9):
        close_clone_modal(state)
        return
    if key == curses.KEY_UP:
        modal.selected = max(0, modal.selected - 1)
        return
    if key == curses.KEY_DOWN:
        modal.selected = min(N_FIELDS - 1, modal.selected + 1)
        return

    selected = modal.selected
    if selected == FIELD_URL and key in (10, 13, curses.KEY_ENTER):
        enter_edit(modal, "url")
        return
    if selected == FIELD_DEST and key in (10, 13, curses.KEY_ENTER):
        if not modal.dest_text.strip():
            modal.dest_text = default_dest(modal)
        enter_edit(modal, "dest")
        return
    if selected == FIELD_BRANCH and key in (10, 13, curses.KEY_ENTER):
        enter_edit(modal, "branch")
        return
    if selected == FIELD_RECURSE and key in (ord(" "), 10, 13, curses.KEY_ENTER):
        modal.recurse_submodules = not modal.recurse_submodules
        return
    if selected == FIELD_BUTTON and key in (10, 13, curses.KEY_ENTER):
        try_clone(state)


def enter_edit(modal: CloneModal, field: str) -> None:
    modal.edit_field = field
    if field == "url":
        modal.edit_pre_value = modal.url
    elif field == "dest":
        modal.edit_pre_value = modal.dest_text
    elif field == "branch":
        modal.edit_pre_value = modal.branch
    modal.error = ""


def commit_edit(modal: CloneModal) -> None:
    if modal.edit_field == "url" and not modal.dest_text.strip():
        modal.dest_text = default_dest(modal)
    modal.edit_field = ""
    modal.edit_pre_value = ""


def cancel_edit(modal: CloneModal) -> None:
    if modal.edit_field == "url":
        modal.url = modal.edit_pre_value
    elif modal.edit_field == "dest":
        modal.dest_text = modal.edit_pre_value
    elif modal.edit_field == "branch":
        modal.branch = modal.edit_pre_value
    modal.edit_field = ""
    modal.edit_pre_value = ""


def handle_typing(modal: CloneModal, key: int) -> None:
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if modal.edit_field == "url":
            modal.url = modal.url[:-1]
        elif modal.edit_field == "dest":
            modal.dest_text = modal.dest_text[:-1]
        elif modal.edit_field == "branch":
            modal.branch = modal.branch[:-1]
        return
    if 32 <= key < 127:
        char = chr(key)
        if modal.edit_field == "url":
            modal.url += char
        elif modal.edit_field == "dest":
            modal.dest_text += char
        elif modal.edit_field == "branch":
            if not modal.branch and char == "-":
                return
            if char in BRANCH_CHARS:
                modal.branch += char


def try_clone(state: State) -> None:
    modal = state.clone_modal
    if modal is None or not can_clone(modal):
        return
    destination = Path(modal.dest_text.strip()).expanduser()
    modal.error = ""
    modal.cloning = True

    def on_done(ok: bool, message: str) -> None:
        modal.cloning = False
        if not ok:
            modal.error = message or "clone failed"
            return
        state.clone_modal = None

    kick_off_clone(
        state,
        modal.url.strip(),
        destination,
        modal.branch.strip(),
        modal.recurse_submodules,
        on_done=on_done,
    )
