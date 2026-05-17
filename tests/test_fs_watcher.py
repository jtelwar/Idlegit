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
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config, fs_watcher  # noqa: E402
from core.fs_watcher import (  # noqa: E402
    MIN_DEBOUNCE_SECONDS,
    RepoWatcher,
    WatcherManager,
    _compile_ignore_spec,
    _is_internal_git_path,
    _matches_ignore_spec,
)
from core.models import ChildRef, Repo, State, Workspace  # noqa: E402


def _make_repo(path: str) -> Repo:
    """Bare Repo with just the fields the watcher reads (`path` and
    `refreshing`). Avoids the discover_repos / refresh_repo pipeline so
    these tests don't touch a real working tree."""
    return Repo(rel=".", path=Path(path))


def _make_child_ref(path_suffix: str = "sub") -> ChildRef:
    """Bare ChildRef for mutex testing. Nests a fresh Repo under a
    fake parent path so equality + the per-ChildRef lock work
    independently of any tracked Repo."""
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
    """Suppression rules: skip when an action is already touching the
    row (`repo.refreshing=True`); queue rather than fire when the
    review sub-loop owns input (`state.in_review=True`)."""

    def test_skip_when_repo_already_refreshing(self):
        repo = _make_repo("/tmp/repo-D")
        repo.refreshing = True  # an action holds this row
        state = _make_state(debounce_ms=20, repos=[repo])
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()  # bypass the debounce, hit the gate directly
        self.assertEqual(fires, [])
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
        manager = WatcherManager()
        for r in (repo_a, repo_b):
            w = RepoWatcher(
                state, r, manager=manager,
                refresh_fn=lambda rr: fires.append(rr.path))
            manager._repos[r.path] = w
        manager._pending.add(repo_a.path)
        manager._pending.add(repo_b.path)
        manager.drain_pending()
        self.assertEqual(set(fires), {repo_a.path, repo_b.path})
        self.assertEqual(manager._pending, set())

    def test_queue_when_tasks_running(self):
        # Multi-repo actions like smart-sync mutate the working tree
        # across siblings. Per-event refreshes during the action would
        # race the action and thrash the spinner; instead we queue
        # everything and drain once `tasks.has_running` goes False.
        repo = _make_repo("/tmp/repo-tasks")
        state = _make_state(debounce_ms=20, repos=[repo])
        state.tasks.add("syncing")  # leaves a running task in flight
        fires = []
        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager,
            refresh_fn=lambda r: fires.append(r.path))
        manager._repos[repo.path] = watcher
        watcher._on_timer()
        self.assertEqual(fires, [])
        # The repo path should be queued for the drain that runs when
        # the main loop sees `has_running` transition False.
        self.assertIn(repo.path, manager._pending)

    def test_on_event_queues_even_when_repo_already_refreshing(self):
        # CRITICAL: when a commit pipeline holds the refresh lock, the
        # repo's `refreshing` flag is True AND a task is running. The
        # gate-order audit caught a bug here: an earlier version of
        # `on_event` returned on `if self.repo.refreshing` BEFORE the
        # `tasks.has_running` check, so user edits during a commit
        # were dropped entirely (no post-task drain would fire). The
        # tasks gate now runs first so the event is queued.
        repo = _make_repo("/tmp/repo-gate-order")
        state = _make_state(debounce_ms=20, repos=[repo])
        # Simulate the commit pipeline: lock held + task running +
        # `refreshing` raised. All three are true at once during a
        # real commit.
        self.assertTrue(repo.try_acquire_refresh())
        try:
            state.tasks.add("committing")  # leaves a running task
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
        finally:
            repo.release_refresh()

    def test_on_timer_queues_even_when_repo_already_refreshing(self):
        # Same gate-order check applied to `_on_timer` — exercises
        # the code path where a debounce thread settled BEFORE the
        # commit pipeline acquired the lock. By the time the timer
        # fires, the pipeline holds both `refreshing=True` and a
        # running task. The event must end up in `_pending` (drained
        # at has_running → idle), not dropped.
        repo = _make_repo("/tmp/repo-timer-gate")
        state = _make_state(debounce_ms=20, repos=[repo])
        self.assertTrue(repo.try_acquire_refresh())
        try:
            state.tasks.add("committing")
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: None)
            manager._repos[repo.path] = watcher
            watcher._on_timer()
            self.assertIn(repo.path, manager._pending)
        finally:
            repo.release_refresh()

    def test_on_event_short_circuits_when_tasks_running(self):
        # When tasks are running we shouldn't even start the debounce
        # thread — that's what made multi-repo syncs amplify into the
        # spinner-thrashing pathology before this gate.
        repo = _make_repo("/tmp/repo-fast-bail")
        state = _make_state(debounce_ms=200, repos=[repo])
        state.tasks.add("syncing")
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
    """`fire_refresh` holds `repo.refreshing` for the duration of the
    refresh call so the main loop's anim_running check picks up the
    row's spinner. Idempotent against concurrent fires."""

    def test_sets_refreshing_during_call(self):
        repo = _make_repo("/tmp/repo-H")
        state = _make_state(debounce_ms=20, repos=[repo])
        saw_flag = []

        def recorder(r):
            saw_flag.append(r.refreshing)

        manager = WatcherManager()
        watcher = RepoWatcher(
            state, repo, manager=manager, refresh_fn=recorder)
        watcher.fire_refresh()
        # Refresh should see refreshing=True; flag restored after.
        self.assertEqual(saw_flag, [True])
        self.assertFalse(repo.refreshing)

    def test_fire_skipped_when_lock_held_by_another_source(self):
        # `try_acquire_refresh` is the real mutex now; just setting
        # `refreshing=True` from the outside doesn't claim the lock.
        # Simulate another source (Ctrl+R, action menu) holding the
        # slot by acquiring through the proper API, then assert that
        # fire_refresh bails without running git.
        repo = _make_repo("/tmp/repo-I")
        self.assertTrue(repo.try_acquire_refresh())
        try:
            state = _make_state(debounce_ms=20, repos=[repo])
            fires = []
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: fires.append(r))
            watcher.fire_refresh()
            self.assertEqual(fires, [])
            # The other source's claim is preserved — we did not
            # touch its lock or the visible spinner flag.
            self.assertTrue(repo.refreshing)
        finally:
            repo.release_refresh()

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
        # The lock should never have been acquired — `_stopped` short-
        # circuits before `try_acquire_refresh`.
        self.assertFalse(repo.refreshing)

    def test_refresh_error_restores_flag(self):
        # If the injected refresh raises, `refreshing` must still flip
        # back so a transient git error doesn't leave the row stuck
        # spinning forever.
        repo = _make_repo("/tmp/repo-J")
        state = _make_state(debounce_ms=20, repos=[repo])
        manager = WatcherManager()

        def boom(_r):
            raise RuntimeError("simulated git failure")

        watcher = RepoWatcher(
            state, repo, manager=manager, refresh_fn=boom)
        with self.assertRaises(RuntimeError):
            watcher.fire_refresh()
        self.assertFalse(repo.refreshing)


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


