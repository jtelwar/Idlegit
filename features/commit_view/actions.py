"""Commit-view key handling and mutation dispatch."""
from __future__ import annotations

import curses
from dataclasses import dataclass

from core.state.app import State
from core.state.views import CommitViewModal
from core.workers import kick_off_add_tag

from .projection import (
    VALID_TAG_CHARS,
    build_action_items,
    commit_view_load_ids,
    cycle_tab_id,
)


@dataclass(frozen=True)
class CommitViewEffect:
    kind: str = "none"
    target_path: object | None = None
    label: str = ""
    file_path: str = ""
    commit_sha: str = ""


NO_EFFECT = CommitViewEffect()


def close_modal(state: State) -> None:
    modal = state.commit_view_modal
    if modal is None:
        return
    state.view_loads.remove_many(commit_view_load_ids(modal))
    state.commit_view_modal = None


def begin_add_tag(modal: CommitViewModal) -> None:
    modal.edit_field = "add_tag"
    modal.edit_typed = ""


def cancel_inline(modal: CommitViewModal) -> None:
    modal.edit_field = ""
    modal.edit_typed = ""


def request_confirm(
    modal: CommitViewModal,
    message: str,
    action: str,
    args: dict[str, str],
) -> None:
    modal.confirm_message = message
    modal.confirm_action = action
    modal.confirm_args = dict(args)


def clear_confirm(modal: CommitViewModal) -> None:
    modal.confirm_message = ""
    modal.confirm_action = ""
    modal.confirm_args = {}


def apply_pending(state: State, modal: CommitViewModal) -> None:
    if modal.confirm_action == "add_tag":
        name = modal.confirm_args.get("name", "")
        sha = modal.confirm_args.get("sha", "")
        kick_off_add_tag(
            state,
            target_label=modal.target_label,
            target_path=modal.target_path,
            target_repo=None,
            target_parent=None,
            name=name,
            sha=sha,
        )
        if name and name not in modal.tags:
            modal.tags = list(modal.tags) + [name]
    clear_confirm(modal)


def handle_confirm(state: State, modal: CommitViewModal, key: int) -> None:
    if key in (ord("y"), ord("Y")):
        apply_pending(state, modal)
        return
    if key in (ord("n"), ord("N"), 27):
        clear_confirm(modal)


def handle_inline_edit(state: State, modal: CommitViewModal, key: int) -> None:
    if key == 27:
        cancel_inline(modal)
        return
    if key in (10, 13, curses.KEY_ENTER):
        text = modal.edit_typed.strip()
        if not text or text.startswith("-"):
            return
        cancel_inline(modal)
        request_confirm(
            modal,
            f"Add tag {text} → {modal.sha[:8]}? [y/N]",
            "add_tag",
            {"name": text, "sha": modal.sha},
        )
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        modal.edit_typed = modal.edit_typed[:-1]
        return
    if 32 <= key < 127:
        char = chr(key)
        if not modal.edit_typed and char == "-":
            return
        if char in VALID_TAG_CHARS:
            modal.edit_typed += char


def open_diff_for_focused_file(modal: CommitViewModal) -> CommitViewEffect:
    if not modal.files:
        return NO_EFFECT
    if not (0 <= modal.file_selected < len(modal.files)):
        return NO_EFFECT
    file_entry = modal.files[modal.file_selected]
    return CommitViewEffect(
        kind="open_diff",
        target_path=modal.target_path,
        label=f"{modal.target_label} · {modal.sha[:8]}",
        file_path=file_entry.path,
        commit_sha=modal.sha,
    )


def handle_commit_view_modal_key(state: State, key: int) -> CommitViewEffect:
    modal = state.commit_view_modal
    if modal is None:
        return NO_EFFECT

    if modal.confirm_message:
        handle_confirm(state, modal, key)
        return NO_EFFECT
    if modal.edit_field:
        handle_inline_edit(state, modal, key)
        return NO_EFFECT

    if key == 27:
        close_modal(state)
        return NO_EFFECT

    if modal.section == "actions":
        return handle_actions_section_key(state, modal, key)
    return handle_tabs_section_key(state, modal, key)


def handle_actions_section_key(
    state: State,
    modal: CommitViewModal,
    key: int,
) -> CommitViewEffect:
    if key == 9:
        close_modal(state)
        return NO_EFFECT
    items = build_action_items()
    count = len(items)
    if key == curses.KEY_UP and count > 0:
        modal.action_selected = max(0, modal.action_selected - 1)
        return NO_EFFECT
    if key == curses.KEY_DOWN and count > 0:
        if modal.action_selected >= count - 1:
            modal.section = "tabs"
            if modal.files and modal.file_selected >= len(modal.files):
                modal.file_selected = 0
            return NO_EFFECT
        modal.action_selected += 1
        return NO_EFFECT
    if key in (10, 13, curses.KEY_ENTER) and count > 0:
        item = items[modal.action_selected]
        if item.id == "add_tag":
            begin_add_tag(modal)
    return NO_EFFECT


def handle_tabs_section_key(
    state: State,
    modal: CommitViewModal,
    key: int,
) -> CommitViewEffect:
    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        modal.active_tab = cycle_tab_id(
            modal.active_tab,
            -1 if key == curses.KEY_LEFT else 1,
        )
        return NO_EFFECT
    if key == 9:
        if modal.active_tab == "changes":
            return open_diff_for_focused_file(modal)
        close_modal(state)
        return NO_EFFECT
    if key == curses.KEY_HOME:
        modal.section = "actions"
        modal.action_selected = 0
        return NO_EFFECT
    if modal.active_tab == "changes":
        handle_changes_key(modal, key)
        return NO_EFFECT
    handle_reflog_key(modal, key)
    return NO_EFFECT


def handle_changes_key(modal: CommitViewModal, key: int) -> None:
    if key == curses.KEY_UP:
        if modal.file_selected <= 0:
            modal.section = "actions"
            modal.file_selected = 0
            return
        modal.file_selected -= 1
        return
    if key == curses.KEY_DOWN:
        if modal.files and modal.file_selected < len(modal.files) - 1:
            modal.file_selected += 1
        return
    if key == curses.KEY_PPAGE:
        modal.file_selected = max(0, modal.file_selected - 10)
        return
    if key == curses.KEY_NPAGE and modal.files:
        modal.file_selected = min(
            len(modal.files) - 1, modal.file_selected + 10)


def handle_reflog_key(modal: CommitViewModal, key: int) -> None:
    if key == curses.KEY_UP:
        if modal.reflog_scroll <= 0:
            modal.section = "actions"
            return
        modal.reflog_scroll -= 1
        return
    if key == curses.KEY_DOWN:
        modal.reflog_scroll += 1
        return
    if key == curses.KEY_PPAGE:
        modal.reflog_scroll = max(0, modal.reflog_scroll - 10)
        return
    if key == curses.KEY_NPAGE:
        modal.reflog_scroll += 10
