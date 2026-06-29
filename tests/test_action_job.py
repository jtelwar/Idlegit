"""Single-repo action worker job lifecycle tests."""
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
from core.state.app import State  # noqa: E402
from core.workers import _refresh_target_state, kick_off_action  # noqa: E402


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestActionJobLifecycle(unittest.TestCase):
    def test_successful_fetch_finishes_job_ok_and_releases_lease(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.workers.git", return_value=(0, "", "")), \
                mock.patch("core.workers._refresh_target_state"), \
                mock.patch("core.workers.MIN_ACTION_REFRESH_SECONDS", 0.0):
            kick_off_action(
                state, "fetch",
                target_label="r",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
            )

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: fetch")
        self.assertEqual(task.status, "ok")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "action")
        self.assertEqual(job.spec.label, "r: fetch")
        self.assertTrue(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)

    def test_failed_fetch_finishes_job_failed(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch(
                "core.workers.git",
                return_value=(1, "", "fetch failed")), \
                mock.patch("core.workers._refresh_target_state"), \
                mock.patch("core.workers.MIN_ACTION_REFRESH_SECONDS", 0.0):
            kick_off_action(
                state, "fetch",
                target_label="r",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
            )

        _wait_for_job_terminal(state)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: fetch")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "fetch failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "fetch failed")

    def test_warn_task_finishes_job_warn(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.workers._head_is_ancestor_of",
                        return_value=False), \
                mock.patch("core.workers._refresh_target_state"), \
                mock.patch("core.workers.MIN_ACTION_REFRESH_SECONDS", 0.0):
            kick_off_action(
                state, "switch_branch",
                target_label="r",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                branch_arg="main",
            )

        _wait_for_job_terminal(state)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: checkout main")
        self.assertEqual(task.status, "warn")
        self.assertIn("would orphan", task.message)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, task.message)

    def test_thread_start_failure_releases_lease_and_fails_job(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_action(
                state, "fetch",
                target_label="r",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
            )

        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "r: failed")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


class TestRefreshTargetState(unittest.TestCase):
    def test_refresh_target_state_reconciles_targets_and_workspace_snapshot(self) -> None:
        repo = _make_repo("repo")
        parent = _make_repo("parent")
        other = _make_repo("other")
        state = State(repos=[repo, parent, other], workspace_name="ws")

        with mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            _refresh_target_state(
                state,
                repo,
                parent,
                snapshot_repos=[repo, parent, other],
                snapshot_subtrees=[],
            )

        reconcile.assert_called_once_with(
            [repo, parent],
            [],
            link_repos=[repo, parent, other],
            refresh_fn=mock.ANY,
            link_fn=mock.ANY,
            should_link=None,
        )

    def test_refresh_target_state_dedupes_parent_when_same_as_target(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")

        with mock.patch("core.workers.reconcile_repos_bounded") as reconcile:
            _refresh_target_state(state, repo, repo)

        refresh_targets = reconcile.call_args.args[0]
        self.assertEqual(refresh_targets, [repo])
        self.assertIsNotNone(reconcile.call_args.kwargs["should_link"])

    def test_refresh_target_state_skips_live_relink_after_workspace_switch(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        from core.state.workspaces import Workspace

        ws_a = Workspace(name="A", folders=[Path("/a")], cached_repos=[repo_a])
        ws_b = Workspace(name="B", folders=[Path("/b")], cached_repos=[repo_b])
        state = State(
            repos=[repo_a],
            workspace_name="A",
            workspaces=[ws_a, ws_b],
            active_workspace_index=0,
        )

        def refresh(_repo):
            state.active_workspace_index = 1
            state.repos = [repo_b]

        with (
            mock.patch(
                "core.workers._refresh_repo_snapshot_into_state",
                side_effect=lambda _state, repo: refresh(repo),
            ),
            mock.patch("core.workers.link_siblings") as link,
        ):
            result = _refresh_target_state(state, repo_a, None)

        self.assertTrue(result.link_skipped)
        link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
