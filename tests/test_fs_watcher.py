"""Tests for `core/fs_watcher.py`.

The fs-watcher manages watchdog Observers on every repo in the active
workspace and runs a debounced `refresh_repo` when an event arrives.
These tests sidestep watchdog (real fs events are noisy + slow + OS-
dependent) by driving the public API directly:
  - `WatcherManager.reconcile()` for the lifecycle
  - `RepoWatcher.on_event()` / `_on_timer()` for the debounce / fire path

The real `refresh_repo` is swapped for a recorder via the manager's
injected `_refresh_fn`, so we can assert which repos were refreshed in
which order without depending on git.
"""
from __future__ import annotations

import sys
import tempfile
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
    assert_child_refresh_available,
    assert_child_refresh_blocked,
    assert_repo_refresh_available,
    assert_repo_refresh_blocked,
    held_child_refresh,
    held_repo_refresh,
)
from core import config, fs_watcher  # noqa: E402
from core.git_ops import LinkSiblingsSnapshot  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.fs_watcher import (  # noqa: E402
    MIN_DEBOUNCE_SECONDS,
    RepoWatcher,
    WatcherManager,
    _compile_ignore_spec,
    _is_internal_git_path,
    _matches_ignore_spec,
)
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef, Repo  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402


def _make_repo(path: str) -> Repo:
    """Bare Repo with just the fields the watcher reads (`path` and
    `refreshing`). Avoids the discover_repos / refresh_repo pipeline so
    these tests don't touch a real working tree."""
    return Repo(rel=".", path=Path(path))


def _empty_link_snapshot(repos):
    return LinkSiblingsSnapshot(
        repos=tuple(repos),
        children_by_parent={id(repo): () for repo in repos},
        siblings_by_repo={id(repo): () for repo in repos},
        synthetic_by_url={},
    )


def _make_child_ref(path_suffix: str = "sub") -> ChildRef:
    """Bare ChildRef for mutex testing. Nests a fresh Repo under a
    fake parent path so equality + the store-owned child lock work
    independently of any tracked Repo object."""
    inner = _make_repo(f"/tmp/{path_suffix}/inner")
    return ChildRef(repo=inner, nested_path=Path(f"/tmp/{path_suffix}"))


def _make_state(*, on: bool = True, debounce_ms: int = 100,
                repos=None) -> State:
    return State(
        repos=list(repos or []),
        workspace_name="test",
        auto_refresh_on_fs_change=on,
        auto_refresh_debounce_ms=debounce_ms,
    )


class InternalPathFilterTests(unittest.TestCase):
    """`_is_internal_git_path` culls ALL writes inside `.git/`.

    The broader filter (over the original `.git/objects/` + `.lock`
    scope) breaks an infinite-loop where `refresh_repo`'s own
    `git status` call writes the stat cache to `.git/index`, fires
    an event, resets the debounce timer, and triggers another
    refresh. The filter has to handle both POSIX and Windows path
    separators in `event.src_path`."""

    def test_filters_objects_dir(self):
        self.assertTrue(_is_internal_git_path(
            "/tmp/repo/.git/objects/ab/cdef"))
        self.assertTrue(_is_internal_git_path(
            "C:\\repo\\.git\\objects\\ab\\cdef"))

    def test_filters_lock_files_in_git_dir(self):
        self.assertTrue(_is_internal_git_path(
            "/tmp/repo/.git/index.lock"))
        self.assertTrue(_is_internal_git_path(
            "/tmp/repo/.git/refs/heads/main.lock"))

    def test_filters_index_and_head(self):
        # These used to pass through; broadening the filter so the
        # `git status` stat-cache write to `.git/index` stops looping.
        self.assertTrue(_is_internal_git_path("/tmp/repo/.git/HEAD"))
        self.assertTrue(_is_internal_git_path("/tmp/repo/.git/index"))
        self.assertTrue(_is_internal_git_path(
            "/tmp/repo/.git/refs/heads/main"))

    def test_passes_working_tree_writes(self):
        self.assertFalse(_is_internal_git_path("/tmp/repo/file.py"))
        self.assertFalse(_is_internal_git_path(
            "/tmp/repo/src/widget.py"))

    def test_passes_empty_path(self):
        # Directory events sometimes arrive with empty src_path on some
        # platforms — the filter should let them through (the debounce
        # will absorb any spurious follow-up).
        self.assertFalse(_is_internal_git_path(""))


