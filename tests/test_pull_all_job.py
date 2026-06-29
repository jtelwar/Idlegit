"""Pull-all job lifecycle tests."""
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
    held_repo_refresh,
    make_repo_model as _make_repo,
    make_state as _state,
)
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.workers import kick_off_pull_all  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _wait_for_job_kind_terminal(state, kind: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [job for job in state.job_registry.snapshot() if job.spec.kind == kind]
        if jobs and jobs[-1].terminal:
            return
        time.sleep(0.01)
    raise AssertionError(f"{kind} job did not finish")


class TestPullAllJob(unittest.TestCase):
    def test_successful_pull_all_finishes_job_ok_and_releases_lock(self):
        repo = _make_repo("pull-ok")
        repo.upstream = "origin/main"
        state = _state(repo)

        git_results = [
            (0, "origin/main\n", ""),
            (0, "before\n", ""),
            (0, "after\n", ""),
        ]

        with mock.patch("core.workers.git",
                        side_effect=lambda *_args: git_results.pop(0)), \
                mock.patch("core.workers._pull_prefer_ff_then_merge",
                           return_value=True), \
                mock.patch(
                    "core.workers._refresh_repo_snapshot_into_state") as refresh, \
                mock.patch("core.workers.link_siblings"):
            kick_off_pull_all(state)
            _wait_for_job_terminal(state)
        refresh.assert_called_once_with(state, repo)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "pull all")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "1 pulled")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "pull-all")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)

    def test_pull_all_warning_finishes_job_warn(self):
        repo = _make_repo("pull-warn")
        state = _state(repo)

        with held_repo_refresh(state, repo):
            with mock.patch("core.workers.git") as git_mock, \
                    mock.patch("core.workers._refresh_repo_snapshot_into_state"), \
                    mock.patch("core.workers.link_siblings"):
                kick_off_pull_all(state)
                _wait_for_job_terminal(state)
            git_mock.assert_not_called()
            task = state.tasks.snapshot()[0]
            self.assertEqual(task.status, "warn")
            self.assertEqual(task.message, "1 locked")
            job = state.job_registry.snapshot()[0]
            self.assertEqual(job.status, JobStatus.WARN)
            self.assertEqual(job.message, "1 locked")
            self.assertFalse(job.spec.local_mutation)

    def test_pull_all_skips_registry_owned_repo_before_locking(self):
        repo = _make_repo("pull-busy")
        state = _state(repo)
        state.job_registry.start(JobSpec(
            kind="commit",
            label="commit",
            local_mutation=True,
            repo_keys=(str(repo.path),),
        ))

        with mock.patch("core.workers.git") as git_mock:
            kick_off_pull_all(state)
            _wait_for_job_kind_terminal(state, "pull-all")

        git_mock.assert_not_called()
        assert_repo_refresh_available(self, state, repo)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "pull all")
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "1 busy")

    def test_thread_start_failure_fails_job_and_releases_lock(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("pull-fail")
        repo.upstream = "origin/main"
        state = _state(repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_pull_all(state)

        self.assertFalse(state.store.repo_busy(repo))
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
