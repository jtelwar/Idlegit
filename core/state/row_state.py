"""State-owned helpers for row refresh ownership."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.state.app import State
    from .repos import ChildRef, Repo


def set_repo_refreshing(state: "State", repo: "Repo", value: bool) -> None:
    """Set a repo row's store-owned busy state."""
    state.store.set_repo_busy(repo, value)


def set_child_refreshing(state: "State", child: "ChildRef", value: bool) -> None:
    """Set a child row's store-owned busy state."""
    state.store.set_child_busy(child, value)


def set_canonical_tree_refreshing(
        state: "State",
        canonical: "Repo",
        value: bool,
) -> None:
    """Set a canonical repo and every nested row pointing at it refreshing."""
    set_repo_refreshing(state, canonical, value)
    canonical_id = state.store.repo_id_for(canonical)
    workspace_id = state.store.active_workspace_id
    if canonical_id is None or workspace_id is None:
        return
    for repo_record in state.store.repo_records_for_workspace(workspace_id):
        for child_record in state.store.child_records_for_repo(
                repo_record.repo_id):
            if child_record.repo_id != canonical_id:
                continue
            status = state.store.child_status_by_id(child_record.child_id)
            kind = child_record.kind if status is None else status.kind
            if kind == "submodule":
                set_child_refreshing(state, child_record.child, value)