class ConfigRoundTripTests(unittest.TestCase):
    """The two new conf keys (`auto_refresh_on_fs_change`,
    `auto_refresh_debounce_ms`) round-trip cleanly through
    `load_config()` and are clamped to safe ranges."""

    def setUp(self):
        # Stash + clear cached load_warnings; load_config clears them
        # too but explicit cleanup keeps test ordering hygienic.
        self._prev_warnings = list(config.get_load_warnings())

    def test_defaults_present_in_config(self):
        cfg = config.Config()
        self.assertTrue(cfg.auto_refresh_on_fs_change)
        self.assertEqual(cfg.auto_refresh_debounce_ms, 400)

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
            )
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        self.assertFalse(cfg.auto_refresh_on_fs_change)
        self.assertEqual(cfg.auto_refresh_debounce_ms, 750)

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


class RefreshMutexTests(unittest.TestCase):
    """The `Repo.refresh_lock` mutex is the cross-source mutual-
    exclusion primitive: fs_watcher, Ctrl+R, action menu, and the
    commit pipeline all acquire it non-blocking and bail when another
    source already holds it. These tests pin down the contract."""

    def test_try_acquire_sets_refreshing_flag(self):
        # The flag (used by the row spinner) and the lock (used by
        # the mutex check) move together. Acquiring sets True;
        # releasing sets False.
        repo = _make_repo("/tmp/mutex-A")
        self.assertFalse(repo.refreshing)
        self.assertTrue(repo.try_acquire_refresh())
        self.assertTrue(repo.refreshing)
        repo.release_refresh()
        self.assertFalse(repo.refreshing)

    def test_second_acquire_fails_while_held(self):
        # Non-blocking — a second `try_acquire_refresh` while the
        # first claim is live returns False without changing state.
        repo = _make_repo("/tmp/mutex-B")
        self.assertTrue(repo.try_acquire_refresh())
        try:
            self.assertFalse(repo.try_acquire_refresh())
            self.assertTrue(repo.refreshing)
        finally:
            repo.release_refresh()
        # After release, the slot is reclaimable.
        self.assertTrue(repo.try_acquire_refresh())
        repo.release_refresh()

    def test_release_is_idempotent_when_not_held(self):
        # The `release_refresh` in a `finally` block can run on a
        # path that bailed before acquiring. The method swallows the
        # underlying lock's RuntimeError so over-release is safe.
        repo = _make_repo("/tmp/mutex-C")
        repo.release_refresh()  # would raise RuntimeError without guard
        # Flag stays whatever it was (False here) — we did NOT own
        # the lock, so release leaves the flag alone (the actual
        # holder, if any, would expect it to stay True).
        self.assertFalse(repo.refreshing)

    def test_release_does_not_clear_flag_when_lock_already_unlocked(self):
        # The asymmetry `release_refresh` guards: if the lock is not
        # held (caller's `finally` fired on a path that bailed before
        # acquiring), the underlying `Lock.release()` raises
        # RuntimeError and our guard catches it WITHOUT clearing
        # `refreshing`. Without this, a buggy finally could silently
        # flip the spinner off for some out-of-band flag setter (e.g.
        # the legacy sibling-sync fan-out in commit_worker, which
        # still uses raw `refreshing = True/False` for UI display).
        repo = _make_repo("/tmp/mutex-C2")
        repo.refreshing = True  # out-of-band setter
        repo.release_refresh()  # lock unlocked → release raises → caught
        self.assertTrue(repo.refreshing,
                        "release with unlocked lock must not touch flag")
        # NOTE: cross-thread release WHEN THE LOCK IS HELD (by a
        # different source) is NOT protected against — Python's
        # threading.Lock doesn't track owner, so any thread can
        # release. Every refresh-source call site in workers.py /
        # fs_watcher.py only calls `release_refresh` after a matched
        # `try_acquire_refresh` returned True, so the cross-thread
        # misuse case is a hypothetical future-bug guard, not a
        # current correctness requirement.
        # Cleanup.
        repo.refreshing = False

    def test_each_repo_has_independent_lock(self):
        # Locks are per-Repo — holding one doesn't block another.
        # Critical for kick_off_inline_refresh's parallel refresh pool.
        repo_a = _make_repo("/tmp/mutex-D")
        repo_b = _make_repo("/tmp/mutex-E")
        self.assertTrue(repo_a.try_acquire_refresh())
        try:
            self.assertTrue(repo_b.try_acquire_refresh())
            repo_b.release_refresh()
        finally:
            repo_a.release_refresh()

    def test_child_ref_mutex_acquires_and_releases(self):
        # ChildRef has its own independent lock from its parent Repo
        # and from its `.repo` (the canonical). Acquiring on the
        # ChildRef must not block on either of those — they're
        # separate working-tree checkouts with separate git state.
        ref = _make_child_ref("mutex-child-A")
        self.assertFalse(ref.refreshing)
        self.assertTrue(ref.try_acquire_refresh())
        self.assertTrue(ref.refreshing)
        # Parent canonical's lock is still free — independent.
        self.assertTrue(ref.repo.try_acquire_refresh())
        ref.repo.release_refresh()
        ref.release_refresh()
        self.assertFalse(ref.refreshing)

    def test_child_ref_second_acquire_fails_while_held(self):
        ref = _make_child_ref("mutex-child-B")
        self.assertTrue(ref.try_acquire_refresh())
        try:
            self.assertFalse(ref.try_acquire_refresh())
            self.assertTrue(ref.refreshing)
        finally:
            ref.release_refresh()

    def test_child_ref_release_idempotent_when_unlocked(self):
        # Same release-asymmetry contract as Repo: over-release on an
        # unlocked ChildRef must not flip the flag, so a sibling-sync
        # `finally` that bails before acquiring can't strand the
        # in-flight lock holder's spinner.
        ref = _make_child_ref("mutex-child-C")
        ref.refreshing = True  # out-of-band setter (e.g. legacy fan-out)
        ref.release_refresh()
        self.assertTrue(ref.refreshing,
                        "unlocked release must not touch the flag")
        ref.refreshing = False

    def test_fs_watcher_skips_refresh_when_lock_held_elsewhere(self):
        # Simulate Ctrl+R holding the lock; an fs event during that
        # window should not run a second concurrent refresh on the
        # same Repo's lists.
        repo = _make_repo("/tmp/mutex-F")
        repo.try_acquire_refresh()  # pretend Ctrl+R is mid-refresh
        try:
            state = _make_state(debounce_ms=20, repos=[repo])
            fires = []
            manager = WatcherManager()
            watcher = RepoWatcher(
                state, repo, manager=manager,
                refresh_fn=lambda r: fires.append(r.path))
            watcher.fire_refresh()
            self.assertEqual(fires, [])
        finally:
            repo.release_refresh()