class RepoWatcherDebounceTests(unittest.TestCase):
    """Debounce timer behaviour: events coalesce within the window;
    the timer fires once after the window settles."""

    def test_single_event_fires_after_debounce(self):
        repo = _make_repo("/tmp/repo-A")
        state = _make_state(debounce_ms=50, repos=[repo])
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        watcher.on_event("/tmp/repo-X/file.py")
        # Sleep past the debounce window — the Timer runs on its own
        # thread, give it room.
        time.sleep(0.2)
        self.assertEqual(fires, [Path("/tmp/repo-A")])

    def test_burst_coalesces_into_single_fire(self):
        repo = _make_repo("/tmp/repo-B")
        state = _make_state(debounce_ms=80, repos=[repo])
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        for _ in range(10):
            watcher.on_event("/tmp/repo-X/file.py")
            time.sleep(0.01)
        time.sleep(0.2)
        # Even with 10 events fired in rapid succession, only one
        # refresh should land. The single-thread debounce keeps
        # extending its sleep deadline as events arrive rather than
        # spawning per-event Timer threads (which on macOS could hit
        # the per-process thread limit and trigger kernel_task
        # throttling under heavy fs event load).
        self.assertEqual(len(fires), 1)

    def test_thread_start_failure_queues_for_later_drain(self):
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def is_alive(self):
                return False

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("/tmp/repo-thread-fail")
        state = _make_state(debounce_ms=50, repos=[repo])
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        manager._repos[repo.path] = watcher

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            watcher.on_event("/tmp/repo-thread-fail/source.py")

        with watcher._lock:
            self.assertIsNone(watcher._timer_thread)
            self.assertGreater(watcher._fire_at, 0.0)
        self.assertIn(repo.path, manager._pending)

    def test_min_debounce_clamp(self):
        # A pathological debounce_ms (e.g. 0) clamps to
        # MIN_DEBOUNCE_SECONDS so we don't busy-fire.
        repo = _make_repo("/tmp/repo-C")
        state = _make_state(debounce_ms=0, repos=[repo])
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        before = time.monotonic()
        watcher.on_event("/tmp/repo-X/file.py")
        with watcher._lock:
            fire_at = watcher._fire_at
            # Tell the persistent debounce thread to exit cleanly so
            # the test doesn't leak a sleeping daemon thread.
            watcher._stopped = True
        # `_fire_at` is set to `now + delay`. The clamp guarantees
        # delay >= MIN_DEBOUNCE_SECONDS regardless of the configured
        # debounce_ms.
        self.assertGreaterEqual(fire_at - before, MIN_DEBOUNCE_SECONDS)


