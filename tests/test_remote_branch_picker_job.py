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
from core.state.action_menu import ActionMenu  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from features.remote_branch_picker.session import open_remote_branch_picker  # noqa: E402


class TestRemoteBranchPickerJob(unittest.TestCase):
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

    def _wait_refs(self, state):
        import time
        picker = state.remote_branch_picker
        deadline = 100
        while deadline > 0:
            refs, loading, error = state.view_loads.snapshot(picker.load_id)
            if not loading:
                return refs, error
            time.sleep(0.01)
            deadline -= 1
        raise AssertionError("remote refs did not finish loading")

    def test_open_loads_refs_through_read_only_job(self) -> None:
        state = self._state()
        with mock.patch(
            "core.workers.list_remote_tracking_refs",
            return_value=["origin/main"],
        ):
            open_remote_branch_picker(state)
            viewer = state.remote_branch_picker
            refs, error = self._wait_refs(state)

        self.assertEqual(viewer.selected, 0)
        self.assertEqual(refs, ["origin/main"])
        self.assertEqual(error, "")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "remote-branch-load")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)

    def test_thread_start_failure_clears_loading(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = self._state()
        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            open_remote_branch_picker(state)

        picker = state.remote_branch_picker
        refs, loading, error = state.view_loads.snapshot(picker.load_id)
        self.assertEqual(refs, ["thread start failed"])
        self.assertFalse(loading)
        self.assertEqual(error, "thread start failed")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)


if __name__ == "__main__":
    unittest.main()
