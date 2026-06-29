"""Commit-view modal loader job lifecycle tests."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_state as _state  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.views import CommitViewModal  # noqa: E402
from core.workers import kick_off_load_commit_view  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestCommitViewJob(unittest.TestCase):
    class ParkedThread:
        target = None

        def __init__(self, target, name) -> None:
            self.target = target
            self.name = name
            self.daemon = False
            TestCommitViewJob.ParkedThread.target = target

        def start(self) -> None:
            return None

    def _modal(self) -> CommitViewModal:
        return CommitViewModal(
            target_label="r",
            target_path=Path("/tmp/r"),
            sha="abc123",
            subject="existing subject",
        )

    def _load_ids(self, modal: CommitViewModal) -> list[str]:
        return [
            modal.tags_load_id,
            modal.details_load_id,
            modal.files_load_id,
            modal.reflog_load_id,
        ]

    def _assert_loads_not_running(self, state, modal: CommitViewModal) -> None:
        for load_id in self._load_ids(modal):
            _lines, loading, _error = state.view_loads.snapshot(load_id)
            self.assertFalse(loading, load_id)

    def test_commit_view_load_finishes_read_only_job(self) -> None:
        state = _state()
        modal = self._modal()

        with mock.patch(
                "core.git_ops.list_tags_at",
                return_value=["v1"]), \
                mock.patch(
                    "core.git_ops.get_commit_details",
                    return_value=("Ada", "today", "subject", "body")), \
                mock.patch(
                    "core.git_ops.query_commit_files",
                    return_value=[]), \
                mock.patch(
                    "core.git_ops.query_commit_reflog",
                    return_value=["checkout abc123"]):
            kick_off_load_commit_view(state, modal)

        _wait_for_job_terminal(state)
        self.assertEqual(modal.tags, ["v1"])
        self.assertEqual(modal.author, "Ada")
        self.assertEqual(modal.body, "body")
        self.assertEqual(modal.reflog_entries, ["checkout abc123"])
        self._assert_loads_not_running(state, modal)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "commit-view-load")
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(modal.target_path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertFalse(state.job_registry.has_active_local_mutation())

    def test_commit_view_cancel_prevents_late_loader_publish(self) -> None:
        state = _state()
        modal = self._modal()

        with mock.patch("core.runtime.threads.threading.Thread", self.ParkedThread), \
                mock.patch("core.git_ops.list_tags_at") as list_tags:
            kick_off_load_commit_view(state, modal)

        state.view_loads.remove_many(self._load_ids(modal))
        assert self.ParkedThread.target is not None
        self.ParkedThread.target()

        list_tags.assert_not_called()
        for load_id in self._load_ids(modal):
            self.assertTrue(state.view_loads.is_cancelled(load_id))
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)

    def test_commit_view_failure_clears_flags_and_fails_job(self) -> None:
        state = _state()
        modal = self._modal()

        with mock.patch(
                "core.git_ops.list_tags_at",
                side_effect=RuntimeError("tag lookup failed")):
            kick_off_load_commit_view(state, modal)

        _wait_for_job_terminal(state)
        self._assert_loads_not_running(state, modal)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "tag lookup failed")

    def test_thread_start_failure_clears_loading_flags(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        modal = self._modal()

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_load_commit_view(state, modal)

        self._assert_loads_not_running(state, modal)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