class SuppressionTests(unittest.TestCase):
    """Suppression rules: skip when store-owned row busy state is active;
    queue rather than fire when the review sub-loop owns input."""

    def test_skip_when_repo_row_is_store_busy(self):
        repo = _make_repo("/tmp/repo-D")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.store.set_repo_busy(repo, True)
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()  # bypass the debounce, hit the gate directly
        self.assertEqual(fires, [])
        self.assertNotIn(repo.path, manager._pending)

    def test_on_event_short_circuits_when_repo_row_is_store_busy(self):
        repo = _make_repo("/tmp/repo-store-busy")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.store.set_repo_busy(repo, True)
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        manager._repos[repo.path] = watcher

        watcher.on_event("/tmp/repo-store-busy/file.py")

        with watcher._lock:
            self.assertIsNone(watcher._timer_thread)
            self.assertEqual(watcher._fire_at, 0.0)
        self.assertTrue(state.store.repo_busy(repo))
        self.assertNotIn(repo.path, manager._pending)

    def test_queue_when_in_review(self):
        repo = _make_repo("/tmp/repo-E")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.in_review = True
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [])
        # Path should be queued for the post-review drain.
        self.assertIn(repo.path, manager._pending)

    def test_drain_pending_fires_queued_refreshes(self):
        repo_a = _make_repo("/tmp/repo-F")
        repo_b = _make_repo("/tmp/repo-G")
        state = _make_state(debounce_ms=20, repos=[repo_a, repo_b])
        fires = []
        drained = threading.Event()

        def refresh(repo):
            fires.append(repo.path)
            if len(fires) == 2:
                drained.set()

        manager = WatcherManager()
        for r in (repo_a, repo_b):
            w = RepoWatcher(
                state, r, manager=manager,
                refresh_fn=refresh)
            manager._repos[r.path] = w
        manager._pending.add(repo_a.path)
        manager._pending.add(repo_b.path)
        manager.drain_pending()
        self.assertTrue(drained.wait(timeout=2.0))
        self.assertEqual(set(fires), {repo_a.path, repo_b.path})
        self.assertEqual(manager._pending, set())
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "fs-watch-drain")
        deadline = time.monotonic() + 2.0
        while not jobs[0].terminal and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(
            set(jobs[0].spec.repo_keys),
            {str(repo_a.path), str(repo_b.path)},
        )

    def test_drain_pending_thread_start_failure_requeues_paths(self):
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("/tmp/repo-drain-thread-fail")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda _repo: None)
        manager._repos[repo.path] = watcher
        manager._pending.add(repo.path)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            manager.drain_pending()

        self.assertIn(repo.path, manager._pending)
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "fs-watch-drain")
        self.assertEqual(jobs[0].status, JobStatus.FAIL)

    def test_drain_pending_refresh_failure_marks_job_warning(self):
        repo = _make_repo("/tmp/repo-drain-refresh-fail")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()

        def refresh(_repo):
            raise RuntimeError("refresh failed")

        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=refresh)
        manager._repos[repo.path] = watcher
        manager._pending.add(repo.path)

        manager.drain_pending()

        jobs = state.job_registry.snapshot()
        deadline = time.monotonic() + 2.0
        while not jobs[0].terminal and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs[0].status, JobStatus.WARN)
        self.assertEqual(jobs[0].message, "1 refresh failed")
        self.assertFalse(state.store.repo_busy(repo))

    def test_drain_pending_link_failure_marks_job_warning(self):
        repo = _make_repo("/tmp/repo-drain-link-fail")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda _repo: None)
        manager._repos[repo.path] = watcher
        manager._pending.add(repo.path)

        with mock.patch(
                "core.fs_watcher.read_link_siblings_snapshot",
                side_effect=RuntimeError("link failed")):
            manager.drain_pending()

        jobs = state.job_registry.snapshot()
        deadline = time.monotonic() + 2.0
        while not jobs[0].terminal and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(jobs[0].status, JobStatus.WARN)
        self.assertEqual(jobs[0].message, "1 link failed")
        self.assertFalse(state.store.repo_busy(repo))

    def test_drain_pending_requeues_busy_repo(self):
        repo = _make_repo("/tmp/repo-drain-busy")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda _repo: None)
        manager._repos[repo.path] = watcher
        manager._pending.add(repo.path)

        with held_repo_refresh(state, repo):
            manager.drain_pending()

            jobs = state.job_registry.snapshot()
            deadline = time.monotonic() + 2.0
            while not jobs[0].terminal and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(jobs[0].status, JobStatus.WARN)
            self.assertEqual(jobs[0].message, "1 busy")
            self.assertIn(repo.path, manager._pending)

    def test_drain_pending_skips_relink_after_workspace_switch(self):
        repo_a = _make_repo("/tmp/repo-drain-workspace-a")
        repo_b = _make_repo("/tmp/repo-drain-workspace-b")
        state = _make_state(debounce_ms=20, repos=[repo_a])
        manager = WatcherManager()
        refresh_entered = threading.Event()
        allow_refresh_exit = threading.Event()
        links = []

        def refresh(_repo):
            refresh_entered.set()
            self.assertTrue(allow_refresh_exit.wait(timeout=2.0))

        watcher = RepoWatcher(
            state, repo_a, manager=manager,
            refresh_fn=refresh)
        manager._repos[repo_a.path] = watcher
        manager._pending.add(repo_a.path)

        with mock.patch(
                "core.fs_watcher.read_link_siblings_snapshot",
                side_effect=lambda repos, _subtrees, **_kwargs:
                links.extend(repo.path for repo in repos) or
                _empty_link_snapshot(repos)):
            manager.drain_pending()
            self.assertTrue(refresh_entered.wait(timeout=2.0))
            state.workspace_name = "other"
            state.repos = [repo_b]
            allow_refresh_exit.set()

            jobs = state.job_registry.snapshot()
            deadline = time.monotonic() + 2.0
            while not jobs[0].terminal and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertEqual(links, [])

    def test_drain_pending_returns_without_waiting_for_refresh(self):
        repo = _make_repo("/tmp/repo-async-drain")
        state = _make_state(debounce_ms=20, repos=[repo])
        entered = threading.Event()
        release = threading.Event()

        def refresh(_repo):
            entered.set()
            self.assertTrue(release.wait(timeout=2.0))

        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=refresh)
        manager._repos[repo.path] = watcher
        manager._pending.add(repo.path)
        manager.drain_pending()
        self.assertEqual(manager._pending, set())
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertTrue(state.store.repo_busy(repo))
        release.set()
        assert_repo_refresh_available(self, state, repo, timeout=2.0)
        self.assertFalse(state.store.repo_busy(repo))

    def test_queue_when_local_mutation_job_running(self):
        # Multi-repo actions like smart-sync mutate the working tree
        # across siblings. Per-event refreshes during the action would
        # race the action and thrash the spinner; instead we queue
        # everything and drain once local mutation jobs finish.
        repo = _make_repo("/tmp/repo-tasks")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.leases.acquire(repo=repo)
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [])
        # The repo path should be queued for the drain that runs when
        # the main loop sees local mutation jobs transition False.
        self.assertIn(repo.path, manager._pending)

    def test_pending_task_without_mutation_owner_does_not_queue(self):
        repo = _make_repo("/tmp/repo-pending-ui-only")
        state = _make_state(debounce_ms=20, repos=[repo])
        pending = state.tasks.add("workflow follow-up")
        state.tasks.update(pending, "pending", "waiting")
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [repo.path])
        self.assertNotIn(repo.path, manager._pending)

    def test_registry_mutation_job_queues_refresh(self):
        repo = _make_repo("/tmp/repo-registry-job")
        state = _make_state(debounce_ms=20, repos=[repo])
        job = state.job_registry.start(
            JobSpec(kind="commit", label="commit", local_mutation=True))
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [])
        self.assertIn(repo.path, manager._pending)

        state.job_registry.finish(job, JobStatus.OK)
        manager.drain_pending()
        deadline = time.monotonic() + 2.0
        while not fires and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(fires, [repo.path])

    def test_unrelated_registry_mutation_job_does_not_queue_refresh(self):
        repo = _make_repo("/tmp/repo-registry-unrelated")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.job_registry.start(JobSpec(
            kind="commit",
            label="commit",
            local_mutation=True,
            repo_keys=("/tmp/other-repo",),
        ))
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [repo.path])
        self.assertNotIn(repo.path, manager._pending)

    def test_child_registry_mutation_job_queues_parent_refresh(self):
        repo = _make_repo("/tmp/repo-registry-child")
        child = ChildRef(repo=_make_repo("/tmp/canonical-child"),
                         nested_path=repo.path / "vendor" / "sdk")
        repo.children = [child]
        state = _make_state(debounce_ms=20, repos=[repo])
        state.job_registry.start(JobSpec(
            kind="child-push",
            label="child push",
            local_mutation=True,
            child_keys=(str(child.nested_path),),
        ))
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [])
        self.assertIn(repo.path, manager._pending)

    def test_unrelated_task_mutation_claim_does_not_queue_refresh(self):
        repo = _make_repo("/tmp/repo-task-unrelated")
        other = _make_repo("/tmp/other-task-repo")
        state = _make_state(debounce_ms=20, repos=[repo, other])
        state.leases.acquire(repo=other)
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [repo.path])
        self.assertNotIn(repo.path, manager._pending)

    def test_on_event_queues_even_when_repo_already_refreshing(self):
        # CRITICAL: when a commit pipeline holds the refresh lock, the
        # repo's `refreshing` flag is True AND a task is running. The
        # gate-order audit caught a bug here: an earlier version of
        # `on_event` returned on store-owned row busy state BEFORE the
        # local-mutation check, so user edits during a commit
        # were dropped entirely (no post-task drain would fire). The
        # tasks gate now runs first so the event is queued.
        repo = _make_repo("/tmp/repo-gate-order")
        state = _make_state(debounce_ms=20, repos=[repo])
        # Simulate the commit pipeline: lock held + task running +
        # `refreshing` raised. All three are true at once during a
        # real commit.
        with held_repo_refresh(state, repo):
            state.leases.acquire(repo=repo)
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: None)
            manager._repos[repo.path] = watcher
            watcher.on_event("/tmp/repo-gate-order/file.py")
            # No new debounce thread, no fire — but the path IS queued
            # for the post-task drain.
            with watcher._lock:
                self.assertIsNone(watcher._timer_thread)
                self.assertEqual(watcher._fire_at, 0.0)
            self.assertIn(repo.path, manager._pending)

    def test_on_timer_queues_even_when_repo_already_refreshing(self):
        # Same gate-order check applied to `_on_timer` — exercises
        # the code path where a debounce thread settled BEFORE the
        # commit pipeline acquired the lock. By the time the timer
        # fires, the pipeline holds both `refreshing=True` and a
        # local mutation job. The event must end up in `_pending` (drained
        # at mutation-active → idle), not dropped.
        repo = _make_repo("/tmp/repo-timer-gate")
        state = _make_state(debounce_ms=20, repos=[repo])
        with held_repo_refresh(state, repo):
            state.leases.acquire(repo=repo)
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: None)
            manager._repos[repo.path] = watcher
            watcher._on_timer()
            self.assertIn(repo.path, manager._pending)

    def test_on_event_short_circuits_when_tasks_running(self):
        # When tasks are running we shouldn't even start the debounce
        # thread — that's what made multi-repo syncs amplify into the
        # spinner-thrashing pathology before this gate.
        repo = _make_repo("/tmp/repo-fast-bail")
        state = _make_state(debounce_ms=200, repos=[repo])
        state.leases.acquire(repo=repo)
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        manager._repos[repo.path] = watcher
        watcher.on_event("/tmp/repo-fast-bail/file.py")
        with watcher._lock:
            self.assertIsNone(watcher._timer_thread)
            self.assertEqual(watcher._fire_at, 0.0)
        # And the repo got queued for post-task drain.
        self.assertIn(repo.path, manager._pending)

    def test_drain_skips_unknown_paths(self):
        # If a repo vanishes (workspace switch) between queue and
        # drain, the drain should silently skip rather than KeyError.
        manager = WatcherManager()
        manager._pending.add(Path("/tmp/stale-repo"))
        manager.drain_pending()  # would raise if not guarded
        self.assertEqual(manager._pending, set())


