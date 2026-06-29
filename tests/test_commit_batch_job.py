"""Commit batch launcher job lifecycle tests."""
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
from core.state.repos import ChildRef  # noqa: E402
from core.state.store import WorkflowIntentSnapshot  # noqa: E402
from core.workers import kick_off_workers  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _wait_for_mock_call(mock_obj, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mock_obj.call_count:
            return
        time.sleep(0.01)
    raise AssertionError("mock was not called")


class TestCommitBatchJob(unittest.TestCase):
    def test_successful_batch_finishes_job_ok_and_releases_claim(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        with mock.patch("core.workers.commit_worker"), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "commit-batch")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        reconcile.assert_called_once_with(
            [repo],
            [],
            refresh_fn=mock.ANY,
            link_fn=mock.ANY,
            max_workers=None,
        )
        with mock.patch("core.workers._refresh_repo_snapshot_into_state") as refresh:
            reconcile.call_args.kwargs["refresh_fn"](repo)
        refresh.assert_called_once_with(state, repo)

    def test_batch_collects_commit_message_from_store_snapshot(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit from store")
        repo.message = ""

        with mock.patch("core.workers.commit_worker") as worker, \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[2], "commit from store")
        self.assertEqual(state.store.row_message(repo), "")

    def test_batch_collects_active_workspace_rows_from_store(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit from store")
        state.repos = []

        with mock.patch("core.workers.commit_worker") as worker, \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[1], repo)
        self.assertEqual(worker.call_args.args[2], "commit from store")

    def test_batch_collects_workflow_intent_from_store_snapshot(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit from store")
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                track_workflow={"CI": True},
                then_run_after_push="Deploy",
                then_run_params_after_push={"env": "prod"},
                then_run_after_workflow={"CI": "Release"},
                then_run_params_after_workflow={"CI": {"tag": "v1"}},
            ),
        )
        with mock.patch("core.workers.commit_worker") as worker, \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[6], {"CI": True})
        self.assertEqual(worker.call_args.args[7], "Deploy")
        self.assertEqual(worker.call_args.args[8], {"env": "prod"})
        self.assertEqual(worker.call_args.args[9], {"CI": "Release"})
        self.assertEqual(worker.call_args.args[10], {"CI": {"tag": "v1"}})
        self.assertTrue(state.store.repo_workflow_intent(repo).empty)
        self.assertFalse(hasattr(repo, "track_workflow"))
        self.assertFalse(hasattr(repo, "then_run_after_push"))

    def test_existing_failed_task_does_not_fail_successful_batch(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")
        stale_task = state.tasks.add("old failed task")
        state.tasks.update(stale_task, "fail", "old failure")

        with mock.patch("core.workers.commit_worker"), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "")

    def test_commit_workers_receive_owning_job_cancel_identity(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")
        seen = {}

        def worker(*args):
            job = state.job_registry.snapshot()[0]
            seen["cancel_event"] = args[13]
            seen["cancel_job_id"] = args[14]
            self.assertIs(args[13], job.cancel_event)
            self.assertEqual(args[14], job.job_id)
            self.assertIs(args[15], False)

        with mock.patch("core.workers.commit_worker",
                        side_effect=worker), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        self.assertIs(seen["cancel_event"], state.job_registry.snapshot()[0].cancel_event)
        self.assertEqual(seen["cancel_job_id"], state.job_registry.snapshot()[0].job_id)

    def test_batch_hands_owned_repo_mutation_lease_to_worker(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        def worker(*args):
            self.assertIs(args[1], repo)
            self.assertIs(args[15], False)
            bridge = args[12]
            task = bridge.add("r: skipped")
            bridge.update(task, "warn", "nothing staged")

        with mock.patch("core.workers.commit_worker",
                        side_effect=worker), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)

    def test_batch_hands_owned_child_mutation_lease_to_worker(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
        )
        parent.children = [child]
        state = _state(parent, canonical)
        state.store.set_row_message(child, "commit child")

        def worker(*args):
            self.assertIs(args[1], parent)
            self.assertIs(args[2], child)
            self.assertIs(args[15], False)
            bridge = args[12]
            task = bridge.add("child: skipped")
            bridge.update(task, "warn", "nothing staged")

        with mock.patch("core.workers.commit_worker_for_child",
                        side_effect=worker), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.child_busy(child))

    def test_worker_warn_finishes_batch_job_warn(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        def warn_worker(*args):
            bridge = args[12]
            task = bridge.add("r: skipped")
            bridge.update(task, "warn", "nothing staged")

        with mock.patch("core.workers.commit_worker",
                        side_effect=warn_worker), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])
            _wait_for_mock_call(reconcile)

        _wait_for_job_terminal(state)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "nothing staged")
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: skipped")
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_worker_exception_fails_batch_job_and_releases_claim(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        with mock.patch("core.workers.commit_worker",
                        side_effect=RuntimeError("worker exploded")), \
                mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            kick_off_workers(state, [])

        _wait_for_job_terminal(state)
        reconcile.assert_not_called()
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        self.assertFalse(state.tasks.has_running())
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "worker exploded")
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "commit workers")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "worker exploded")
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_worker_start_failure_fails_batch_job(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_workers(state, [])

        self.assertFalse(state.store.repo_busy(repo))
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "commit workers")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_later_worker_start_failure_releases_all_claims(self) -> None:
        class FirstThreadRunsSecondFails:
            created = 0
            daemon = False

            def __init__(self, target, args=(), **kwargs):
                type(self).created += 1
                self._target = target
                self._args = args
                self._should_fail = type(self).created == 2

            def start(self):
                if self._should_fail:
                    raise RuntimeError("second worker start failed")
                self._target(*self._args)

            def join(self):
                return

        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        state = _state(repo_a, repo_b)
        state.store.set_row_message(repo_a, "commit a")
        state.store.set_row_message(repo_b, "commit b")

        with mock.patch("core.runtime.threads.threading.Thread",
                        FirstThreadRunsSecondFails), \
                mock.patch("core.workers.commit_worker"), \
                mock.patch("core.workers._refresh_repo_snapshot_into_state"), \
                mock.patch("core.workers.link_siblings"):
            kick_off_workers(state, [])

        self.assertFalse(state.store.repo_busy(repo_a))
        self.assertFalse(state.store.repo_busy(repo_b))
        assert_repo_refresh_available(self, state, repo_a)
        assert_repo_refresh_available(self, state, repo_b)
        self.assertFalse(state.job_registry.has_active_local_mutation())
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "commit workers")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "second worker start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "second worker start failed")
        self.assertIs(state.job_registry.job_for_task(task), job)

    def test_supervisor_start_failure_fails_batch_job_and_cleans_up(self) -> None:
        class FirstThreadRunsSecondFails:
            created = 0
            daemon = False

            def __init__(self, target, args=(), **kwargs):
                type(self).created += 1
                self._target = target
                self._args = args
                self._should_fail = type(self).created == 2

            def start(self):
                if self._should_fail:
                    raise RuntimeError("supervisor start failed")
                self._target(*self._args)

            def join(self):
                return

        repo = _make_repo("r")
        state = _state(repo)
        state.store.set_row_message(repo, "commit this")

        with mock.patch("core.runtime.threads.threading.Thread",
                        FirstThreadRunsSecondFails), \
                mock.patch("core.workers.commit_worker"), \
                mock.patch("core.workers._refresh_repo_snapshot_into_state"), \
                mock.patch("core.workers.link_siblings"):
            kick_off_workers(state, [])

        self.assertFalse(state.store.repo_busy(repo))
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "commit supervisor")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "supervisor start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "supervisor start failed")
        self.assertIs(state.job_registry.job_for_task(task), job)


if __name__ == "__main__":
    unittest.main()
