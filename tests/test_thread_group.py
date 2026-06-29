"""ThreadGroup helper tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.runtime.threads import ThreadGroup  # noqa: E402


class _ImmediateThread:
    def __init__(self, target, args, daemon):
        self._target = target
        self._args = args
        self.daemon = daemon

    def start(self):
        self._target(*self._args)

    def join(self):
        return


class _FailingThread(_ImmediateThread):
    def start(self):
        raise RuntimeError("thread start failed")


class TestThreadGroup(unittest.TestCase):
    def test_started_count_only_includes_successfully_started_threads(self) -> None:
        calls: list[str] = []
        group = ThreadGroup(lambda target, args, daemon: _ImmediateThread(target, args, daemon))

        group.start(lambda value: calls.append(value), ("one",))
        group.join_all()

        self.assertEqual(group.started_count, 1)
        self.assertEqual(calls, ["one"])

    def test_start_failure_does_not_increment_started_count(self) -> None:
        group = ThreadGroup(lambda target, args, daemon: _FailingThread(target, args, daemon))

        with self.assertRaisesRegex(RuntimeError, "thread start failed"):
            group.start(lambda: None)

        self.assertEqual(group.started_count, 0)

    def test_join_all_raises_worker_exception(self) -> None:
        group = ThreadGroup(lambda target, args, daemon: _ImmediateThread(target, args, daemon))

        group.start(lambda: (_ for _ in ()).throw(RuntimeError("worker failed")))

        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            group.join_all()
        self.assertEqual(group.started_count, 1)


if __name__ == "__main__":
    unittest.main()
