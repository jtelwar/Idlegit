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
from core.state.pickers import RemoteBranchPicker  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from features.remote_branch_picker.actions import (  # noqa: E402
    handle_remote_branch_picker_key,
)
from features.remote_branch_picker.projection import (  # noqa: E402
    picker_refs,
    title_label,
    tracking_label,
)
from features.remote_branch_picker.session import (  # noqa: E402
    close_remote_branch_picker,
    open_remote_branch_picker,
)


class TestRemoteBranchPickerFeature(unittest.TestCase):
    def _state(self):
        repo = _make_repo("repo")
        state = _state(repo)
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
        )
        return state

    def _picker(self, state) -> RemoteBranchPicker:
        picker = RemoteBranchPicker(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            load_id="remote-branch-picker:test",
        )
        state.remote_branch_picker = picker
        return picker

    def test_open_session_installs_picker_and_starts_loader(self) -> None:
        state = self._state()

        with mock.patch(
                "features.remote_branch_picker.session.kick_off_remote_branch_picker_load") as kickoff:
            open_remote_branch_picker(state)

        self.assertIsNotNone(state.remote_branch_picker)
        kickoff.assert_called_once_with(state, state.remote_branch_picker)

    def test_close_session_removes_view_load_and_picker(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.create(picker.load_id)

        close_remote_branch_picker(state)

        self.assertIsNone(state.remote_branch_picker)
        self.assertIsNone(state.view_loads.get(picker.load_id))

    def test_projection_reads_view_load_snapshot(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.finish(picker.load_id, ["origin/main"])

        self.assertEqual(title_label(picker), "Checkout remote branch")
        self.assertEqual(tracking_label("origin/main"),
                         "checkout main (track origin/main)")
        self.assertEqual(picker_refs(state, picker), (["origin/main"], False))

    def test_enter_dispatches_checkout_remote_branch(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.finish(picker.load_id, ["origin/main"])

        with mock.patch(
                "features.remote_branch_picker.actions.kick_off_action") as action:
            handle_remote_branch_picker_key(state, 10)

        action.assert_called_once()
        self.assertEqual(action.call_args.args[1], "checkout_remote_branch")
        self.assertEqual(action.call_args.kwargs["branch_arg"], "origin/main")
        self.assertIsNone(state.remote_branch_picker)
        self.assertIsNone(state.action_menu)
        self.assertIsNone(state.view_loads.get(picker.load_id))

    def test_enter_ignores_unsafe_or_unqualified_ref(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.finish(picker.load_id, ["--bad", "main"])

        with mock.patch(
                "features.remote_branch_picker.actions.kick_off_action") as action:
            handle_remote_branch_picker_key(state, 10)
            picker.selected = 1
            handle_remote_branch_picker_key(state, 10)

        action.assert_not_called()
        self.assertIs(state.remote_branch_picker, picker)

    def test_navigation_clamps_to_bounds(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.finish(picker.load_id, ["origin/a", "origin/b"])

        handle_remote_branch_picker_key(state, curses.KEY_DOWN)
        handle_remote_branch_picker_key(state, curses.KEY_DOWN)
        self.assertEqual(picker.selected, 1)
        handle_remote_branch_picker_key(state, curses.KEY_UP)
        handle_remote_branch_picker_key(state, curses.KEY_UP)
        self.assertEqual(picker.selected, 0)


if __name__ == "__main__":
    unittest.main()
