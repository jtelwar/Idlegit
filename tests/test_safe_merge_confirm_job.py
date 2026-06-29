"""Safe-merge confirm phase job lifecycle tests."""
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
from core.runtime.claims import RefreshClaim  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import Repo  # noqa: E402
from core.state.safe_merge import SafeMergeScreen  # noqa: E402
from core import workers  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestSafeMergeConfirmJob(unittest.TestCase):
    def _screen_state(self):
        repo = Repo("repo", Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="test")
        header = state.tasks.add("safe-merge repo")
        claim = RefreshClaim(state, repo=repo)
        self.assertTrue(claim.acquire())
        screen = SafeMergeScreen(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            header_task=header,
            repo_locked=True,
            repo_refresh_claim=claim,
            phase="confirm",
            commit_sha="abc123",
        )
        return repo, state, screen

    def test_confirm_without_push_finishes_job_ok_and_releases_lock(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = False

        with mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.phase, "done")
        self.assertEqual(screen.header_task.status, "ok")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "safe-merge-confirm")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "abc123")

    def test_confirm_push_failure_finishes_job_warn(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = True

        with mock.patch.object(workers, "_safe_merge_push",
                               return_value=False), \
                mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.header_task.status, "warn")
        self.assertEqual(screen.header_task.message, "push failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "push failed")

    def test_confirm_exception_finishes_job_failed_and_releases_lock(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = True

        with mock.patch.object(
                workers, "_safe_merge_push",
                side_effect=RuntimeError("push exploded")), \
                mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.header_task.status, "fail")
        self.assertEqual(screen.header_task.message, "push exploded")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "push exploded")

    def test_confirm_sync_failure_finishes_job_failed_not_ok(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = True
        screen.is_tracked_submodule = True

        def sync(_state, target):
            child = state.tasks.add("  ↳ sync sibling", parent=target.header_task)
            state.tasks.update(child, "fail", "sync failed")
            return JobTaskOutcome(JobStatus.FAIL, "sync failed")

        with mock.patch.object(workers, "_safe_merge_push",
                               return_value=True), \
                mock.patch.object(workers, "_safe_merge_sync_submodule",
                                  side_effect=sync), \
                mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.header_task.status, "fail")
        self.assertEqual(screen.header_task.message, "sync failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "sync failed")

    def test_confirm_drop_stash_warning_finishes_job_warn(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = False
        screen.confirm_remove_stash = True
        screen.backup_stash_name = "pre-merge"

        with mock.patch.object(workers, "drop_named_stash",
                               return_value=(False, "drop failed")), \
                mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.header_task.status, "warn")
        self.assertEqual(screen.header_task.message, "drop failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "drop failed")

    def test_confirm_success_uses_worker_outcome_not_stale_header_status(self):
        repo, state, screen = self._screen_state()
        screen.confirm_push = False
        state.tasks.update(screen.header_task, "fail", "stale display failure")

        with mock.patch.object(workers, "_safe_merge_refresh_targets"):
            workers.kick_off_safe_merge_confirm(state, screen)
            _wait_for_job_terminal(state)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.header_task.status, "ok")
        self.assertEqual(screen.header_task.message, "abc123")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "abc123")

    def test_thread_start_failure_fails_job_and_releases_lock(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo, state, screen = self._screen_state()

        with mock.patch.object(workers.threading, "Thread", FailingThread):
            workers.kick_off_safe_merge_confirm(state, screen)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertEqual(screen.phase, "done")
        self.assertEqual(screen.header_task.status, "fail")
        self.assertEqual(screen.header_task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