class BlockingAcquireTests(unittest.TestCase):
    """`acquire_refresh(timeout=...)` is the blocking variant used by
    commit-pipeline paths (cascade-to-parent submodule bump) that
    MUST get the lock. The non-blocking `try_acquire_refresh` is
    fine for opportunistic refresh sources but caused a regression
    where a brief fs_watcher contention silently dropped the
    propagation. These tests pin the timing-sensitive contract."""

    def test_blocking_acquire_succeeds_when_free(self):
        repo = _make_repo("/tmp/blocking-A")
        self.assertTrue(repo.acquire_refresh(timeout=0.5))
        self.assertTrue(repo.refreshing)
        repo.release_refresh()

    def test_blocking_acquire_waits_for_release(self):
        # Hold the lock in a side thread for 100ms, then release.
        # Blocking acquire should pick it up shortly after.
        repo = _make_repo("/tmp/blocking-B")
        self.assertTrue(repo.try_acquire_refresh())

        def release_after_delay() -> None:
            time.sleep(0.1)
            repo.release_refresh()

        import threading
        threading.Thread(target=release_after_delay, daemon=True).start()
        before = time.monotonic()
        self.assertTrue(repo.acquire_refresh(timeout=2.0))
        waited = time.monotonic() - before
        # We should have waited ~100ms — but timing is fuzzy, so
        # bound the assertion generously.
        self.assertGreaterEqual(waited, 0.05)
        self.assertLess(waited, 1.0)
        repo.release_refresh()

    def test_blocking_acquire_times_out_when_held_forever(self):
        # If the lock is genuinely stuck, blocking acquire must
        # still give up rather than pin the pipeline forever.
        repo = _make_repo("/tmp/blocking-C")
        self.assertTrue(repo.try_acquire_refresh())
        try:
            before = time.monotonic()
            self.assertFalse(repo.acquire_refresh(timeout=0.2))
            waited = time.monotonic() - before
            # We should have waited roughly 200ms.
            self.assertGreaterEqual(waited, 0.15)
        finally:
            repo.release_refresh()

    def test_blocking_acquire_sets_flag_on_success(self):
        # Same flag-set behaviour as try_acquire_refresh on the
        # success path, so the row spinner picks up the claim.
        repo = _make_repo("/tmp/blocking-D")
        self.assertFalse(repo.refreshing)
        self.assertTrue(repo.acquire_refresh(timeout=0.1))
        self.assertTrue(repo.refreshing)
        repo.release_refresh()

    def test_blocking_acquire_leaves_flag_alone_on_timeout(self):
        # On timeout, neither the lock nor the flag move — leaving
        # the current holder's spinner intact.
        repo = _make_repo("/tmp/blocking-E")
        self.assertTrue(repo.try_acquire_refresh())
        # First holder has the flag True.
        self.assertTrue(repo.refreshing)
        try:
            self.assertFalse(repo.acquire_refresh(timeout=0.1))
            # First holder's claim is intact.
            self.assertTrue(repo.refreshing)
        finally:
            repo.release_refresh()

    def test_child_ref_blocking_acquire_independent(self):
        # ChildRef has its own blocking acquire and is independent
        # of its parent Repo's lock.
        ref = _make_child_ref("blocking-child")
        self.assertTrue(ref.acquire_refresh(timeout=0.5))
        self.assertTrue(ref.refreshing)
        # Parent Repo's lock is free.
        self.assertTrue(ref.repo.acquire_refresh(timeout=0.1))
        ref.repo.release_refresh()
        ref.release_refresh()


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
        self.assertTrue(repo.try_acquire_refresh())
        try:
            kick_off_action(
                state, "fetch",
                target_label="action-A", target_path=repo.path,
                target_repo=repo, target_parent=None)
        finally:
            repo.release_refresh()

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
        self.assertTrue(child.try_acquire_refresh())
        try:
            kick_off_action(
                state, "fetch",
                target_label="child",
                target_path=child.nested_path,
                target_repo=parent, target_parent=parent)
        finally:
            child.release_refresh()

        # Parent's lock must be free again (kick_off_action acquired
        # it, then released on the child-contention bail). If the
        # release wasn't paired, `try_acquire_refresh` here would
        # return False and the parent's row would stay stuck.
        self.assertTrue(parent.try_acquire_refresh())
        parent.release_refresh()


