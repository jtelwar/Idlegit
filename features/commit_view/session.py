"""Commit-view session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.views import CommitViewModal
from core.workers import kick_off_load_commit_view


def open_commit_view_modal(
    state: State,
    target_path,
    target_label: str,
    sha: str,
    subject: str = "",
) -> None:
    if not sha or sha.startswith("-"):
        return
    base_load_id = f"commit-view:{id(state)}:{id(target_path)}:{sha}"
    modal = CommitViewModal(
        target_label=target_label,
        target_path=target_path,
        sha=sha,
        subject=subject,
        tags_load_id=f"{base_load_id}:tags",
        details_load_id=f"{base_load_id}:details",
        files_load_id=f"{base_load_id}:files",
        reflog_load_id=f"{base_load_id}:reflog",
    )
    state.commit_view_modal = modal
    kick_off_load_commit_view(state, modal)