class FireRefreshTests(unittest.TestCase):
    """`fire_refresh` holds store-owned row busy state for the duration
    of the refresh call. Idempotent against concurrent fires."""

    def test_sets_store_busy_during_call(self):
        repo = _make_repo("/tmp/repo-H")
        state = _make_state(debounce_ms=20, repos=[repo])
        saw_busy = []

        def recorder(r):
            saw_busy.append(state.store.repo_busy(r))

        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager, refresh_fn=recorder)
        watcher.fire_refresh()
        self.assertEqual(saw_busy, [True])
        self.assertFalse(state.store.repo_busy(repo))

    def test_fire_skipped_when_lock_held_by_another_source(self):
        # The store refresh mutex is the real mutex now; just setting
        # store busy state from the outside doesn't claim the lock.
        # Simulate another source (Ctrl+R, action menu) holding the
        # slot by acquiring through the proper API, then assert that
        # fire_refresh bails without running git.
        repo = _make_repo("/tmp/repo-I")
        state = _make_state(debounce_ms=20, repos=[repo])
        with held_repo_refresh(state, repo):
            fires = []
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: fires.append(r))
            watcher.fire_refresh()
            self.assertEqual(fires, [])
            assert_repo_refresh_blocked(self, state, repo)

    def test_fire_refresh_bails_when_watcher_stopped(self):
        # `_stopped` is set by `detach()` (called by reconcile when a
        # repo vanishes, or by stop_all). A `drain_pending_refreshes`
        # that fires after the watcher was torn down should not run
        # `refresh_repo` on it — the manager doesn't track this
        # watcher anymore and a refresh would do useless work on a
        # Repo the user has navigated away from.
        repo = _make_repo("/tmp/repo-stopped")
        state = _make_state(debounce_ms=20, repos=[repo])
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        watcher._stopped = True  # simulate post-detach state
        watcher.fire_refresh()
        self.assertEqual(fires, [])
        # The lock should never have been acquired because `_stopped`
        # short-circuits before claiming the store mutex.
        assert_repo_refresh_available(self, state, repo)

    def test_refresh_error_restores_store_busy(self):
        # If the injected refresh raises, store-owned busy state must still
        # clear so a transient git error doesn't leave the row stuck busy.
        repo = _make_repo("/tmp/repo-J")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()

        def boom(_r):
            raise RuntimeError("simulated git failure")

        watcher = RepoWatcher(
            state, repo, manager=manager, refresh_fn=boom)
        with self.assertRaises(RuntimeError):
            watcher.fire_refresh()
        self.assertFalse(state.store.repo_busy(repo))


