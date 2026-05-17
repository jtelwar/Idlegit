"""Filesystem-watched per-repo auto-refresh.

Idlegit's main screen normally only re-queries a repo's git state when
the user hits Ctrl+R. That's safe but leaves the display stale whenever
an external edit (editor save, terminal `git checkout`, build artifact)
changes disk between refreshes — at worst the user reads a clean repo as
dirty and reaches for a commit. The commit pipeline itself re-reads disk
at fire time, so the worst case is a "nothing staged" sidebar warn, but
the friction is real.

This module attaches a `watchdog` Observer to each repo's working tree
and runs a debounced `refresh_repo` after the noise settles. The scope
is intentionally narrow:

  - Only `refresh_repo` (the local, no-network refresh — no `gh workflow
    list`, no re-discovery). Full re-discovery still belongs to Ctrl+R.
  - Suppressed while `repo.refreshing=True` (an action is already in
    flight on that row) or `state.in_review=True` (the confirm sub-loop
    owns input). In-review events are queued per repo and drained on
    review exit so the review pane never shifts under the user.
  - `.git/objects/` and `.git/index.lock` churn is filtered out before
    the debounce timer resets, so normal git internals don't trigger a
    refresh.

The Observer is a process-singleton; per-repo `RepoWatcher` instances
register one recursive schedule each. `reconcile()` is idempotent — it
adds watchers for newly-discovered repos, drops watchers for repos that
vanished from `state.repos`, and is a no-op when the config flag is off
(any existing watchers are stopped). Callers wire it into the tail of
`kick_off_inline_refresh` and `switch_workspace`, plus once at startup
after the initial repo load."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import pathspec
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .git_ops import refresh_repo
from .models import Repo, State


# Minimum debounce — anything shorter risks `git status` racing the
# editor's atomic-replace write pattern (write tmp + rename) so we'd see
# the file vanish, refresh, see it reappear, refresh again.
MIN_DEBOUNCE_SECONDS = 0.05


def _is_internal_git_path(path: str) -> bool:
    """True for any path inside a `.git/` directory.

    Why everything, not just `.git/objects/` + `.lock` files: `git status`
    (called by `refresh_repo` itself) refreshes its stat cache by writing
    `.git/index`, which fires an event, resets the debounce timer, and
    triggers another refresh — an infinite loop that manifests as the
    repo rows constantly flashing their spinner. Filtering the entire
    `.git/` tree breaks the loop at the cost of not noticing external
    `git checkout` / `git fetch` until the next working-tree change (or
    a manual Ctrl+R). Acceptable trade — the working-tree path is the
    primary use-case for auto-refresh; branch changes are already a
    deliberate, user-initiated action where reaching for Ctrl+R fits."""
    if not path:
        return False
    # Normalise separators so the substring check works on Windows too.
    norm = path.replace("\\", "/")
    if "/.git/" in norm or norm.endswith("/.git"):
        return True
    return False


def _compile_ignore_spec(
        patterns: List[str]) -> Optional[pathspec.PathSpec]:
    """Compile gitignore patterns into a `PathSpec` for matching, or
    return None for an empty pattern list so callers can short-circuit
    the relative-path computation in the hot path. `GitWildMatch` is
    the parser flavour that matches `git`'s actual semantics (`**`,
    leading `/` anchors, trailing `/` directory-only, `!` negation)."""
    if not patterns:
        return None
    try:
        # `gitignore` is the modern parser name in pathspec 0.12+;
        # `gitwildmatch` was the original alias and is deprecated.
        # Both implement the same `man gitignore` semantics — `**`,
        # leading `/` anchoring, trailing `/` directory-only, `!`
        # negation.
        return pathspec.PathSpec.from_lines("gitignore", patterns)
    except (ValueError, TypeError):
        # Malformed pattern (e.g. trailing escape) — drop the spec
        # entirely rather than half-applying it. The user sees the
        # full list ignored until they fix the bad line.
        return None


def _matches_ignore_spec(spec: Optional[pathspec.PathSpec],
                        repo_path: Path, event_path: str) -> bool:
    """True when `event_path` matches `spec`, with the path normalised
    relative to `repo_path` and using forward-slash separators (the
    canonical form gitignore patterns expect). Returns False on any
    path that can't be made relative (e.g. an event from outside the
    repo — shouldn't happen, but the watcher protocol leaves it open)."""
    if spec is None:
        return False
    try:
        rel = Path(event_path).resolve().relative_to(repo_path.resolve())
    except (ValueError, OSError):
        return False
    return spec.match_file(rel.as_posix())


