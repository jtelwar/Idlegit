from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.workspaces import Workspace, WorkspaceSwitcher  # noqa: E402
from features.workspace_switcher.actions import (  # noqa: E402
    handle_workspace_switcher_key,
)
from features.workspace_switcher.projection import (  # noqa: E402
    clamped_active_workspace_index,
    workspace_switcher_hint_specs,
)
from features.workspace_switcher.session import (  # noqa: E402
    close_workspace_switcher,
    open_workspace_switcher,
)


class TestWorkspaceSwitcherFeature(unittest.TestCase):
    def _state(self, active: int = 1):
        workspaces = [
            Workspace(name="One", folders=[Path("/one")]),
            Workspace(name="Two", folders=[Path("/two")]),
            Workspace(name="Three", folders=[Path("/three")]),
        ]
        return _state(
            _make_repo("repo"),
            workspaces=workspaces,
            active_workspace_index=active,
        )

    def test_open_session_selects_clamped_active_workspace(self) -> None:
        state = self._state(active=99)

        open_workspace_switcher(state)

        self.assertEqual(clamped_active_workspace_index(state), 2)
        self.assertIsNotNone(state.workspace_switcher)
        self.assertEqual(state.workspace_switcher.selected, 2)

    def test_open_session_noops_without_workspaces(self) -> None:
        state = _state(_make_repo("repo"), workspaces=[])

        open_workspace_switcher(state)

        self.assertIsNone(state.workspace_switcher)

    def test_close_session_clears_switcher(self) -> None:
        state = self._state()
        state.workspace_switcher = WorkspaceSwitcher()

        close_workspace_switcher(state)

        self.assertIsNone(state.workspace_switcher)

    def test_projection_hints_describe_active_and_target_rows(self) -> None:
        state = self._state(active=1)
        active_switcher = WorkspaceSwitcher(selected=1)
        target_switcher = WorkspaceSwitcher(selected=2)

        self.assertIn(
            ("Enter", "stay (already active)"),
            workspace_switcher_hint_specs(state, active_switcher),
        )
        self.assertIn(
            ("Enter", "switch to Three"),
            workspace_switcher_hint_specs(state, target_switcher),
        )

    def test_key_handler_moves_selection(self) -> None:
        state = self._state(active=1)
        open_workspace_switcher(state)

        handle_workspace_switcher_key(state, curses.KEY_UP)
        self.assertEqual(state.workspace_switcher.selected, 0)

        handle_workspace_switcher_key(state, curses.KEY_END)
        self.assertEqual(state.workspace_switcher.selected, 2)

    def test_enter_on_active_workspace_closes_without_switching(self) -> None:
        state = self._state(active=1)
        open_workspace_switcher(state)

        with mock.patch(
                "features.workspace_switcher.actions.switch_workspace"
        ) as switch:
            result = handle_workspace_switcher_key(state, 10)

        switch.assert_not_called()
        self.assertIsNone(result)
        self.assertIsNone(state.workspace_switcher)

    def test_enter_on_other_workspace_switches_and_returns_signal(self) -> None:
        state = self._state(active=1)
        open_workspace_switcher(state)
        state.workspace_switcher.selected = 2

        with mock.patch(
                "features.workspace_switcher.actions.switch_workspace"
        ) as switch:
            result = handle_workspace_switcher_key(state, 10)

        switch.assert_called_once_with(state, 2)
        self.assertEqual(result, "switch-workspace")
        self.assertIsNone(state.workspace_switcher)

    def test_escape_closes_without_switching(self) -> None:
        state = self._state(active=1)
        open_workspace_switcher(state)

        with mock.patch(
                "features.workspace_switcher.actions.switch_workspace"
        ) as switch:
            result = handle_workspace_switcher_key(state, 27)

        switch.assert_not_called()
        self.assertIsNone(result)
        self.assertIsNone(state.workspace_switcher)


if __name__ == "__main__":
    unittest.main()
