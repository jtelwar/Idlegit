"""Post-push workflow discovery job lifecycle tests."""
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
from core.jobs import JobStatus  # noqa: E402
from core.workers import kick_off_post_push_run_tracking  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestPostPushDiscoveryJob(unittest.TestCase):
    def _repo(self):
        return _make_repo(
            "r",
            remote_url_raw="git@github.com:owner/repo.git",
        )

    def test_discovered_runs_finish_job_ok(self):
        repo = self._repo()
        state = _state(repo)
        run = {"databaseId": 7, "workflowName": "CI"}

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch("core.workers.list_recent_runs",
                           return_value=[run]), \
                mock.patch(
                    "core.workers.kick_off_workflow_tracking") as tracking:
            kick_off_post_push_run_tracking(
                state, repo, "main", "abc123", ["CI"])

        _wait_for_job_terminal(state)
        tracking.assert_called_once_with(
            state, "owner/repo", run, repo, pushed_sha="abc123")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "workflow-poll-discovery")
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertFalse(state.job_registry.has_active_local_mutation())

    def test_discovered_runs_forward_then_run_snapshot(self):
        repo = self._repo()
        state = _state(repo)
        run = {"databaseId": 7, "workflowName": "CI"}
        then_runs = {"CI": "Deploy"}
        then_run_params = {"CI": {"environment": "staging"}}

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch("core.workers.list_recent_runs",
                           return_value=[run]), \
                mock.patch(
                    "core.workers.kick_off_workflow_tracking") as tracking:
            kick_off_post_push_run_tracking(
                state,
                repo,
                "main",
                "abc123",
                ["CI"],
                then_run_after_workflow=then_runs,
                then_run_params_after_workflow=then_run_params,
            )

        _wait_for_job_terminal(state)
        tracking.assert_called_once_with(
            state,
            "owner/repo",
            run,
            repo,
            pushed_sha="abc123",
            then_run_after_workflow=then_runs,
            then_run_params_after_workflow=then_run_params,
        )

    def test_missing_runs_finish_job_warn_and_emit_task(self):
        class ImmediateThread:
            daemon = False

            def __init__(self, target, *args, **kwargs):
                self._target = target

            def start(self):
                self._target()

        repo = self._repo()
        state = _state(repo)

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch("core.workers.list_recent_runs",
                           return_value=[]), \
                mock.patch("core.runtime.threads.threading.Thread",
                           ImmediateThread), \
                mock.patch(
                    "core.workers.POST_PUSH_RUN_DISCOVERY_TIMEOUT_SECONDS",
                    0.0):
            kick_off_post_push_run_tracking(
                state, repo, "main", "abc123", ["CI"])
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "↗ r: CI")
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "no run triggered within 2 min")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "some workflow runs did not appear")

    def test_thread_start_failure_finishes_job_failed(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = self._repo()
        state = _state(repo)

        with mock.patch("core.workers.gh_available", return_value=True), \
                mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_post_push_run_tracking(
                state, repo, "main", "abc123", ["CI"])

        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        self.assertEqual(jobs[0].message, "thread start failed")
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "↗ r: discover workflow runs")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
