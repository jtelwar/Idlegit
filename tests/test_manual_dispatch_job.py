"""Manual workflow dispatch job lifecycle tests."""
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

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.workers import kick_off_manual_dispatch  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _wait_for_job_count_terminal(state, index: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if len(jobs) > index and jobs[index].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestManualDispatchJob(unittest.TestCase):
    def _repo(self):
        return _make_repo(
            "r",
            remote_url_raw="git@github.com:owner/repo.git",
        )

    def test_successful_dispatch_hands_off_tracking_and_finishes_ok(self):
        repo = self._repo()
        state = _state(repo)
        run = {"databaseId": 42, "workflowName": "Deploy"}

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch(
                    "core.workers.dispatch_workflow",
                    return_value=(True, "dispatched")), \
                mock.patch(
                    "core.workers.list_recent_runs",
                    side_effect=[[], [run]]), \
                mock.patch(
                    "core.workers.kick_off_workflow_tracking") as tracking:
            kick_off_manual_dispatch(
                state, repo, "Deploy", "main", inputs={"env": "prod"})

        _wait_for_job_terminal(state)
        tracking.assert_called_once_with(
            state, "owner/repo", run, repo)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "↗ r: dispatch Deploy")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "dispatched")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "workflow-dispatch")
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_failed_dispatch_finishes_job_failed(self):
        repo = self._repo()
        state = _state(repo)

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch(
                    "core.workers.dispatch_workflow",
                    return_value=(False, "dispatch failed")):
            kick_off_manual_dispatch(state, repo, "Deploy", "main")

        _wait_for_job_terminal(state)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "dispatch failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "dispatch failed")

    def test_existing_pending_task_is_reused(self):
        repo = self._repo()
        state = _state(repo)
        task = state.tasks.add("  ↪ then run: Deploy")
        state.tasks.update(task, "pending", "waiting on Tests")

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch(
                    "core.workers.dispatch_workflow",
                    return_value=(False, "dispatch failed")):
            kick_off_manual_dispatch(
                state, repo, "Deploy", "main", existing_task=task)

        _wait_for_job_terminal(state)
        tasks = state.tasks.snapshot()
        self.assertEqual(len(tasks), 1)
        self.assertIs(tasks[0], task)
        self.assertEqual(task.label, "  ↪ dispatch Deploy")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "dispatch failed")
        job = state.job_registry.snapshot()[0]
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_existing_task_active_dispatch_job_wins_over_prior_poll_job(self):
        repo = self._repo()
        state = _state(repo)
        task = state.tasks.add("  ↪ then run: Deploy")
        state.tasks.update(task, "pending", "waiting on Tests")
        poll_job = state.job_registry.start(
            JobSpec(kind="workflow-poll", label="poll"))
        state.job_registry.link_task(poll_job, task)
        state.job_registry.finish(poll_job, JobStatus.OK)

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch(
                    "core.workers.dispatch_workflow",
                    return_value=(False, "dispatch failed")):
            kick_off_manual_dispatch(
                state, repo, "Deploy", "main", existing_task=task)

        _wait_for_job_count_terminal(state, 1)
        jobs = state.job_registry.snapshot()
        dispatch_job = jobs[1]
        self.assertIs(state.job_registry.job_for_task(task), dispatch_job)
        self.assertEqual(dispatch_job.status, JobStatus.FAIL)

    def test_thread_start_failure_marks_task_and_job_failed(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = self._repo()
        state = _state(repo)

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_manual_dispatch(state, repo, "Deploy", "main")

        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
