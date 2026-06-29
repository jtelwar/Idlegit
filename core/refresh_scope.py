"""Workspace-scoped refresh generation guards."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from core.state.app import State
from .state.repos import Repo
from .state.workspaces import SubtreeSpec, Workspace


@dataclass(frozen=True)
class WorkspaceRefreshScope:
    """Identity snapshot for applying async refresh results safely."""

    target_idx: int
    target_ws: Optional[Workspace]
    workspace_name: str
    folders: Tuple[Path, ...]
    subtrees: Tuple[SubtreeSpec, ...]

    @classmethod
    def capture(cls, state: State) -> "WorkspaceRefreshScope":
        target_ws = state.active_workspace
        subtrees = tuple(target_ws.subtrees) if target_ws is not None else tuple(state.subtrees)
        return cls(
            target_idx=state.active_workspace_index,
            target_ws=target_ws,
            workspace_name=state.workspace_name,
            folders=tuple(state.active_folders),
            subtrees=subtrees,
        )

    def is_current(self, state: State) -> bool:
        """Return True when the original workspace identity is still valid."""
        if self.target_ws is None:
            return not state.workspaces and state.workspace_name == self.workspace_name
        return (
            0 <= self.target_idx < len(state.workspaces)
            and state.workspaces[self.target_idx] is self.target_ws
            and tuple(state.workspaces[self.target_idx].folders) == self.folders
            and tuple(state.workspaces[self.target_idx].subtrees) == self.subtrees
        )

    def is_active_current(self, state: State) -> bool:
        return self.is_current(state) and state.active_workspace_index == self.target_idx

    def update_cache_if_current(self, state: State, repos: List[Repo]) -> bool:
        if not self.is_current(state):
            return False
        if self.target_ws is not None:
            state.workspaces[self.target_idx].cached_repos = repos
        return True

    def publish_live_if_active(
            self,
            state: State,
            repos: List[Repo],
            *,
            on_published: Optional[Callable[[State], None]] = None,
    ) -> bool:
        """Swap live repos only when this scope is still the active workspace."""
        if not self.is_active_current(state):
            return False
        focus_key = state.body_focus_key()
        state.replace_repos(repos, workspace=self.target_ws)
        if focus_key is not None:
            state.restore_body_focus(focus_key)
        elif state.selected >= 0:
            rows = state.selectable_rows()
            if rows:
                state.selected = max(0, min(state.selected, len(rows) - 1))
        if on_published is not None:
            on_published(state)
        return True
