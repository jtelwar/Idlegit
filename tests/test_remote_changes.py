from __future__ import annotations

import sys
import threading
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
from core.jobs import JobStatus  # noqa: E402
from core.state.remotes import RemoteRow  # noqa: E402
from core.workers import kick_off_remote_changes  # noqa: E402


class TestRemoteChangesLocking(unittest.TestCase):
    def _add_row(self) -> RemoteRow:
        return RemoteRow(
            original_name="",
            original_url="",
            name="upstream",
            url="https://example.com/upstream.git",
            is_new=True,
        )

    def test_busy_repo_warns_and_skips_remote_changes(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        with held_repo_refresh(state, repo):
            with mock.patch("core.workers.git") as git_mock:
                count = kick_off_remote_changes(
                    state, [self._add_row()], "r", repo.path, repo)

        self.assertEqual(count, 0)
        git_mock.assert_not_called()
        self.assertEqual(len(state.tasks.items), 1)
        task = state.tasks.items[0]
        self.assertEqual(task.status, "warn")
        self.assertIn("busy", task.message)

    def test_remote_changes_hold_and_release_repo_refresh_mutex(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        refreshed = threading.Event()
        allow_refresh_return = threading.Event()

        def refresh(target) -> None:
            self.assertIs(target, repo)
            self.assertTrue(state.store.repo_busy(repo))
            refreshed.set()
            self.assertTrue(allow_refresh_return.wait(timeout=2.0))

        with mock.patch("core.workers.git", return_value=(0, "", "")) as git_mock, \
                mock.patch("core.workers.refresh_repo_with_remote_state",
                           side_effect=refresh):
            count = kick_off_remote_changes(
                state, [self._add_row()], "r", repo.path, repo)
            self.assertEqual(count, 1)
            self.assertTrue(state.store.repo_busy(repo))
            jobs = state.job_registry.snapshot()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].spec.kind, "remote-edit")
            self.assertTrue(jobs[0].spec.local_mutation)
            self.assertEqual(jobs[0].spec.repo_keys, (str(repo.path),))
            self.assertTrue(state.job_registry.has_active_local_mutation())
            self.assertTrue(refreshed.wait(timeout=2.0))
            self.assertTrue(state.store.repo_busy(repo))
            allow_refresh_return.set()

        assert_repo_refresh_available(self, state, repo, timeout=2.0)
        self.assertFalse(state.store.repo_busy(repo))
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertFalse(state.job_registry.has_active_local_mutation())
        git_mock.assert_called_once_with(
            repo.path,
            ["remote", "add", "upstream", "https://example.com/upstream.git"],
        )
        self.assertEqual(state.tasks.items[0].status, "ok")

    def test_thread_start_failure_releases_repo_refresh_mutex(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            count = kick_off_remote_changes(
                state, [self._add_row()], "r", repo.path, repo)

        self.assertEqual(count, 0)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        self.assertEqual(state.tasks.items[0].status, "fail")
        self.assertEqual(state.tasks.items[0].message, "thread start failed")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        self.assertEqual(jobs[0].message, "thread start failed")

    def test_remote_change_git_failure_marks_job_failed(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch(
                "core.workers.git",
                return_value=(1, "", "rejected")), \
                mock.patch(
                    "core.workers.refresh_repo_with_remote_state"):
            count = kick_off_remote_changes(
                state, [self._add_row()], "r", repo.path, repo)

        self.assertEqual(count, 1)
        deadline = threading.Event()
        # The worker is short-lived; polling avoids depending on scheduler
        # timing in the assertion path.
        for _ in range(200):
            jobs = state.job_registry.snapshot()
            if jobs and jobs[0].terminal:
                deadline.set()
                break
            threading.Event().wait(0.01)
        self.assertTrue(deadline.is_set())
        self.assertEqual(state.tasks.items[0].status, "fail")
        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        self.assertEqual(jobs[0].message, "rejected")


if __name__ == "__main__":
    unittest.main()
