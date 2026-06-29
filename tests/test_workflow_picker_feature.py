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
from core.state.action_menu import ActionMenu  # noqa: E402
from core.state.pickers import WorkflowPicker  # noqa: E402
from core.state.repos import WorkflowInfo  # noqa: E402
from features.workflow_picker.actions import (  # noqa: E402
    handle_workflow_picker_key,
)
from features.workflow_picker.projection import (  # noqa: E402
    first_runnable_workflow_index,
    selected_workflow,
    workflow_picker_hint_specs,
    workflow_row_status,
)
from features.workflow_picker.session import (  # noqa: E402
    close_workflow_picker,
    open_workflow_picker,
)


class TestWorkflowPickerFeature(unittest.TestCase):
    def _workflow(
            self,
            name: str = "ci",
            state: str = "active",
            dispatchable: bool = True,
    ) -> WorkflowInfo:
        return WorkflowInfo(
            name=name,
            path=f".github/workflows/{name}.yml",
            state=state,
            dispatchable=dispatchable,
        )

    def _state(self):
        repo = _make_repo("repo")
        repo.branch = "main"
        repo.workflows = [
            self._workflow("lint", dispatchable=False),
            self._workflow("deploy"),
        ]
        state = _state(repo)
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            branch="feature/test",
        )
        return state

    def _picker(self, *workflows: WorkflowInfo) -> WorkflowPicker:
        return WorkflowPicker(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            workflows=list(workflows),
            branch="main",
            selected=0,
        )

    def test_open_session_copies_action_menu_target_and_first_runnable(self) -> None:
        state = self._state()

        open_workflow_picker(state)

        self.assertIsNotNone(state.workflow_picker)
        self.assertEqual(state.workflow_picker.target_label, "repo")
        self.assertEqual(state.workflow_picker.branch, "feature/test")
        self.assertEqual(state.workflow_picker.selected, 1)

    def test_close_session_clears_picker_only(self) -> None:
        state = self._state()
        state.workflow_picker = self._picker(self._workflow())

        close_workflow_picker(state)

        self.assertIsNone(state.workflow_picker)
        self.assertIsNotNone(state.action_menu)

    def test_projection_marks_runnable_and_unavailable_rows(self) -> None:
        runnable, reason = workflow_row_status(self._workflow())
        self.assertTrue(runnable)
        self.assertEqual(reason, "")

        disabled, reason = workflow_row_status(
            self._workflow(state="disabled_manually"))
        self.assertFalse(disabled)
        self.assertEqual(reason, "(disabled manually)")

        no_dispatch, reason = workflow_row_status(
            self._workflow(dispatchable=False))
        self.assertFalse(no_dispatch)
        self.assertEqual(reason, "(no workflow_dispatch trigger)")

    def test_projection_selects_first_runnable_row(self) -> None:
        workflows = [
            self._workflow("disabled", state="disabled_manually"),
            self._workflow("no-dispatch", dispatchable=False),
            self._workflow("deploy"),
        ]

        self.assertEqual(first_runnable_workflow_index(workflows), 2)
        self.assertEqual(first_runnable_workflow_index(workflows[:2]), 0)

    def test_projection_hints_describe_enter_result(self) -> None:
        runnable_picker = self._picker(self._workflow())
        unavailable_picker = self._picker(self._workflow(dispatchable=False))

        self.assertIn(
            ("Enter", "run on main"),
            workflow_picker_hint_specs(runnable_picker),
        )
        actions = [
            action for _keys, action
            in workflow_picker_hint_specs(unavailable_picker)
        ]
        self.assertTrue(any(action.startswith("unavailable") for action in actions))

    def test_selected_workflow_handles_empty_picker(self) -> None:
        self.assertIsNone(selected_workflow(self._picker()))

    def test_key_handler_moves_selection(self) -> None:
        state = self._state()
        open_workflow_picker(state)

        handle_workflow_picker_key(state, curses.KEY_UP)
        self.assertEqual(state.workflow_picker.selected, 0)

        handle_workflow_picker_key(state, curses.KEY_DOWN)
        self.assertEqual(state.workflow_picker.selected, 1)

    def test_enter_ignores_unavailable_workflow(self) -> None:
        state = self._state()
        open_workflow_picker(state)
        state.workflow_picker.selected = 0

        with mock.patch(
                "features.workflow_picker.actions.kick_off_manual_dispatch"
        ) as dispatch:
            handle_workflow_picker_key(state, 10)

        dispatch.assert_not_called()
        self.assertIsNotNone(state.workflow_picker)
        self.assertIsNotNone(state.action_menu)

    def test_enter_dispatches_runnable_workflow_and_closes_modals(self) -> None:
        state = self._state()
        open_workflow_picker(state)

        with mock.patch(
                "features.workflow_picker.actions.kick_off_manual_dispatch"
        ) as dispatch:
            handle_workflow_picker_key(state, 10)

        dispatch.assert_called_once()
        self.assertIs(dispatch.call_args.args[1], state.repos[0])
        self.assertEqual(dispatch.call_args.args[2], "deploy")
        self.assertEqual(dispatch.call_args.args[3], "feature/test")
        self.assertIsNone(state.workflow_picker)
        self.assertIsNone(state.action_menu)

    def test_escape_closes_picker_only(self) -> None:
        state = self._state()
        open_workflow_picker(state)

        handle_workflow_picker_key(state, 27)

        self.assertIsNone(state.workflow_picker)
        self.assertIsNotNone(state.action_menu)


if __name__ == "__main__":
    unittest.main()