class _RepoEventHandler(FileSystemEventHandler):
    """Watchdog handler that forwards every non-internal event to its
    parent `RepoWatcher`. One handler per watch — the parent owns the
    debounce timer and the suppression checks."""

    def __init__(self, watcher: "RepoWatcher"):
        super().__init__()
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        # `event.src_path` is set on every event type we care about;
        # moves also have `dest_path` which we don't need to inspect
        # separately — the rename within the watched tree fires both
        # paths and one is enough to trip the debounce timer.
        if _is_internal_git_path(event.src_path):
            return
        self._watcher.on_event(event.src_path)


class RepoWatcher:
    """Per-repo debounce + refresh-fire logic. Held by the singleton
    `WatcherManager`; not constructed directly outside this module.

    `refresh_fn` is injected so tests can substitute a recorder for the
    real `refresh_repo` without touching git or watchdog. Production
    callers pass `core.git_ops.refresh_repo` via `WatcherManager`."""

    def __init__(self, state: State, repo: Repo, manager: "WatcherManager",
                 refresh_fn: Callable[[Repo], None] = refresh_repo):
        self.state = state
        self.repo = repo
        self._manager = manager
        self._refresh_fn = refresh_fn
        # Debounce uses ONE persistent thread per watcher rather than
        # cancel-and-recreate-per-event. The naive `threading.Timer`
        # pattern (spawn a Timer thread per event, cancel previous) is
        # a thread-explosion bomb under heavy fs event load — a build
        # process or `npm install` writing thousands of files per
        # second blows past macOS's per-process thread limit before
        # the debounce window expires, manifesting as kernel-level
        # memory pressure (kernel_task throttling). With the single-
        # thread approach below, `on_event` just bumps `_fire_at`; the
        # already-running thread loops on `time.sleep(remaining)` and
        # only fires when the window truly settles.
        self._lock = threading.Lock()
        self._fire_at: float = 0.0  # monotonic deadline; 0 = no pending
        self._timer_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._watch = None  # watchdog ObservedWatch handle, set in attach()
        # Compiled PathSpec for the active workspace's fs_watch_ignore
        # patterns + the patterns tuple they were compiled from. The
        # tuple is the cache key — when `state.fs_watch_ignore`
        # changes (workspace switch, modal edit) the next event
        # recompiles. Tuple rather than list so the equality check is
        # cheap and we don't rely on identity.
        self._ignore_spec: Optional[pathspec.PathSpec] = None
        self._ignore_patterns: Tuple[str, ...] = ()

    # ---------- watchdog wiring ----------

    def attach(self, observer: Observer) -> None:
        """Register a recursive schedule for this repo's working tree.
        Watching the whole tree is the cheap path — the alternative
        (separate schedules for working tree + `.git/HEAD` + `.git/refs/`
        + `.git/index`) doubles the observer's bookkeeping for no
        practical gain, and the internal-path filter in
        `_is_internal_git_path` already culls the noisy bits before they
        reach the debounce timer."""
        handler = _RepoEventHandler(self)
        self._watch = observer.schedule(
            handler, str(self.repo.path), recursive=True)

    def detach(self, observer: Observer) -> None:
        """Unschedule from `observer` and signal the debounce thread
        to exit on its next iteration. Safe to call when nothing is
        registered (e.g. failed-attach paths) — the unschedule + stop
        flag are both idempotent."""
        if self._watch is not None:
            try:
                observer.unschedule(self._watch)
            except (KeyError, ValueError):
                pass
            self._watch = None
        with self._lock:
            self._stopped = True
            # Wake the sleeping thread (if any) by zeroing fire_at —
            # next loop iteration sees `_stopped` and returns. We
            # don't join() the thread; it's a daemon, exits promptly,
            # and the caller (reconcile/stop_all) shouldn't block.
            self._fire_at = 0.0

    # ---------- debounce + fire ----------

    def on_event(self, event_path: str) -> None:
        """Bump the debounce deadline. Called from watchdog's observer
        thread (one thread per Observer); a single per-watcher debounce
        thread (lazily created here) handles the actual sleep + fire.

        Skipped while `repo.refreshing` is True so events the refresh
        itself triggers (e.g. atomic writes to working-tree files that
        a hook touches) don't keep retriggering us. The `.git/` filter
        in the handler already absorbs the common case (git's own
        index/stat-cache write); this is defence-in-depth.

        Also skipped when `event_path` matches the workspace's
        gitignore-style ignore list — `self._ignore_spec` compiled
        from `state.fs_watch_ignore`, recompiled on demand when the
        patterns tuple changes.

        Queued (not fired) while any action task is running. Multi-
        repo actions like smart-sync mutate the working tree across
        every sibling — firing per-event refreshes during that would
        race the action and thrash the spinner. Instead the main loop
        calls `drain_pending_refreshes()` on the transition where
        `has_running` flips from True to False, so each affected repo
        gets exactly one refresh after the action settles.

        Gate order matters: `tasks.has_running()` is checked BEFORE
        `repo.refreshing` so that a commit pipeline (which both
        creates a task AND holds the refresh lock) routes the event
        to the queue rather than dropping it. Reversed, a user edit
        landing during a commit would be lost entirely — the lock
        would short-circuit the queue path."""
        if self.state.tasks.has_running():
            # Don't even bother starting the debounce — mark this
            # repo for drain when tasks clear. Cheaper than spinning
            # up the debounce thread to discover the same gate at
            # _on_timer time, and avoids running the pathspec match
            # on every event during a sync flood.
            self._manager.mark_pending(self.repo.path)
            return
        if self.repo.refreshing:
            # Another fs_watcher / Ctrl+R refresh is in flight on
            # this repo (no task involved). It'll leave the state
            # consistent — no need to queue or schedule a new fire.
            return
        patterns = tuple(self.state.fs_watch_ignore)
        if patterns != self._ignore_patterns:
            self._ignore_spec = _compile_ignore_spec(list(patterns))
            self._ignore_patterns = patterns
        if _matches_ignore_spec(
                self._ignore_spec, self.repo.path, event_path):
            return
        delay = max(MIN_DEBOUNCE_SECONDS,
                    self.state.auto_refresh_debounce_ms / 1000.0)
        now = time.monotonic()
        with self._lock:
            if self._stopped:
                return
            self._fire_at = now + delay
            if (self._timer_thread is None
                    or not self._timer_thread.is_alive()):
                self._timer_thread = threading.Thread(
                    target=self._debounce_loop, daemon=True)
                self._timer_thread.start()

    def _debounce_loop(self) -> None:
        """Persistent debounce thread. Sleeps until `_fire_at`, then
        loops to re-check — events that arrived during the sleep can
        push the deadline forward, in which case we sleep again. Only
        one of these runs at a time per watcher (gated by
        `_timer_thread.is_alive()` in `on_event`)."""
        while True:
            with self._lock:
                if self._stopped:
                    return
                remaining = self._fire_at - time.monotonic()
                if remaining <= 0:
                    # Window settled — clear the thread reference
                    # inside the lock so the next on_event spins up
                    # a fresh thread for the next burst, then fall
                    # through to fire outside the lock.
                    self._timer_thread = None
                    break
            # Cap each sleep at 60s so a stale `_fire_at` (e.g. the
            # process is asleep / debugger paused) doesn't pin this
            # thread forever — when we wake we re-check and adjust.
            time.sleep(min(remaining, 60.0))
        self._on_timer()

    def _on_timer(self) -> None:
        """Apply the suppression gates and (if clear) fire the refresh.
        Split out from `_debounce_loop` so tests can exercise the gate
        logic synchronously without standing up a real debounce thread.
        Called once per debounce settle in production.

        Two queue-not-fire gates here: in_review (drained after the
        confirm sub-loop exits) and tasks.has_running (drained on the
        has-running → idle transition in the main loop). Either one
        latches the repo into `_pending` so a single refresh fires
        once the blocker clears.

        Both queue gates are checked BEFORE `repo.refreshing` so that
        a commit pipeline holding the refresh lock (which also raises
        `refreshing=True`) doesn't drop the event — its task running
        signal routes the event to the queue and the post-task drain
        catches it."""
        if self.state.in_review:
            self._manager.mark_pending(self.repo.path)
            return
        if self.state.tasks.has_running():
            self._manager.mark_pending(self.repo.path)
            return
        if self.repo.refreshing:
            return
        self.fire_refresh()

    def fire_refresh(self) -> None:
        """Run the per-repo refresh. Acquires `repo.refresh_lock`
        non-blocking — if any other source (Ctrl+R, action menu,
        commit pipeline) is already refreshing this repo, we bail
        without running git calls. The lock-holder will leave the
        Repo in a consistent state; we'd just be doing duplicate
        work + interleaving writes on the same lists.

        Bails when `self._stopped` is set so a `drain_pending` that
        fires after the manager tore the watcher down (workspace
        switch / stop_all race) doesn't run a refresh on a Repo the
        user no longer cares about."""
        if self._stopped:
            return
        if not self.repo.try_acquire_refresh():
            return
        try:
            self._refresh_fn(self.repo)
        finally:
            self.repo.release_refresh()


