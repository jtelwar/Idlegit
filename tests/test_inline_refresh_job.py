"""Inline refresh job lifecycle tests."""
from __future__ import annotations

import sys
import threading
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
)
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.git_ops import LinkSiblingsSnapshot, RepoRefreshSnapshot  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from core import workers  # noqa: E402


def _empty_link_snapshot(repos):
    return LinkSiblingsSnapshot(
        repos=tuple(repos),
        children_by_parent={id(repo): () for repo in repos},
        siblings_by_repo={id(repo): () for repo in repos},
        synthetic_by_url={},
    )


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


class TestInlineRefreshJob(unittest.TestCase):
    def setUp(self) -> None:
        with workers._inline_refresh_lock:
            workers._inline_refresh_targets_in_flight.clear()
            workers._inline_refresh_targets_pending.clear()
            workers._inline_refresh_targets_started_at.clear()
            workers._sync_inline_refresh_flags_locked()

    def _state(self, repo):
        ws = Workspace(
            name="A",
            folders=[repo.path.parent],
            cached_repos=[repo],
        )
        return State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

    def test_manual_refresh_finishes_job_ok_after_gate_cleanup(self) -> None:
        repo = _make_repo("refresh-ok")
        state = self._state(repo)

        with mock.patch.object(workers, "discover_repos",
                               return_value=[repo]), \
                mock.patch.object(
                    workers,
                    "read_repo_refresh_snapshot",
                    return_value=RepoRefreshSnapshot(),
                ), \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=lambda repos, _subtrees, **_kwargs:
                                  _empty_link_snapshot(repos)), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state, manual=True)
            _wait_for_job_kind_terminal(state, "manual-refresh")

        self.assertFalse(workers._inline_refresh_in_flight)
        self.assertFalse(state.store.repo_busy(repo))
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh workspace")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "1 refreshed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "manual-refresh")
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertEqual(job.status, JobStatus.OK)

    def test_manual_refresh_warning_finishes_job_warn(self) -> None:
        repo = _make_repo("refresh-warn")
        state = self._state(repo)

        with mock.patch.object(workers, "discover_repos",
                               return_value=[repo]), \
                mock.patch.object(workers, "read_repo_refresh_snapshot",
                                  side_effect=RuntimeError("index bad")), \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=lambda repos, _subtrees, **_kwargs:
                                  _empty_link_snapshot(repos)), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state, manual=True)
            _wait_for_job_kind_terminal(state, "manual-refresh")

        task = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh workspace")
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "1 refreshed")
        warn = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh-warn: refresh failed")
        self.assertEqual(warn.status, "warn")
        self.assertEqual(warn.message, "index bad")
        self.assertFalse(state.store.repo_busy(repo))
        assert_repo_refresh_available(self, state, repo)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "1 refreshed")

    def test_thread_start_failure_fails_job_and_releases_gate(self) -> None:
        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("refresh-fail")
        state = self._state(repo)

        with mock.patch.object(workers, "discover_repos"), \
                mock.patch.object(workers.threading, "Thread", FailingThread):
            workers.kick_off_inline_refresh(state, manual=True)

        self.assertFalse(workers._inline_refresh_in_flight)
        self.assertFalse(state.store.repo_busy(repo))
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh workspace")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")

    def test_parent_submodules_link_before_entire_refresh_finishes(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        state = self._state(parent)
        state.replace_repos([parent, canonical])
        state.workspaces[0].cached_repos = state.repos
        release_canonical = threading.Event()
        parent_linked = threading.Event()
        canonical_refresh_entered = threading.Event()

        def read_repo_refresh_snapshot(repo, **_kwargs):
            if repo is parent:
                return RepoRefreshSnapshot(
                    nested_subs=[
                        ("github.com/acme/canonical", parent.path / "vendor")
                    ]
                )
            if repo is canonical:
                canonical_refresh_entered.set()
                self.assertTrue(release_canonical.wait(timeout=2.0))
                return RepoRefreshSnapshot()
            return RepoRefreshSnapshot()

        def read_link_siblings_snapshot(_repos, _subtrees, *,
                                        busy_child_predicate=None,
                                        **_kwargs):
            self.assertIsNotNone(busy_child_predicate)
            if parent.nested_subs:
                child = ChildRef(
                    repo=canonical,
                    nested_path=parent.nested_subs[0][1],
                    kind="submodule",
                )
                parent_linked.set()
                return LinkSiblingsSnapshot(
                    repos=tuple(_repos),
                    children_by_parent={id(parent): (child,), id(canonical): ()},
                    siblings_by_repo={id(parent): (), id(canonical): ()},
                    synthetic_by_url={},
                )
            return _empty_link_snapshot(_repos)

        with mock.patch.object(workers, "discover_repos",
                               return_value=[parent, canonical]), \
                mock.patch.object(workers, "read_repo_refresh_snapshot",
                                  side_effect=read_repo_refresh_snapshot), \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=read_link_siblings_snapshot), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state)
            self.assertTrue(canonical_refresh_entered.wait(timeout=2.0))
            self.assertTrue(parent_linked.wait(timeout=2.0))
            parent_id = state.store.repo_id_for(parent)
            self.assertIsNotNone(parent_id)
            deadline = time.monotonic() + 2.0
            while (
                    not state.store.child_records_for_repo(parent_id)
                    and time.monotonic() < deadline):
                time.sleep(0.01)
            self.assertEqual(len(state.store.child_records_for_repo(parent_id)), 1)
            self.assertTrue(state.store.repo_busy(canonical))
            release_canonical.set()
            _wait_for_job_kind_terminal(state, "refresh")

        self.assertFalse(state.store.repo_busy(canonical))

    def test_parent_submodules_do_not_incrementally_link_after_workspace_switch(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        other = _make_repo("other")
        state = self._state(parent)
        state.replace_repos([parent, canonical])
        state.workspaces[0].cached_repos = state.repos
        state.workspaces.append(Workspace(
            name="B",
            folders=[other.path.parent],
            cached_repos=[other],
        ))
        parent_refresh_entered = threading.Event()
        release_parent = threading.Event()
        canonical_refresh_entered = threading.Event()
        link_calls = []

        def read_repo_refresh_snapshot(repo, **_kwargs):
            if repo is parent:
                parent_refresh_entered.set()
                self.assertTrue(release_parent.wait(timeout=2.0))
                return RepoRefreshSnapshot(
                    nested_subs=[
                        ("github.com/acme/canonical", parent.path / "vendor")
                    ]
                )
            if repo is canonical:
                canonical_refresh_entered.set()
                return RepoRefreshSnapshot()
            return RepoRefreshSnapshot()

        with mock.patch.object(workers, "discover_repos",
                               return_value=[parent, canonical]), \
                mock.patch.object(workers, "read_repo_refresh_snapshot",
                                  side_effect=read_repo_refresh_snapshot), \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=lambda repos, _subtrees, **_kwargs:
                                  link_calls.append("link") or
                                  _empty_link_snapshot(repos)), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state)
            self.assertTrue(parent_refresh_entered.wait(timeout=2.0))
            state.active_workspace_index = 1
            state.replace_repos([other])
            release_parent.set()
            self.assertTrue(canonical_refresh_entered.wait(timeout=2.0))
            _wait_for_job_kind_terminal(state, "refresh")

        self.assertEqual(link_calls, [])
        self.assertEqual(parent.children, [])
        self.assertFalse(state.store.repo_busy(parent))
        self.assertFalse(state.store.repo_busy(canonical))

    def test_workspace_mutation_gate_sees_child_only_registry_job(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        state = self._state(parent)
        state.replace_repos([parent, canonical])
        state.job_registry.start(JobSpec(
            kind="child-push",
            label="child push",
            local_mutation=True,
            child_keys=(str(child.nested_path),),
        ))

        self.assertTrue(workers._workspace_has_local_mutation(state, [parent, canonical]))

    def test_workspace_mutation_gate_ignores_unrelated_registry_job(self) -> None:
        repo = _make_repo("repo")
        state = self._state(repo)
        state.job_registry.start(JobSpec(
            kind="commit",
            label="commit other",
            local_mutation=True,
            repo_keys=("/tmp/other",),
        ))

        self.assertFalse(workers._workspace_has_local_mutation(state, [repo]))

    def test_workspace_mutation_gate_uses_explicit_claim(self) -> None:
        repo = _make_repo("repo")
        state = self._state(repo)
        claim_id = state.leases.acquire(repo=repo)
        try:
            self.assertTrue(workers._workspace_has_local_mutation(state, [repo]))
        finally:
            state.leases.release(claim_id)

    def test_inline_refresh_skips_store_busy_repo_before_locking(self) -> None:
        repo = _make_repo("repo")
        state = self._state(repo)
        state.store.set_repo_busy(repo, True)

        with mock.patch.object(workers, "discover_repos", return_value=[repo]), \
                mock.patch.object(workers, "read_repo_refresh_snapshot") as refresh, \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=lambda repos, _subtrees, **_kwargs:
                                  _empty_link_snapshot(repos)), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state, manual=True)
            _wait_for_job_kind_terminal(state, "manual-refresh")

        refresh.assert_not_called()
        self.assertTrue(state.store.repo_busy(repo))
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh workspace")
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "0 refreshed, 1 skipped")

    def test_inline_refresh_skips_registry_owned_repo_before_locking(self) -> None:
        repo = _make_repo("repo")
        state = self._state(repo)
        state.job_registry.start(JobSpec(
            kind="commit",
            label="commit",
            local_mutation=True,
            repo_keys=(str(repo.path),),
        ))

        with mock.patch.object(workers, "discover_repos", return_value=[repo]), \
                mock.patch.object(workers, "read_repo_refresh_snapshot") as refresh, \
                mock.patch.object(workers, "read_link_siblings_snapshot",
                                  side_effect=lambda repos, _subtrees, **_kwargs:
                                  _empty_link_snapshot(repos)), \
                mock.patch("core.fs_watcher.reconcile_repo_watchers"):
            workers.kick_off_inline_refresh(state, manual=True)
            _wait_for_job_kind_terminal(state, "manual-refresh")

        refresh.assert_not_called()
        task = next(t for t in state.tasks.snapshot()
                    if t.label == "refresh workspace")
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "0 refreshed, 1 skipped")


if __name__ == "__main__":
    unittest.main()
