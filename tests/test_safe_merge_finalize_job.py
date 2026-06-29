"""Safe-merge finalize phase job lifecycle tests."""
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


class TestSafeMergeFinalizeJob(unittest.TestCase):
    def _screen_state(self):
        repo = Repo("repo", Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="test")
        header = state.tasks.add("safe-merge repo")
        screen = SafeMergeScreen(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            header_task=header,
            phase="resolve",
        )
        return state, screen

    def test_finalize_success_finishes_job_ok(self) -> None:
        state, screen = self._screen_state()

        def do_commit(_state, target):
            target.commit_sha = "abc123"
            target.phase = "confirm"
            return JobTaskOutcome(JobStatus.OK, "abc123")

        with mock.patch.object(workers, "_safe_merge_do_commit",
                               side_effect=do_commit):
            workers.kick_off_safe_merge_finalize(state, screen)
            _wait_for_job_terminal(state)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "safe-merge-finalize")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(screen.target_repo.path),))
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "abc123")
        self.assertEqual(screen.phase, "confirm")

    def test_finalize_unresolved_finishes_job_warn(self) -> None:
        state, screen = self._screen_state()

        def do_commit(_state, target):
            target.status_note = "1 file needs manual resolution"
            target.phase = "resolve"
            return JobTaskOutcome(JobStatus.WARN, target.status_note)

        with mock.patch.object(workers, "_safe_merge_do_commit",
                               side_effect=do_commit):
            workers.kick_off_safe_merge_finalize(state, screen)
            _wait_for_job_terminal(state)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "1 file needs manual resolution")
        self.assertEqual(screen.phase, "resolve")

    def test_finalize_exception_finishes_job_failed(self) -> None:
        state, screen = self._screen_state()

        with mock.patch.object(
                workers, "_safe_merge_do_commit",
                side_effect=RuntimeError("commit exploded")):
            workers.kick_off_safe_merge_finalize(state, screen)
            _wait_for_job_terminal(state)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "commit exploded")
        self.assertEqual(screen.phase, "resolve")
        self.assertEqual(screen.status_note, "commit failed: commit exploded")

    def test_thread_start_failure_fails_job_and_restores_resolve_phase(self):
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state, screen = self._screen_state()

        with mock.patch.object(workers.threading, "Thread", FailingThread):
            workers.kick_off_safe_merge_finalize(state, screen)

        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")
        self.assertEqual(screen.phase, "resolve")
        self.assertEqual(screen.status_note, "thread start failed")


if __name__ == "__main__":
    unittest.main()
