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

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.config import Config  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from features.workspace_menu.actions import handle_workspace_menu_key  # noqa: E402
from features.workspace_menu.projection import build_rows  # noqa: E402
from features.workspace_menu.session import open_workspace_menu  # noqa: E402


class TestWorkspaceMenuProjection(unittest.TestCase):
    def test_build_rows_includes_workspace_sections_and_settings(self) -> None:
        ws = Workspace(
            name="W",
            folders=[Path("/tmp"), Path("/var")],
            fs_watch_ignore=["*.log"],
        )

        rows = build_rows(ws)

        kinds = [row.kind for row in rows]
        labels = [row.label for row in rows]
        self.assertEqual(kinds.count("folder"), 2)
        self.assertEqual(kinds.count("ignore_pattern"), 1)
        self.assertIn("+ Clone repository…", labels)
        self.assertIn("Auto-push submodule parent", labels)


class TestWorkspaceMenuSession(unittest.TestCase):
    def test_open_menu_builds_drafts_and_dispatches_path_checks(self) -> None:
        state = self._state([Path("/tmp"), Path("/var")])

        with mock.patch(
            "features.workspace_menu.session.kick_off_workspace_path_check",
        ) as kick:
            open_workspace_menu(state)

        self.assertIsNotNone(state.workspace_menu)
        self.assertEqual(
            [draft.path_text for draft in state.workspace_menu.path_drafts],
            ["/tmp", "/var"],
        )
        self.assertEqual(kick.call_count, 2)

    def _state(self, folders: list[Path]) -> State:
        cfg = Config()
        ws = Workspace(name="W", folders=folders)
        return _state(
            _make_repo("r"),
            workspaces=[ws],
            active_workspace_index=0,
            base_config=cfg,
        )


class TestWorkspaceMenuActions(unittest.TestCase):
    def test_toggle_bool_persists_through_feature_worker_dispatch(self) -> None:
        state = self._state([Path("/tmp")])
        with mock.patch("features.workspace_menu.session.kick_off_workspace_path_check"):
            open_workspace_menu(state)
        self._focus_attr(state, "default_auto_stage")

        with mock.patch(
            "features.workspace_menu.actions.kick_off_workspace_settings_save",
        ) as save:
            handle_workspace_menu_key(state, ord(" "))

        self.assertIn("default_auto_stage", state.active_workspace.overrides)
        save.assert_called_once_with(state)

    def test_clone_row_returns_ui_effect_without_opening_clone(self) -> None:
        state = self._state([Path("/tmp")])
        with mock.patch("features.workspace_menu.session.kick_off_workspace_path_check"):
            open_workspace_menu(state)
        self._focus_kind(state, "clone")

        effect = handle_workspace_menu_key(state, curses.KEY_ENTER)

        self.assertEqual(effect.kind, "open_clone")
        self.assertIsNone(state.clone_modal)

    def test_folder_edit_commits_and_rechecks_via_feature_session(self) -> None:
        state = self._state([Path("/tmp")])
        with mock.patch("features.workspace_menu.session.kick_off_workspace_path_check"):
            open_workspace_menu(state)
        self._focus_kind(state, "folder")
        handle_workspace_menu_key(state, curses.KEY_ENTER)
        state.workspace_menu.edit_buffer = "/var"
        state.workspace_menu.edit_cursor = len("/var")

        with (
            mock.patch(
                "features.workspace_menu.actions.kick_off_workspace_settings_save",
            ),
            mock.patch(
                "features.workspace_menu.session.kick_off_workspace_path_check",
            ) as kick,
        ):
            handle_workspace_menu_key(state, curses.KEY_ENTER)

        self.assertFalse(state.workspace_menu.editing)
        self.assertEqual(state.active_workspace.folders, [Path("/var")])
        kick.assert_called_once()

    def _state(self, folders: list[Path]) -> State:
        cfg = Config()
        ws = Workspace(name="W", folders=folders)
        state = _state(
            _make_repo("r"),
            workspaces=[ws],
            active_workspace_index=0,
            base_config=cfg,
        )
        state.auto_stage = cfg.default_auto_stage
        return state

    def _focus_attr(self, state: State, attr_name: str) -> None:
        menu = state.workspace_menu
        for i, row in enumerate(menu.rows):
            if row.attr_name == attr_name and row.kind != "header":
                menu.selected = i
                return
        raise AssertionError(f"missing row {attr_name}")

    def _focus_kind(self, state: State, kind: str) -> None:
        menu = state.workspace_menu
        for i, row in enumerate(menu.rows):
            if row.kind == kind:
                menu.selected = i
                return
        raise AssertionError(f"missing row kind {kind}")
