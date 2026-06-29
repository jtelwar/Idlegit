"""Workflow tracking job lifecycle tests."""
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
from core.jobs import JobStatus, JobTaskOutcome  # noqa: E402
from core.state.store import WorkflowIntentSnapshot  # noqa: E402
from core.workers import kick_off_workflow_tracking  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestWorkflowTrackingJob(unittest.TestCase):
    def _repo(self):
        return _make_repo("r")

    def _completed_view(self, conclusion: str) -> dict:
        return {
            "status": "completed",
            "conclusion": conclusion,
            "url": "https://example.invalid/run",
            "jobs": [],
        }

    def test_successful_run_finishes_job_ok(self):
        repo = self._repo()
        state = _state(repo)

        with mock.patch(
                "core.workers.get_run_view",
                return_value=self._completed_view("success")):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        _wait_for_job_terminal(state)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, "ok")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "workflow-poll")
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_failed_run_finishes_job_failed(self):
        repo = self._repo()
        state = _state(repo)

        with mock.patch(
                "core.workers.get_run_view",
                return_value=self._completed_view("failure")):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        _wait_for_job_terminal(state)
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "failure")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "failure")

    def test_cancelled_run_finishes_job_warn(self):
        repo = self._repo()
        state = _state(repo)

        with mock.patch(
                "core.workers.get_run_view",
                return_value=self._completed_view("cancelled")):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        _wait_for_job_terminal(state)
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "cancelled")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "cancelled")

    def test_job_uses_poll_outcome_not_task_row_status(self):
        repo = self._repo()
        state = _state(repo)

        def poll(_state, _slug, _run_id, _repo, _workflow, task,
                 _pending_task=None, _pushed_sha="", task_bridge=None):
            task_bridge.update(task, "fail", "presentation-only failure")
            return JobTaskOutcome(JobStatus.OK, "typed outcome")

        with mock.patch("core.workers._poll_run", side_effect=poll):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        _wait_for_job_terminal(state)
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "presentation-only failure")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "typed outcome")

    def test_thread_start_failure_marks_run_and_pending_tasks(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = self._repo()
        state = _state(repo)
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                then_run_after_workflow={"CI": "Deploy"}))

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        pending = next(t for t in state.tasks.snapshot()
                       if t.label == "  ↪ then run: Deploy")
        self.assertEqual(pending.status, "warn")
        self.assertEqual(pending.message,
                         "skipped — workflow polling failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")
        self.assertIs(state.job_registry.job_for_task(task), job)
        self.assertIs(state.job_registry.job_for_task(pending), job)

    def test_poll_job_links_run_pending_and_job_rows(self):
        repo = self._repo()
        state = _state(repo)
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                then_run_after_workflow={"CI": "Deploy"}))
        view = {
            "status": "completed",
            "conclusion": "success",
            "url": "https://example.invalid/run",
            "jobs": [
                {
                    "databaseId": 100,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [],
                },
            ],
        }

        with mock.patch("core.workers.get_run_view", return_value=view), \
                mock.patch("core.workers.kick_off_manual_dispatch"):
            task = kick_off_workflow_tracking(
                state, "owner/repo", {"databaseId": 1, "workflowName": "CI"},
                repo)

        _wait_for_job_terminal(state)
        job = state.job_registry.snapshot()[0]
        pending = next(t for t in state.tasks.snapshot()
                       if t.label == "  ↪ then run: Deploy")
        job_row = next(t for t in state.tasks.snapshot()
                       if "build" in t.label)

        self.assertIs(state.job_registry.job_for_task(task), job)
        self.assertIs(state.job_registry.job_for_task(pending), job)
        self.assertIs(state.job_registry.job_for_task(job_row), job)

    def test_snapshot_then_run_does_not_read_repo_fields(self):
        repo = self._repo()
        state = _state(repo)
        then_runs = {"CI": "Deploy"}
        then_run_params = {"CI": {"environment": "staging"}}

        with mock.patch(
                "core.workers.get_run_view",
                return_value=self._completed_view("success")), \
                mock.patch("core.workers.kick_off_manual_dispatch") as dispatch:
            task = kick_off_workflow_tracking(
                state,
                "owner/repo",
                {"databaseId": 1, "workflowName": "CI"},
                repo,
                then_run_after_workflow=then_runs,
                then_run_params_after_workflow=then_run_params,
            )
            _wait_for_job_terminal(state)

        self.assertIsNotNone(task)
        pending = next(t for t in state.tasks.snapshot()
                       if t.label == "  ↪ then run: Deploy")
        dispatch.assert_called_once_with(
            state,
            repo,
            "Deploy",
            repo.branch or "main",
            existing_task=pending,
            inputs={"environment": "staging"},
        )
        self.assertFalse(hasattr(repo, "then_run_after_workflow"))
        self.assertFalse(hasattr(repo, "then_run_params_after_workflow"))
        self.assertEqual(then_runs, {})
        self.assertEqual(then_run_params, {})

    def test_pending_followup_registry_supplies_live_then_run_target(self):
        repo = self._repo()
        state = _state(repo)
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                then_run_after_workflow={"CI": "Deploy"}))

        def mutate_pending_then_complete(_slug, _run_id):
            pending = next(t for t in state.tasks.snapshot()
                           if t.label == "  ↪ then run: Deploy")
            state.workflow_followups.update(pending.subject_id, target="Release")
            return self._completed_view("success")

        with mock.patch("core.workers.get_run_view",
                        side_effect=mutate_pending_then_complete), \
                mock.patch("core.workers.kick_off_manual_dispatch") as dispatch:
            task = kick_off_workflow_tracking(
                state,
                "owner/repo",
                {"databaseId": 1, "workflowName": "CI"},
                repo,
            )
            _wait_for_job_terminal(state)

        self.assertIsNotNone(task)
        pending = next(t for t in state.tasks.snapshot()
                       if t.label == "  ↪ then run: Deploy")
        dispatch.assert_called_once_with(
            state,
            repo,
            "Release",
            repo.branch or "main",
            existing_task=pending,
            inputs={},
        )
        self.assertTrue(state.store.repo_workflow_intent(repo).empty)
        self.assertFalse(hasattr(repo, "then_run_after_workflow"))


if __name__ == "__main__":
    unittest.main()
