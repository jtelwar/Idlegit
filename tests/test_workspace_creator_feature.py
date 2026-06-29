from __future__ import annotations

import curses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.workspaces import WorkspaceCreator, WorkspaceDraft  # noqa: E402
from features.workspace_creator.actions import (  # noqa: E402
    commit_workspace_creator,
    drafts_to_workspaces,
    handle_workspace_creator_key,
)
from features.workspace_creator.session import (  # noqa: E402
    close_workspace_creator,
    open_workspace_creator,
    tick_creator_checks,
)


class TestWorkspaceCreatorFeature(unittest.TestCase):
    def _state(self):
        state = _state(_make_repo("repo"))
        open_workspace_creator(state)
        return state

    def test_open_session_installs_default_modal(self) -> None:
        state = _state(_make_repo("repo"))

        open_workspace_creator(state, title="Add", intro="Intro")

        self.assertIsNotNone(state.workspace_creator)
        self.assertEqual(state.workspace_creator.title, "Add")
        self.assertEqual(state.workspace_creator.intro, "Intro")
        self.assertEqual(len(state.workspace_creator.drafts), 1)

    def test_close_session_clears_modal(self) -> None:
        state = self._state()

        close_workspace_creator(state)

        self.assertIsNone(state.workspace_creator)

    def test_drafts_to_workspaces_skips_empty_and_deduplicates_names(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            one = Path(root) / "same"
            two = Path(root) / "other" / "same"
            one.mkdir()
            two.mkdir(parents=True)

            result = drafts_to_workspaces([
                WorkspaceDraft(path_text=str(one)),
                WorkspaceDraft(path_text=""),
                WorkspaceDraft(path_text=str(two)),
            ])

        self.assertEqual([workspace.name for workspace in result],
                         ["same", "same (2)"])

    def test_typing_updates_path_and_invalidates_check(self) -> None:
        state = self._state()
        draft = state.workspace_creator.drafts[0]
        draft.last_checked = "/old"

        for char in "/tmp":
            handle_workspace_creator_key(state, ord(char))

        self.assertEqual(draft.path_text, "/tmp")
        self.assertEqual(draft.last_checked, "")
        self.assertEqual(state.workspace_creator.field_cursor, 4)

    def test_enter_advances_and_done_commits(self) -> None:
        state = self._state()
        for char in "/tmp":
            handle_workspace_creator_key(state, ord(char))

        handle_workspace_creator_key(state, 10)
        self.assertEqual(len(state.workspace_creator.drafts), 2)
        handle_workspace_creator_key(state, curses.KEY_DOWN)
        handle_workspace_creator_key(state, 10)

        self.assertIsNotNone(state.workspace_creator.result)
        self.assertEqual(len(state.workspace_creator.result), 1)

    def test_escape_cancels_and_closes(self) -> None:
        state = self._state()

        handle_workspace_creator_key(state, 27)

        self.assertIsNone(state.workspace_creator)

    def test_commit_sets_result(self) -> None:
        state = self._state()
        state.workspace_creator.drafts[0].path_text = "/tmp"

        commit_workspace_creator(state)

        self.assertEqual(len(state.workspace_creator.result), 1)
        self.assertEqual(
            state.workspace_creator.result[0].folders[0],
            Path("/tmp").resolve(),
        )

    def test_tick_dispatches_path_check_from_feature_boundary(self) -> None:
        state = _state(_make_repo("repo"))
        draft = WorkspaceDraft(path_text="/tmp")
        state.workspace_creator = WorkspaceCreator(drafts=[draft])

        with mock.patch(
                "features.workspace_creator.session.kick_off_workspace_path_check"
        ) as kickoff:
            checking = tick_creator_checks(state)

        kickoff.assert_called_once_with(
            state,
            draft,
            kind="workspace-path-check",
        )
        self.assertFalse(checking)


if __name__ == "__main__":
    unittest.main()
