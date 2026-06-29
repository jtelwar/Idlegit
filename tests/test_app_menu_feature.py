from __future__ import annotations

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
from core.state.workspaces import Workspace  # noqa: E402
from features.app_menu.actions import (  # noqa: E402
    fire_app_action,
    handle_app_menu_key,
)
from features.app_menu.projection import (  # noqa: E402
    ACTION_ADJUST_PERIODIC_REFRESH,
    ACTION_CYCLE_AUTO_REMOVE_COMPLETED,
    ACTION_OPEN_HELP,
    ACTION_TOGGLE_AUTO_REFRESH,
    ACTION_UPDATE_NOW,
    build_app_menu_rows,
    rebuild_app_menu_rows,
)
from features.app_menu.session import open_app_menu_session  # noqa: E402


class TestAppMenuProjection(unittest.TestCase):
    def _state(self):
        workspaces = [
            Workspace(name="A", folders=[Path("/a")]),
            Workspace(name="B", folders=[Path("/b")]),
        ]
        return _state(
            _make_repo("repo"),
            workspaces=workspaces,
            active_workspace_index=1,
        )

    def test_open_session_installs_rows_and_schedules_status_refresh(self) -> None:
        s = self._state()

        with mock.patch(
                "features.app_menu.session.kick_off_app_menu_status_refresh") as kickoff:
            result = open_app_menu_session(s)

        self.assertFalse(result.open_workspace_creator)
        self.assertIsNotNone(s.app_menu)
        self.assertEqual(s.app_menu.rows[s.app_menu.selected].kind, "workspace")
        self.assertEqual(s.app_menu.rows[s.app_menu.selected].attr_name, "1")
        kickoff.assert_called_once_with(s, s.app_menu)

    def test_open_session_without_workspaces_requests_creator_handoff(self) -> None:
        s = _state(_make_repo("repo"))

        result = open_app_menu_session(s)

        self.assertTrue(result.open_workspace_creator)
        self.assertIsNone(s.app_menu)

    def test_projection_marks_periodic_refresh_off(self) -> None:
        s = self._state()
        s.periodic_refresh_seconds = 0
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)

        row = next(row for row in s.app_menu.rows
                   if row.attr_name == ACTION_ADJUST_PERIODIC_REFRESH)

        self.assertEqual(row.label, "Periodic refresh: 0s (OFF)")

    def test_projection_uses_successful_task_removal_label(self) -> None:
        s = self._state()
        s.auto_remove_completed_after = 6
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)

        row = next(row for row in s.app_menu.rows
                   if row.attr_name == ACTION_CYCLE_AUTO_REMOVE_COMPLETED)

        self.assertEqual(row.label, "Remove successful tasks: 6s")

    def test_rebuild_preserves_selected_action_by_identity(self) -> None:
        s = self._state()
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)
        menu = s.app_menu
        self.assertIsNotNone(menu)
        menu.selected = next(i for i, row in enumerate(menu.rows)
                             if row.attr_name == ACTION_OPEN_HELP)
        menu.update_check = "done"
        menu.latest_version = "v999.0.0"

        rebuild_app_menu_rows(s)

        selected = menu.rows[menu.selected]
        self.assertEqual(selected.kind, "app_action")
        self.assertEqual(selected.attr_name, ACTION_OPEN_HELP)
        labels = [row.label for row in build_app_menu_rows(s, menu)]
        self.assertIn("Update available: v999.0.0", labels)


class TestAppMenuActions(unittest.TestCase):
    def _state(self):
        workspaces = [
            Workspace(name="A", folders=[Path("/a")]),
            Workspace(name="B", folders=[Path("/b")]),
        ]
        return _state(
            _make_repo("repo"),
            workspaces=workspaces,
            active_workspace_index=0,
        )

    def test_update_now_returns_ui_effect(self) -> None:
        s = self._state()
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)

        effect = fire_app_action(s, ACTION_UPDATE_NOW)

        self.assertEqual(effect.kind, "update_now")

    def test_open_help_closes_menu_and_returns_ui_effect(self) -> None:
        s = self._state()
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)

        effect = fire_app_action(s, ACTION_OPEN_HELP)

        self.assertEqual(effect.kind, "open_help")
        self.assertIsNone(s.app_menu)

    def test_auto_refresh_toggle_schedules_feature_worker(self) -> None:
        s = self._state()
        s.auto_refresh_on_fs_change = False
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)

        with mock.patch(
                "features.app_menu.actions.kick_off_auto_refresh_toggle") as kickoff:
            effect = fire_app_action(s, ACTION_TOGGLE_AUTO_REFRESH)

        self.assertEqual(effect.kind, "none")
        self.assertTrue(s.auto_refresh_on_fs_change)
        kickoff.assert_called_once_with(s, True)

    def test_enter_on_inactive_workspace_switches_from_feature_boundary(self) -> None:
        s = self._state()
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu_session(s)
        menu = s.app_menu
        self.assertIsNotNone(menu)
        menu.selected = next(i for i, row in enumerate(menu.rows)
                             if row.kind == "workspace" and row.attr_name == "1")

        with mock.patch("features.app_menu.actions.switch_workspace") as switch:
            effect = handle_app_menu_key(s, 10)

        self.assertEqual(effect.kind, "close")
        self.assertIsNone(s.app_menu)
        switch.assert_called_once_with(s, 1)


if __name__ == "__main__":
    unittest.main()
