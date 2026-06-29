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
from core.state.clone import CloneModal  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from features.clone_modal.actions import (  # noqa: E402
    cancel_edit,
    commit_edit,
    enter_edit,
    handle_clone_modal_key,
    handle_typing,
    try_clone,
)
from features.clone_modal.projection import (  # noqa: E402
    FIELD_BUTTON,
    FIELD_DEST,
    FIELD_RECURSE,
    FIELD_URL,
    can_clone,
    clone_modal_hint_specs,
    default_dest,
    name_from_url,
)
from features.clone_modal.session import (  # noqa: E402
    close_clone_modal,
    open_clone_modal,
)


class TestCloneModalFeature(unittest.TestCase):
    def _state(self):
        workspace = Workspace(
            name="Main",
            folders=[Path("/workspace")],
        )
        return _state(
            _make_repo("repo"),
            workspaces=[workspace],
            active_workspace_index=0,
        )

    def _modal(self) -> CloneModal:
        return CloneModal(
            workspace_name="Main",
            workspace_folders=[Path("/workspace")],
            url="git@github.com:org/project.git",
            dest_text="/workspace/project",
        )

    def test_open_session_uses_active_workspace_folders(self) -> None:
        state = self._state()

        open_clone_modal(state)

        self.assertIsNotNone(state.clone_modal)
        self.assertEqual(state.clone_modal.workspace_name, "Main")
        self.assertEqual(state.clone_modal.workspace_folders, [Path("/workspace")])
        self.assertEqual(state.clone_modal.selected, FIELD_URL)

    def test_close_session_clears_modal(self) -> None:
        state = self._state()
        state.clone_modal = self._modal()

        close_clone_modal(state)

        self.assertIsNone(state.clone_modal)

    def test_projection_derives_name_and_default_destination(self) -> None:
        modal = self._modal()

        self.assertEqual(name_from_url("https://github.com/org/repo.git"), "repo")
        self.assertEqual(default_dest(modal), "/workspace/project")
        self.assertTrue(can_clone(modal))
        self.assertIn(("Enter", "run clone"), clone_modal_hint_specs(
            CloneModal(
                workspace_name="Main",
                workspace_folders=[Path("/workspace")],
                url="u",
                dest_text="/workspace/u",
                selected=FIELD_BUTTON,
            )))

    def test_commit_url_edit_populates_default_destination(self) -> None:
        modal = CloneModal(
            workspace_name="Main",
            workspace_folders=[Path("/workspace")],
            url="git@github.com:org/project.git",
            dest_text="",
            edit_field="url",
        )

        commit_edit(modal)

        self.assertEqual(modal.dest_text, "/workspace/project")
        self.assertEqual(modal.edit_field, "")

    def test_cancel_edit_restores_previous_value(self) -> None:
        modal = self._modal()
        enter_edit(modal, "url")
        modal.url = "changed"

        cancel_edit(modal)

        self.assertEqual(modal.url, "git@github.com:org/project.git")
        self.assertEqual(modal.edit_field, "")

    def test_branch_typing_rejects_leading_dash(self) -> None:
        modal = self._modal()
        modal.edit_field = "branch"

        handle_typing(modal, ord("-"))
        handle_typing(modal, ord("f"))
        handle_typing(modal, ord("/"))
        handle_typing(modal, ord("x"))

        self.assertEqual(modal.branch, "f/x")

    def test_key_handler_moves_and_toggles_recurse(self) -> None:
        state = self._state()
        state.clone_modal = self._modal()

        handle_clone_modal_key(state, curses.KEY_DOWN)
        self.assertEqual(state.clone_modal.selected, FIELD_DEST)

        state.clone_modal.selected = FIELD_RECURSE
        handle_clone_modal_key(state, ord(" "))
        self.assertFalse(state.clone_modal.recurse_submodules)

    def test_try_clone_dispatches_worker_and_callback_updates_modal(self) -> None:
        state = self._state()
        state.clone_modal = self._modal()

        with mock.patch("features.clone_modal.actions.kick_off_clone") as clone:
            try_clone(state)

        clone.assert_called_once()
        self.assertTrue(state.clone_modal.cloning)
        on_done = clone.call_args.kwargs["on_done"]

        on_done(False, "clone failed")
        self.assertFalse(state.clone_modal.cloning)
        self.assertEqual(state.clone_modal.error, "clone failed")

        on_done(True, "cloned")
        self.assertIsNone(state.clone_modal)

    def test_button_enter_runs_clone(self) -> None:
        state = self._state()
        state.clone_modal = self._modal()
        state.clone_modal.selected = FIELD_BUTTON

        with mock.patch("features.clone_modal.actions.kick_off_clone") as clone:
            handle_clone_modal_key(state, 10)

        clone.assert_called_once()


if __name__ == "__main__":
    unittest.main()