class ReconcileTests(unittest.TestCase):
    """`WatcherManager.reconcile()` keeps the watcher set in sync with
    `state.repos`. These tests stub the real watchdog Observer with a
    recording fake so we can assert schedule/unschedule calls without
    spawning a real fs-events thread."""

    def _patch_observer(self):
        """Patch the Observer class used by WatcherManager with a fake
        that records schedule/unschedule/stop without spinning threads."""

        class FakeWatch:
            def __init__(self, path):
                self.path = path

        class FakeObserver:
            def __init__(self):
                self.scheduled = []
                self.unscheduled = []
                self.started = False
                self.stopped = False

            def schedule(self, handler, path, recursive=False):
                w = FakeWatch(path)
                self.scheduled.append((path, recursive))
                return w

            def unschedule(self, watch):
                self.unscheduled.append(watch.path)

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

            def join(self, timeout=None):
                pass

        return mock.patch.object(fs_watcher, "Observer", FakeObserver)

    def test_reconcile_disabled_stops_existing(self):
        repo = _make_repo("/tmp/repo-K")
        state = _make_state(on=False, repos=[repo])
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            # Disabled feature → no Observer was started.
            self.assertIsNone(manager._observer)

    def test_reconcile_attaches_for_each_repo(self):
        repo_a = _make_repo("/tmp/repo-L")
        repo_b = _make_repo("/tmp/repo-M")
        state = _make_state(on=True, repos=[repo_a, repo_b])
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            self.assertIsNotNone(manager._observer)
            self.assertEqual(
                {str(repo_a.path), str(repo_b.path)},
                {p for p, _ in manager._observer.scheduled})
            self.assertEqual(set(manager._repos.keys()),
                             {repo_a.path, repo_b.path})

    def test_reconcile_drops_vanished_repos(self):
        repo_a = _make_repo("/tmp/repo-N")
        repo_b = _make_repo("/tmp/repo-O")
        state = _make_state(on=True, repos=[repo_a, repo_b])
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            # Drop repo_b from the workspace and reconcile again.
            state.repos = [repo_a]
            manager.reconcile(state)
            self.assertEqual(set(manager._repos.keys()), {repo_a.path})
            self.assertIn(str(repo_b.path), manager._observer.unscheduled)

    def test_reconcile_idempotent_for_unchanged_repo(self):
        # Same repo twice → second reconcile shouldn't re-schedule.
        repo = _make_repo("/tmp/repo-P")
        state = _make_state(on=True, repos=[repo])
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            initial = list(manager._observer.scheduled)
            manager.reconcile(state)
            self.assertEqual(manager._observer.scheduled, initial)

    def test_disable_after_enable_tears_observer_down(self):
        repo = _make_repo("/tmp/repo-Q")
        state = _make_state(on=True, repos=[repo])
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            self.assertIsNotNone(manager._observer)
            state.auto_refresh_on_fs_change = False
            manager.reconcile(state)
            self.assertIsNone(manager._observer)
            self.assertEqual(manager._repos, {})

    def test_disable_wakes_pending_debounce_threads(self):
        repo = _make_repo("/tmp/repo-Q2")
        state = _make_state(on=True, repos=[repo], debounce_ms=10_000)
        manager = WatcherManager()
        with self._patch_observer():
            manager.reconcile(state)
            watcher = manager._repos[repo.path]
            watcher.on_event(str(repo.path / "changed.txt"))
            thread = watcher._timer_thread
            self.assertIsNotNone(thread)
            state.auto_refresh_on_fs_change = False
            manager.reconcile(state)
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())


