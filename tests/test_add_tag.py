"""Add-tag worker job lifecycle tests."""
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

from _helpers import (  # noqa: E402
    assert_repo_refresh_available,
    make_repo_model as _make_repo,
    make_state as _state,
)
from core.jobs import JobStatus  # noqa: E402
from core.workers import kick_off_add_tag  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestAddTagJobLifecycle(unittest.TestCase):
    def test_successful_tag_push_finishes_job_ok_and_releases_lease(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        calls = []

        def git_side_effect(_path, args):
            calls.append(args)
            if args[0] == "for-each-ref":
                return 0, "refs/remotes/origin/main\n", ""
            return 0, "", ""

        with mock.patch("core.workers.git", side_effect=git_side_effect):
            kick_off_add_tag(
                state, "r", repo.path, repo, None, "v1", "abc123")

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: tag v1")
        self.assertEqual(task.status, "ok")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "tag")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(calls[-1], ["push", "origin", "v1"])

    def test_local_only_tag_finishes_job_warn(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        def git_side_effect(_path, args):
            if args[0] == "for-each-ref":
                return 0, "", ""
            return 0, "", ""

        with mock.patch("core.workers.git", side_effect=git_side_effect):
            kick_off_add_tag(
                state, "r", repo.path, repo, None, "v1", "abc123")

        _wait_for_job_terminal(state)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: tag v1")
        self.assertEqual(task.status, "warn")
        self.assertIn("commit not on origin", task.message)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, task.message)

    def test_git_tag_failure_finishes_job_failed(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch(
                "core.workers.git",
                return_value=(1, "", "bad tag")):
            kick_off_add_tag(
                state, "r", repo.path, repo, None, "v1", "abc123")

        _wait_for_job_terminal(state)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: tag v1")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "bad tag")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "bad tag")

    def test_thread_start_failure_releases_lease_and_fails_job(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_add_tag(
                state, "r", repo.path, repo, None, "v1", "abc123")

        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: tag v1")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
