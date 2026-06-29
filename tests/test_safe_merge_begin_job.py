"""Safe-merge begin phase job lifecycle tests."""
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

from core.jobs import JobStatus, JobTaskOutcome  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import Repo  # noqa: E402
from core import workers  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestSafeMergeBeginJob(unittest.TestCase):
    def _repo_state(self):
        repo = Repo("repo", Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="test")
        return repo, state

    def test_begin_phase_finishes_job_ok_and_keeps_flow_lock(self) -> None:
        repo, state = self._repo_state()

        def begin(_state, screen):
            screen.phase = "resolve"
            return JobTaskOutcome()

        with mock.patch.object(workers, "_safe_merge_begin_worker",
                               side_effect=begin):
            opened = workers.kick_off_safe_merge(
                state,
                target_label="repo",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                merge_ref="origin/main",
            )
            self.assertTrue(opened)
            _wait_for_job_terminal(state)

        self.assertTrue(state.store.repo_busy(repo))
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "safe-merge-begin")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        workers._safe_merge_release_locks(state.safe_merge)
        self.assertFalse(state.store.repo_busy(repo))

    def test_begin_phase_error_finishes_job_warn(self) -> None:
        repo, state = self._repo_state()

        def begin(_state, screen):
            screen.error = "already up to date — nothing to merge"
            screen.phase = "error"
            return JobTaskOutcome(JobStatus.WARN, screen.error)

        with mock.patch.object(workers, "_safe_merge_begin_worker",
                               side_effect=begin):
            opened = workers.kick_off_safe_merge(
                state,
                target_label="repo",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                merge_ref="origin/main",
            )
            self.assertTrue(opened)
            _wait_for_job_terminal(state)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "already up to date — nothing to merge")
        workers._safe_merge_release_locks(state.safe_merge)

    def test_thread_start_failure_fails_job_and_releases_lock(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo, state = self._repo_state()

        with mock.patch.object(workers.threading, "Thread", FailingThread):
            opened = workers.kick_off_safe_merge(
                state,
                target_label="repo",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                merge_ref="origin/main",
            )

        self.assertFalse(opened)
        self.assertIsNone(state.safe_merge)
        self.assertFalse(state.store.repo_busy(repo))
        header = state.tasks.snapshot()[0]
        self.assertEqual(header.status, "fail")
        self.assertEqual(header.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")

    def test_begin_phase_uses_worker_outcome_not_header_status(self) -> None:
        repo, state = self._repo_state()

        def begin(_state, screen):
            screen.phase = "resolve"
            state.tasks.update(screen.header_task, "fail", "stale display failure")
            return JobTaskOutcome(JobStatus.OK, "typed outcome")

        with mock.patch.object(workers, "_safe_merge_begin_worker",
                               side_effect=begin):
            opened = workers.kick_off_safe_merge(
                state,
                target_label="repo",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                merge_ref="origin/main",
            )
            self.assertTrue(opened)
            _wait_for_job_terminal(state)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "typed outcome")
        workers._safe_merge_release_locks(state.safe_merge)


if __name__ == "__main__":
    unittest.main()
