"""Clone worker job lifecycle tests."""
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
from core.workers import kick_off_clone  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestCloneWorkerJobLifecycle(unittest.TestCase):
    def test_successful_clone_finishes_job_ok_and_calls_on_done(self) -> None:
        state = _state()
        dest = Path("/tmp/new-repo")
        done = []

        with mock.patch(
                "core.git_ops.clone_repo",
                return_value=(True, "cloned")) as clone_mock:
            kick_off_clone(
                state, "https://example.com/repo.git", dest, "main",
                recurse_submodules=True,
                on_done=lambda ok, msg: done.append((ok, msg)))

        _wait_for_job_terminal(state)
        clone_mock.assert_called_once_with(
            "https://example.com/repo.git",
            dest,
            branch="main",
            recurse_submodules=True,
        )
        self.assertEqual(done, [(True, "cloned")])
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "new-repo: clone")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "cloned")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "clone")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(dest),))
        self.assertEqual(job.status, JobStatus.OK)

    def test_failed_clone_finishes_job_failed_and_calls_on_done(self) -> None:
        state = _state()
        dest = Path("/tmp/new-repo")
        done = []

        with mock.patch(
                "core.git_ops.clone_repo",
                return_value=(False, "clone failed")):
            kick_off_clone(
                state, "https://example.com/repo.git", dest, "",
                recurse_submodules=False,
                on_done=lambda ok, msg: done.append((ok, msg)))

        _wait_for_job_terminal(state)
        self.assertEqual(done, [(False, "clone failed")])
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "clone failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "clone failed")

    def test_on_done_exception_does_not_fail_successful_clone(self) -> None:
        state = _state()
        dest = Path("/tmp/new-repo")

        def bad_callback(_ok: bool, _msg: str) -> None:
            raise RuntimeError("callback exploded")

        with mock.patch(
                "core.git_ops.clone_repo",
                return_value=(True, "cloned")):
            kick_off_clone(
                state, "https://example.com/repo.git", dest, "",
                recurse_submodules=False,
                on_done=bad_callback)

        _wait_for_job_terminal(state)
        self.assertEqual(state.tasks.snapshot()[0].status, "ok")
        self.assertEqual(state.job_registry.snapshot()[0].status, JobStatus.OK)

    def test_thread_start_failure_marks_task_and_job_failed(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        dest = Path("/tmp/new-repo")
        done = []

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_clone(
                state, "https://example.com/repo.git", dest, "",
                recurse_submodules=False,
                on_done=lambda ok, msg: done.append((ok, msg)))

        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        self.assertEqual(done, [(False, "thread start failed")])
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
