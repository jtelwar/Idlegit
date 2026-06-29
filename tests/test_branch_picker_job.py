from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from features.branch_picker.session import open_branch_picker  # noqa: E402


class TestBranchPickerJob(unittest.TestCase):
    class ParkedThread:
        def __init__(self, target, name) -> None:
            self.target = target
            self.name = name
            self.daemon = False

        def start(self) -> None:
            return None

    class InlineThread:
        def __init__(self, target, name) -> None:
            self.target = target
            self.name = name
            self.daemon = False

        def start(self) -> None:
            self.target()

    def _state(self):
        repo = _make_repo("repo")
        state = _state(repo)
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            target_parent=None,
        )
        return state

    def test_open_installs_loading_picker_before_branch_query_runs(self) -> None:
        state = self._state()
        with mock.patch("core.runtime.threads.threading.Thread", self.ParkedThread), \
                mock.patch("core.workers.list_branches") as list_branches:
            open_branch_picker(state)

        picker = state.branch_picker
        self.assertIsNotNone(picker)
        branches, loading, error = state.view_loads.snapshot(picker.load_id)
        self.assertTrue(loading)
        self.assertEqual(branches, [])
        self.assertEqual(error, "")
        list_branches.assert_not_called()
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "branch-picker-load")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertFalse(jobs[0].terminal)

    def test_loader_populates_local_branches_and_selection(self) -> None:
        state = self._state()
        with mock.patch("core.runtime.threads.threading.Thread", self.InlineThread), \
                mock.patch(
                    "core.workers.list_branches",
                    return_value=(["main", "feature"], "main"),
                ):
            open_branch_picker(state)

        picker = state.branch_picker
        self.assertIsNotNone(picker)
        branches, loading, error = state.view_loads.snapshot(picker.load_id)
        details = state.view_loads.details(picker.load_id)
        self.assertFalse(loading)
        self.assertEqual(error, "")
        self.assertEqual(branches, ["main", "feature"])
        self.assertEqual(details["current"], "main")
        self.assertEqual(picker.selected, 0)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)

    def test_loader_populates_upstream_refs_and_current_branch(self) -> None:
        state = self._state()
        with mock.patch("core.runtime.threads.threading.Thread", self.InlineThread), \
                mock.patch(
                    "core.workers.list_remote_tracking_refs",
                    return_value=["origin/main", "origin/dev"],
                ), \
                mock.patch("core.workers.git",
                           return_value=(0, "dev\n", "")):
            open_branch_picker(state, mode="set_upstream")

        picker = state.branch_picker
        self.assertIsNotNone(picker)
        branches, loading, error = state.view_loads.snapshot(picker.load_id)
        details = state.view_loads.details(picker.load_id)
        self.assertFalse(loading)
        self.assertEqual(error, "")
        self.assertEqual(branches, ["origin/main", "origin/dev"])
        self.assertEqual(details["current"], "dev")
        self.assertEqual(picker.selected, 1)


if __name__ == "__main__":
    unittest.main()