class WatcherManager:
    """Process-singleton owner of the watchdog `Observer` and the
    per-repo `RepoWatcher` map. `reconcile()` is the only entry point
    callers need — it handles enable/disable, the diff between the
    current repo set and the desired one, and observer lifecycle."""

    def __init__(self):
        self._observer: Optional[Observer] = None
        self._repos: Dict[Path, RepoWatcher] = {}
        self._lock = threading.Lock()
        self._pending: Set[Path] = set()
        # Injected refresh function — production callers leave the
        # default. Tests can swap this before reconcile() to record
        # fires without touching real git.
        self._refresh_fn: Callable[[Repo], None] = refresh_repo

    # ---------- public API ----------

    def reconcile(self, state: State) -> None:
        """Bring the watcher set in line with `state.repos` and the
        `auto_refresh_on_fs_change` flag. Idempotent — safe to call on
        every Ctrl+R refresh + every workspace switch."""
        if not state.auto_refresh_on_fs_change:
            self.stop_all()
            return
        desired: Dict[Path, Repo] = {r.path: r for r in state.repos}
        with self._lock:
            # Drop watchers for paths no longer in state.repos.
            gone = [p for p in self._repos if p not in desired]
            for path in gone:
                watcher = self._repos.pop(path)
                if self._observer is not None:
                    watcher.detach(self._observer)
                self._pending.discard(path)
            if not desired:
                # No repos to watch — tear the observer down so we're
                # not holding a daemon thread for nothing.
                self._stop_observer_locked()
                return
            # Stand the observer up on first use; subsequent reconciles
            # reuse it. Observer.start() is idempotent-safe-ish only
            # before the first start(), so we gate on `_observer is None`.
            if self._observer is None:
                self._observer = Observer()
                self._observer.start()
            # Add watchers for new paths; refresh the stored Repo ref
            # for paths that already have a watcher (the Repo object
            # may have been replaced by kick_off_inline_refresh's
            # discover pass).
            for path, repo in desired.items():
                existing = self._repos.get(path)
                if existing is not None:
                    existing.repo = repo
                    existing.state = state
                    continue
                watcher = RepoWatcher(
                    state, repo, manager=self,
                    refresh_fn=self._refresh_fn)
                try:
                    watcher.attach(self._observer)
                except OSError:
                    # Schedule can fail on macOS when the path is on a
                    # network mount or when the per-process file
                    # descriptor cap is hit. Skip this repo silently —
                    # Ctrl+R still works as a manual fallback.
                    continue
                self._repos[path] = watcher

    def stop_all(self) -> None:
        """Stop the observer and drop every per-repo watcher. Called on
        shutdown and whenever the feature flag flips OFF."""
        with self._lock:
            for watcher in list(self._repos.values()):
                if self._observer is not None:
                    watcher.detach(self._observer)
            self._repos.clear()
            self._pending.clear()
            self._stop_observer_locked()

    def drain_pending(self) -> None:
        """Fire any refreshes that were queued while `state.in_review`
        was True. Call this right after the confirm sub-loop exits. Each
        drained repo runs synchronously on the caller's thread — typical
        is one or two repos per drain, well under a frame budget."""
        with self._lock:
            paths = list(self._pending)
            self._pending.clear()
            watchers = [self._repos[p] for p in paths if p in self._repos]
        for w in watchers:
            w.fire_refresh()

    # ---------- internal ----------

    def mark_pending(self, path: Path) -> None:
        """Record that a debounce-timer fire was suppressed for `path`
        because the review screen was active. Called from `RepoWatcher
        ._on_timer`, which runs on a `threading.Timer` thread."""
        with self._lock:
            if path in self._repos:
                self._pending.add(path)

    def _stop_observer_locked(self) -> None:
        """Stop and join the watchdog Observer. Caller holds `_lock`.
        Joining caps at 1s — the daemon thread will exit with the
        process anyway, this is just a polite cleanup."""
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=1.0)
        except RuntimeError:
            pass
        self._observer = None


# Module-level singleton. Created lazily by `get_manager()` so import
# of this module never spawns a thread (matters for tests that import
# but don't reconcile).
_manager: Optional[WatcherManager] = None
_manager_lock = threading.Lock()


def get_manager() -> WatcherManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WatcherManager()
        return _manager


def reconcile_repo_watchers(state: State) -> None:
    """Public wrapper around `WatcherManager.reconcile()` so callers
    don't have to import the manager class. Safe to call from any
    thread; the manager serialises mutations via its own lock."""
    get_manager().reconcile(state)


def stop_repo_watchers() -> None:
    """Tear every watcher down. Idempotent. Called on shutdown."""
    if _manager is None:
        return
    _manager.stop_all()


def drain_pending_refreshes() -> None:
    """Fire any refreshes suppressed during a just-exited review."""
    if _manager is None:
        return
    _manager.drain_pending()