class ConfigRoundTripTests(unittest.TestCase):
    """Auto-refresh conf keys round-trip through `load_config()`."""

    def setUp(self):
        # Stash + clear cached load_warnings; load_config clears them
        # too but explicit cleanup keeps test ordering hygienic.
        self._prev_warnings = list(config.get_load_warnings())

    def test_defaults_present_in_config(self):
        cfg = config.Config()
        self.assertFalse(cfg.auto_refresh_on_fs_change)
        self.assertEqual(cfg.auto_refresh_debounce_ms, 400)
        self.assertEqual(cfg.periodic_refresh_seconds, 60)

    def test_load_config_picks_up_user_overrides(self):
        # Point CONFIG_FILE at a temp file with our two keys set
        # explicitly and assert load_config picks them up.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "idlegit.conf"
            conf_path.write_text(
                "[idlegit]\n"
                "auto_refresh_on_fs_change = false\n"
                "auto_refresh_debounce_ms = 750\n"
                "periodic_refresh_seconds = 30\n"
            )
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        self.assertFalse(cfg.auto_refresh_on_fs_change)
        self.assertEqual(cfg.auto_refresh_debounce_ms, 750)
        self.assertEqual(cfg.periodic_refresh_seconds, 30)

    def test_load_config_clamps_too_small_debounce(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "idlegit.conf"
            conf_path.write_text(
                "[idlegit]\nauto_refresh_debounce_ms = 5\n")
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        # Floor is 50 ms — anything smaller risks busy-fire on atomic
        # editor saves (write tmp + rename pattern).
        self.assertEqual(cfg.auto_refresh_debounce_ms, 50)

    def test_load_config_treats_periodic_refresh_below_one_as_off(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "idlegit.conf"
            conf_path.write_text(
                "[idlegit]\nperiodic_refresh_seconds = 0.5\n")
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        self.assertEqual(cfg.periodic_refresh_seconds, 0)


class RefreshMutexTests(unittest.TestCase):
    """Store-owned refresh mutexes provide cross-source exclusion."""

    def test_repo_mutex_claims_until_release(self):
        repo = _make_repo("/tmp/mutex-A")
        state = _make_state(debounce_ms=20, repos=[repo])
        acquired, repo_id = state.store.acquire_repo_refresh(repo)
        self.assertTrue(acquired)
        assert_repo_refresh_blocked(self, state, repo)
        state.store.release_repo_refresh_by_id(repo_id)
        assert_repo_refresh_available(self, state, repo)

    def test_release_is_idempotent_when_not_held(self):
        repo = _make_repo("/tmp/mutex-C")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.store.release_repo_refresh_by_id(state.store.repo_id_for(repo))
        assert_repo_refresh_available(self, state, repo)

    def test_release_does_not_clear_store_busy_when_lock_already_unlocked(self):
        repo = _make_repo("/tmp/mutex-C2")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.store.set_repo_busy(repo, True)
        state.store.release_repo_refresh_by_id(state.store.repo_id_for(repo))
        self.assertTrue(state.store.repo_busy(repo))
        state.store.set_repo_busy(repo, False)

    def test_each_repo_has_independent_lock(self):
        repo_a = _make_repo("/tmp/mutex-D")
        repo_b = _make_repo("/tmp/mutex-E")
        state = _make_state(debounce_ms=20, repos=[repo_a, repo_b])
        with held_repo_refresh(state, repo_a):
            assert_repo_refresh_available(self, state, repo_b)

    def test_child_ref_mutex_acquires_and_releases(self):
        ref = _make_child_ref("mutex-child-A")
        parent = _make_repo("/tmp/child-parent-A")
        parent.children = [ref]
        state = _make_state(debounce_ms=20, repos=[parent, ref.repo])
        acquired, child_id = state.store.acquire_child_refresh(ref)
        self.assertTrue(acquired)
        assert_child_refresh_blocked(self, state, ref)
        assert_repo_refresh_available(self, state, ref.repo)
        state.store.release_child_refresh_by_id(child_id)
        assert_child_refresh_available(self, state, ref)

    def test_child_ref_release_does_not_clear_store_busy_when_unlocked(self):
        ref = _make_child_ref("mutex-child-C")
        parent = _make_repo("/tmp/child-parent-C")
        parent.children = [ref]
        state = _make_state(debounce_ms=20, repos=[parent, ref.repo])
        state.store.set_child_busy(ref, True)
        state.store.release_child_refresh_by_id(state.store.child_id_for(ref))
        self.assertTrue(state.store.child_busy(ref))
        state.store.set_child_busy(ref, False)

    def test_fs_watcher_skips_refresh_when_lock_held_elsewhere(self):
        repo = _make_repo("/tmp/mutex-F")
        state = _make_state(debounce_ms=20, repos=[repo])
        with held_repo_refresh(state, repo):
            fires = []
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: fires.append(r.path))
            watcher.fire_refresh()
            self.assertEqual(fires, [])


class BlockingAcquireTests(unittest.TestCase):
    """Store-owned refresh mutexes support bounded blocking acquisition."""

    def test_blocking_acquire_succeeds_when_free(self):
        repo = _make_repo("/tmp/blocking-A")
        state = _make_state(debounce_ms=20, repos=[repo])
        acquired, repo_id = state.store.acquire_repo_refresh(
            repo,
            timeout=0.5,
        )
        self.assertTrue(acquired)
        assert_repo_refresh_blocked(self, state, repo)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_blocking_acquire_waits_for_release(self):
        repo = _make_repo("/tmp/blocking-B")
        state = _make_state(debounce_ms=20, repos=[repo])
        acquired, repo_id = state.store.acquire_repo_refresh(repo)
        self.assertTrue(acquired)

        def release_after_delay() -> None:
            time.sleep(0.1)
            state.store.release_repo_refresh_by_id(repo_id)

        threading.Thread(target=release_after_delay, daemon=True).start()
        before = time.monotonic()
        acquired, reacquired_id = state.store.acquire_repo_refresh(
            repo,
            timeout=2.0,
        )
        waited = time.monotonic() - before
        self.assertTrue(acquired)
        self.assertGreaterEqual(waited, 0.05)
        self.assertLess(waited, 1.0)
        state.store.release_repo_refresh_by_id(reacquired_id)

    def test_blocking_acquire_times_out_when_held_forever(self):
        repo = _make_repo("/tmp/blocking-C")
        state = _make_state(debounce_ms=20, repos=[repo])
        with held_repo_refresh(state, repo):
            before = time.monotonic()
            acquired, _repo_id = state.store.acquire_repo_refresh(
                repo,
                timeout=0.2,
            )
            waited = time.monotonic() - before
            self.assertFalse(acquired)
            self.assertGreaterEqual(waited, 0.15)

    def test_blocking_acquire_claims_mutex_on_success(self):
        repo = _make_repo("/tmp/blocking-D")
        state = _make_state(debounce_ms=20, repos=[repo])
        acquired, repo_id = state.store.acquire_repo_refresh(
            repo,
            timeout=0.1,
        )
        self.assertTrue(acquired)
        assert_repo_refresh_blocked(self, state, repo)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_blocking_acquire_leaves_current_lock_on_timeout(self):
        repo = _make_repo("/tmp/blocking-E")
        state = _make_state(debounce_ms=20, repos=[repo])
        with held_repo_refresh(state, repo):
            acquired, _repo_id = state.store.acquire_repo_refresh(
                repo,
                timeout=0.1,
            )
            self.assertFalse(acquired)
            assert_repo_refresh_blocked(self, state, repo)

    def test_child_ref_blocking_acquire_independent(self):
        ref = _make_child_ref("blocking-child")
        parent = _make_repo("/tmp/blocking-child-parent")
        parent.children = [ref]
        state = _make_state(debounce_ms=20, repos=[parent, ref.repo])
        acquired, child_id = state.store.acquire_child_refresh(
            ref,
            timeout=0.5,
        )
        self.assertTrue(acquired)
        assert_child_refresh_blocked(self, state, ref)
        assert_repo_refresh_available(self, state, ref.repo)
        state.store.release_child_refresh_by_id(child_id)


class IgnorePatternTests(unittest.TestCase):
    """Per-workspace `fs_watch_ignore` patterns use full gitignore
    semantics via `pathspec`. These tests pin down the common matching
    cases the user is most likely to hit so a future pathspec upgrade
    can't silently change behaviour."""

    def _spec(self, patterns):
        return _compile_ignore_spec(patterns)

    def test_empty_list_returns_none_spec(self):
        # `None` lets the matcher short-circuit before computing a
        # relative path — important on the hot event path.
        self.assertIsNone(self._spec([]))

    def test_star_glob_matches_extension(self):
        spec = self._spec(["*.log"])
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/foo.log"))
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/sub/foo.log"))
        self.assertFalse(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/foo.py"))

    def test_double_star_matches_recursive(self):
        spec = self._spec(["build/**"])
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/build/out.o"))
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/build/nested/out.o"))
        self.assertFalse(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/src/out.o"))

    def test_negation_re_includes(self):
        # gitignore semantics: a later `!` un-ignores an earlier match.
        spec = self._spec(["*.log", "!keep.log"])
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/foo.log"))
        self.assertFalse(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/keep.log"))

    def test_anchored_pattern_only_matches_root(self):
        spec = self._spec(["/dist"])
        self.assertTrue(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/dist"))
        # Nested `dist` is not anchored — leading `/` says root-only.
        self.assertFalse(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/tmp/repo/src/dist"))

    def test_event_outside_repo_does_not_match(self):
        # If the watcher somehow fires with a path outside the repo
        # (shouldn't happen, but the protocol leaves it possible), the
        # match should return False rather than crashing.
        spec = self._spec(["*.log"])
        self.assertFalse(_matches_ignore_spec(
            spec, Path("/tmp/repo"), "/elsewhere/foo.log"))

    def test_malformed_pattern_falls_back_to_no_match(self):
        # pathspec is forgiving but pathologically broken input could
        # raise; helper returns None so the watcher behaves as if the
        # entire list were empty rather than half-applying.
        spec = self._spec(None)
        self.assertIsNone(spec)


class IgnorePatternsAppliedToWatcherTests(unittest.TestCase):
    """End-to-end: an event whose path matches `state.fs_watch_ignore`
    must NOT schedule the debounce timer. Recompilation happens on the
    next event after the patterns tuple changes."""

    def test_matched_event_does_not_schedule_timer(self):
        repo = _make_repo("/tmp/ignore-A")
        state = _make_state(debounce_ms=50, repos=[repo])
        state.fs_watch_ignore = ["*.log"]
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        watcher.on_event("/tmp/ignore-A/noisy.log")
        # No debounce thread was started — the ignore filter dropped
        # the event before `_fire_at` got bumped.
        with watcher._lock:
            self.assertIsNone(watcher._timer_thread)
            self.assertEqual(watcher._fire_at, 0.0)

    def test_unmatched_event_still_schedules_timer(self):
        repo = _make_repo("/tmp/ignore-B")
        state = _make_state(debounce_ms=50, repos=[repo])
        state.fs_watch_ignore = ["*.log"]
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        watcher.on_event("/tmp/ignore-B/source.py")
        with watcher._lock:
            thread = watcher._timer_thread
            fire_at = watcher._fire_at
            watcher._stopped = True  # let the debounce thread exit
        self.assertIsNotNone(thread)
        self.assertGreater(fire_at, 0.0)

    def test_patterns_recompile_when_state_list_changes(self):
        # When the user edits the workspace's fs_watch_ignore via the
        # modal, the watcher should recompile on the next event rather
        # than carry stale rules from the previous pattern set.
        repo = _make_repo("/tmp/ignore-C")
        state = _make_state(debounce_ms=50, repos=[repo])
        state.fs_watch_ignore = ["*.log"]
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: None)
        watcher.on_event("/tmp/ignore-C/x.log")  # matches, dropped
        # Swap to a pattern set that doesn't match this path.
        state.fs_watch_ignore = ["*.bak"]
        watcher.on_event("/tmp/ignore-C/x.log")  # no longer matches
        with watcher._lock:
            thread = watcher._timer_thread
            fire_at = watcher._fire_at
            watcher._stopped = True  # let the debounce thread exit
        self.assertIsNotNone(thread)
        self.assertGreater(fire_at, 0.0)


class KickOffActionContentionTests(unittest.TestCase):
    """`kick_off_action` must surface a sidebar warn task and bail
    (without spawning its worker thread) when the target's refresh
    lock is held by another source. The UI gate in main_loop already
    prevents the action menu from opening over a refreshing row, but
    there's a tiny window between menu-open and action-dispatch where
    an fs_watcher debounce can win the race — this test pins that
    fallback so a future regression doesn't reintroduce a concurrent
    git pipeline running on top of an in-flight refresh."""

    def _make_repo_state(self) -> "tuple[Repo, State]":
        repo = _make_repo("/tmp/action-contention-A")
        state = State(repos=[repo], workspace_name="test")
        return repo, state

    def test_action_bails_with_warn_when_repo_lock_held(self):
        from core.workers import kick_off_action

        repo, state = self._make_repo_state()
        # Simulate fs_watcher / Ctrl+R holding the lock at action
        # dispatch time.
        with held_repo_refresh(state, repo):
            kick_off_action(
                state, "fetch",
                target_label="action-A", target_path=repo.path,
                target_repo=repo, target_parent=None)

        # A "skipped" warn task lands; no worker thread runs git.
        snap = state.tasks.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0].status, "warn")
        self.assertIn("skipped", snap[0].label.lower())
        self.assertIn("refresh in progress", snap[0].message.lower())

    def test_action_releases_parent_claim_when_child_contention(self):
        # If the action targets a child ref and the child lock is
        # held (but the parent's wasn't), the parent's claim should
        # be released on the bail path — otherwise the parent's row
        # would stay locked forever waiting for a worker that never
        # ran.
        from core.workers import kick_off_action

        parent = _make_repo("/tmp/action-parent")
        child = _make_child_ref("action-child")
        child.nested_path = Path("/tmp/action-parent/vendor/child")
        parent.children = [child]
        state = State(repos=[parent], workspace_name="test")

        # Hold ONLY the child's lock — parent's is free.
        with held_child_refresh(state, child):
            kick_off_action(
                state, "fetch",
                target_label="child",
                target_path=child.nested_path,
                target_repo=parent, target_parent=parent)

        # Parent's store lock must be free again after child-contention bail.
        assert_repo_refresh_available(self, state, parent)


class FetchOnManualRefreshFlagTests(unittest.TestCase):
    """`fetch_on_manual_refresh` (default off) round-trips through
    `load_config` and applies to State via `apply_workspace_overrides`.
    Workspace-scoped overrides are coerced through the standard schema
    like every other override."""

    def test_default_present_in_config(self):
        cfg = config.Config()
        self.assertFalse(cfg.fetch_on_manual_refresh)

    def test_load_config_picks_up_user_override(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "idlegit.conf"
            conf_path.write_text(
                "[idlegit]\n"
                "fetch_on_manual_refresh = true\n"
            )
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        self.assertTrue(cfg.fetch_on_manual_refresh)

    def test_apply_workspace_overrides_propagates_to_state(self):
        from core.state.workspaces import Workspace
        cfg = config.Config()
        ws = Workspace(
            name="W", folders=[Path("/tmp")],
            overrides={"fetch_on_manual_refresh": True})
        # State defaults to False — apply should flip it.
        state = State(repos=[], workspace_name="W")
        config.apply_workspace_overrides(state, cfg, ws)
        self.assertTrue(state.fetch_on_manual_refresh)


class FsWatchIgnoreConfRoundTripTests(unittest.TestCase):
    """`fs_watch_ignore` is a multi-line per-workspace block in
    idlegit.workspaces — should round-trip through save → load
    without re-ordering or stripping content."""

    def test_save_then_load_preserves_patterns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws_path = Path(td) / "idlegit.workspaces"
            ws = Workspace(
                name="proj",
                folders=[Path(td) / "repo"],
                fs_watch_ignore=["*.log", "!keep.log", "build/**"])
            with mock.patch.object(config, "WORKSPACES_FILE", ws_path):
                config.save_workspaces([ws], active_index=0)
                workspaces, _ = config.load_workspaces()
        self.assertEqual(len(workspaces), 1)
        self.assertEqual(
            workspaces[0].fs_watch_ignore,
            ["*.log", "!keep.log", "build/**"])

    def test_empty_list_is_not_persisted_to_file(self):
        # Workspaces that never set an ignore list shouldn't grow a
        # noisy empty key on save — a fresh file is cleaner.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws_path = Path(td) / "idlegit.workspaces"
            ws = Workspace(name="proj",
                           folders=[Path(td) / "repo"])
            with mock.patch.object(config, "WORKSPACES_FILE", ws_path):
                config.save_workspaces([ws], active_index=0)
                contents = ws_path.read_text()
        self.assertNotIn("fs_watch_ignore", contents)


if __name__ == "__main__":
    unittest.main()
