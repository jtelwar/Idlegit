"""Runtime thread helpers for job-owned worker fan-out."""

from __future__ import annotations

import threading
from typing import Callable, List, Tuple


ThreadFactory = Callable[
    [Callable[..., None], Tuple[object, ...], bool],
    threading.Thread,
]
JobThreadFactory = Callable[[Callable[[], None], str], threading.Thread]


def create_job_thread(target: Callable[[], None], name: str) -> threading.Thread:
    """Create one named daemon-job thread for the runtime runner."""
    return threading.Thread(target=target, name=name)


def create_daemon_thread(
        target: Callable[[], None],
        name: str,
) -> threading.Thread:
    """Create one named daemon thread for runtime-owned schedulers."""
    return threading.Thread(target=target, name=name, daemon=True)


def create_worker_thread(
        target: Callable[..., None],
        args: Tuple[object, ...],
        daemon: bool,
) -> threading.Thread:
    """Create one worker fan-out thread for a runtime thread group."""
    return threading.Thread(target=target, args=args, daemon=daemon)


class ThreadGroup:
    """Own started worker threads and expose reliable start-count semantics."""

    def __init__(self, thread_factory: ThreadFactory):
        self._thread_factory = thread_factory
        self._threads: List[threading.Thread] = []
        self._errors: List[Exception] = []
        self._lock = threading.Lock()

    @property
    def started_count(self) -> int:
        return len(self._threads)

    def start(
            self,
            target: Callable[..., None],
            args: Tuple[object, ...] = (),
    ) -> None:
        def run_target(*target_args: object) -> None:
            try:
                target(*target_args)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._errors.append(exc)

        thread = self._thread_factory(run_target, args, True)
        thread.start()
        self._threads.append(thread)

    def join_all(self) -> None:
        for thread in self._threads:
            thread.join()
        with self._lock:
            if self._errors:
                raise self._errors[0]