class SubmoduleAndFetchFlagsTests(unittest.TestCase):
    """`auto_recurse_submodules` (default on) + `fetch_on_manual_refresh`
    (default off) round-trip through `load_config` and apply to State
    via `apply_workspace_overrides`. Workspace-scoped overrides are
    coerced through the standard schema like every other override."""

    def test_defaults_present_in_config(self):
        cfg = config.Config()
        self.assertTrue(cfg.auto_recurse_submodules)
        self.assertFalse(cfg.fetch_on_manual_refresh)

    def test_load_config_picks_up_user_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "idlegit.conf"
            conf_path.write_text(
                "[idlegit]\n"
                "auto_recurse_submodules = false\n"
                "fetch_on_manual_refresh = true\n"
            )
            with mock.patch.object(config, "CONFIG_FILE", conf_path):
                cfg = config.load_config()
        self.assertFalse(cfg.auto_recurse_submodules)
        self.assertTrue(cfg.fetch_on_manual_refresh)

    def test_apply_workspace_overrides_propagates_to_state(self):
        from core.models import Workspace
        cfg = config.Config()
        ws = Workspace(
            name="W", folders=[Path("/tmp")],
            overrides={
                "auto_recurse_submodules": False,
                "fetch_on_manual_refresh": True,
            })
        # State defaults to True/False — apply should flip both.
        state = State(repos=[], workspace_name="W")
        config.apply_workspace_overrides(state, cfg, ws)
        self.assertFalse(state.auto_recurse_submodules)
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
