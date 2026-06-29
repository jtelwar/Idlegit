"""Commit message editor session lifecycle."""
from __future__ import annotations

from typing import Optional

from core.state.app import State
from core.state.edit_buffers import CommitMsgEditor
from core.state.repos import ChildRef, Repo
from core.state.selectors import child_row_state, repo_row_state

from .projection import holder_message


def focused_holder(
    state: State,
) -> tuple[Optional[object], Optional[Repo], str, str]:
    if state.focused_panel != "repos":
        return None, None, "", ""
    if state.on_title_row or state.on_workspace_row:
        return None, None, "", ""
    current_repo = state.current_repo
    if current_repo is not None:
        status = state.store.repo_status(current_repo)
        branch = "" if status is None else status.branch
        return current_repo, None, current_repo.display_name, branch
    current_child = state.current_child
    if current_child is not None:
        parent_repo, child = current_child
        status = state.store.child_status(child)
        if status is None or status.kind != "submodule":
            return None, None, "", ""
        nested = child.repo.display_name
        label = f"{parent_repo.display_name} / {nested}"
        canonical_status = state.store.repo_status(child.repo)
        fallback_branch = "" if canonical_status is None else canonical_status.branch
        branch = status.branch or fallback_branch
        return child, parent_repo, label, branch
    return None, None, "", ""


def holder_is_editable(state: State, holder) -> bool:
    if isinstance(holder, Repo):
        return repo_row_state(state, holder).show_message_field
    if isinstance(holder, ChildRef):
        return child_row_state(state, holder).show_message_field
    return False


def holder_is_busy(state: State, holder) -> bool:
    if isinstance(holder, Repo):
        return repo_row_state(state, holder).busy
    if isinstance(holder, ChildRef):
        return child_row_state(state, holder).busy
    return False


def open_commit_msg_editor(state: State) -> bool:
    holder, parent, label, branch = focused_holder(state)
    if holder is None:
        return False
    if holder_is_busy(state, holder):
        return False
    if not holder_is_editable(state, holder):
        return False
    msg = holder_message(state, holder)
    state.commit_msg_editor = CommitMsgEditor(
        holder=holder,
        parent=parent,
        label=label,
        branch=branch or "",
        cursor=len(msg),
        scroll=0,
    )
    return True
