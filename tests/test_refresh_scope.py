"""Workspace refresh scope tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from core.refresh_scope import WorkspaceRefreshScope  # noqa: E402


class TestWorkspaceRefreshScope(unittest.TestCase):
    def test_scope_is_not_current_after_workspace_switch(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        ws_a = Workspace(name="A", folders=[Path("/a")], cached_repos=[repo_a])
        ws_b = Workspace(name="B", folders=[Path("/b")], cached_repos=[repo_b])
        state = State(
            repos=[repo_a],
            workspace_name="A",
            workspaces=[ws_a, ws_b],
            active_workspace_index=0,
        )
        scope = WorkspaceRefreshScope.capture(state)

        state.active_workspace_index = 1
        state.repos = [repo_b]

        self.assertTrue(scope.is_current(state))
        self.assertFalse(scope.is_active_current(state))
        self.assertTrue(scope.update_cache_if_current(state, [repo_a]))
        self.assertIs(ws_a.cached_repos[0], repo_a)
        self.assertFalse(scope.publish_live_if_active(state, [_make_repo("new-a")]))
        self.assertEqual(state.repos, [repo_b])

    def test_scope_rejects_changed_workspace_identity(self) -> None:
        repo = _make_repo("repo")
        ws = Workspace(name="A", folders=[Path("/a")], cached_repos=[repo])
        state = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )
        scope = WorkspaceRefreshScope.capture(state)

        state.workspaces[0] = Workspace(name="A", folders=[Path("/different")])

        self.assertFalse(scope.is_current(state))
        self.assertFalse(scope.update_cache_if_current(state, [_make_repo("fresh")]))

    def test_scope_rejects_renamed_legacy_workspace_state(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="legacy")
        scope = WorkspaceRefreshScope.capture(state)

        state.workspace_name = "other"

        self.assertFalse(scope.is_current(state))
        self.assertFalse(scope.is_active_current(state))

    def test_publish_live_preserves_focus_by_repo_path(self) -> None:
        old_a = _make_repo("a")
        old_b = _make_repo("b")
        fresh_a = _make_repo("a")
        fresh_b = _make_repo("b")
        ws = Workspace(name="A", folders=[Path("/a")], cached_repos=[old_a, old_b])
        state = State(
            repos=[old_a, old_b],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )
        state.selected = 1
        called = []
        scope = WorkspaceRefreshScope.capture(state)

        published = scope.publish_live_if_active(
            state,
            [fresh_a, fresh_b],
            on_published=lambda _state: called.append("watchers"),
        )

        self.assertTrue(published)
        self.assertEqual(state.repos, [fresh_a, fresh_b])
        self.assertEqual(state.selected, 1)
        self.assertEqual(called, ["watchers"])


if __name__ == "__main__":
    unittest.main()
