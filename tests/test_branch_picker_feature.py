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
from core.state.pickers import BranchPicker  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from features.branch_picker.actions import handle_branch_picker_key  # noqa: E402
from features.branch_picker.projection import (  # noqa: E402
    has_create_row,
    picker_branches,
    title_label,
)
from features.branch_picker.session import (  # noqa: E402
    close_branch_picker,
    open_branch_picker,
)


class TestBranchPickerFeature(unittest.TestCase):
    def _state(self):
        repo = _make_repo("repo")
        state = _state(repo)
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
        )
        return state

    def _picker(self, state, mode: str = "switch") -> BranchPicker:
        picker = BranchPicker(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            load_id=f"branch-picker:test:{mode}",
            mode=mode,
        )
        state.branch_picker = picker
        return picker

    def test_open_session_installs_picker_and_starts_loader(self) -> None:
        state = self._state()

        with mock.patch(
                "features.branch_picker.session.kick_off_branch_picker_load") as kickoff:
            open_branch_picker(state, mode="merge")

        self.assertIsNotNone(state.branch_picker)
        self.assertEqual(state.branch_picker.mode, "merge")
        kickoff.assert_called_once_with(state, state.branch_picker)

    def test_close_session_removes_view_load_and_picker(self) -> None:
        state = self._state()
        picker = self._picker(state)
        state.view_loads.create(picker.load_id)

        close_branch_picker(state)

        self.assertIsNone(state.branch_picker)
        self.assertIsNone(state.view_loads.get(picker.load_id))

    def test_projection_reads_view_load_snapshot(self) -> None:
        state = self._state()
        picker = self._picker(state, mode="set_upstream")
        state.view_loads.finish(
            picker.load_id,
            ["origin/main"],
            details={"current": "main"},
        )

        self.assertFalse(has_create_row(picker))
        self.assertEqual(title_label(picker), "Set upstream")
        self.assertEqual(
            picker_branches(state, picker),
            (["origin/main"], "main", False),
        )

    def test_create_row_enter_dispatches_create_branch(self) -> None:
        state = self._state()
        picker = self._picker(state)
        picker.selected = -1
        picker.create_typed = "feature/test"
        state.view_loads.finish(
            picker.load_id,
            ["main"],
            details={"current": "main"},
        )

        with mock.patch("features.branch_picker.actions.kick_off_action") as action:
            handle_branch_picker_key(state, 10)

        action.assert_called_once()
        self.assertEqual(action.call_args.args[1], "create_branch")
        self.assertEqual(action.call_args.kwargs["branch_arg"], "feature/test")
        self.assertIsNone(state.branch_picker)
        self.assertIsNone(state.action_menu)

    def test_switch_branch_enter_dispatches_switch_branch(self) -> None:
        state = self._state()
        picker = self._picker(state)
        picker.selected = 1
        state.view_loads.finish(
            picker.load_id,
            ["main", "feature/test"],
            details={"current": "main"},
        )

        with mock.patch("features.branch_picker.actions.kick_off_action") as action:
            handle_branch_picker_key(state, 10)

        action.assert_called_once()
        self.assertEqual(action.call_args.args[1], "switch_branch")
        self.assertEqual(action.call_args.kwargs["branch_arg"], "feature/test")
        self.assertIsNone(state.branch_picker)
        self.assertIsNone(state.action_menu)

    def test_merge_non_fast_forward_dispatches_safe_merge(self) -> None:
        state = self._state()
        picker = self._picker(state, mode="merge")
        state.view_loads.finish(
            picker.load_id,
            ["main", "feature/test"],
            details={"current": "main"},
        )
        picker.selected = 1

        with (
            mock.patch(
                "features.branch_picker.actions.is_fast_forward_merge",
                return_value=False,
            ),
            mock.patch(
                "features.branch_picker.actions.kick_off_safe_merge") as safe_merge,
        ):
            handle_branch_picker_key(state, 10)

        safe_merge.assert_called_once()
        self.assertEqual(safe_merge.call_args.kwargs["merge_ref"], "feature/test")
        self.assertIsNone(state.branch_picker)
        self.assertIsNone(state.action_menu)

    def test_create_row_typing_rejects_leading_dash(self) -> None:
        state = self._state()
        picker = self._picker(state)
        picker.selected = -1
        state.view_loads.finish(picker.load_id, ["main"], details={"current": "main"})

        handle_branch_picker_key(state, ord("-"))
        handle_branch_picker_key(state, ord("f"))
        handle_branch_picker_key(state, curses.KEY_BACKSPACE)

        self.assertEqual(picker.create_typed, "")


if __name__ == "__main__":
    unittest.main()
