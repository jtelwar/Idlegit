"""Refresh queue bookkeeping tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.refresh_queue import InlineRefreshQueue  # noqa: E402


class TestInlineRefreshQueue(unittest.TestCase):
    def test_duplicate_target_is_queued(self) -> None:
        queue = InlineRefreshQueue(stale_seconds=30.0)

        self.assertTrue(queue.try_start(0, manual=False, now=1.0, workspace_busy=False))
        self.assertFalse(queue.try_start(0, manual=False, now=2.0, workspace_busy=False))

        self.assertEqual(queue.in_flight, {0})
        self.assertEqual(queue.pending, {0})
        self.assertTrue(queue.complete(0, active_current=True, stale_result=False))
        self.assertFalse(queue.has_in_flight())

    def test_stale_manual_refresh_replaces_in_flight_target_when_idle(self) -> None:
        queue = InlineRefreshQueue(stale_seconds=30.0)

        self.assertTrue(queue.try_start(0, manual=False, now=1.0, workspace_busy=False))
        self.assertTrue(queue.try_start(0, manual=True, now=32.0, workspace_busy=False))

        self.assertEqual(queue.in_flight, {0})
        self.assertEqual(queue.pending, set())
        self.assertEqual(queue.started_at[0], 32.0)

    def test_stale_manual_refresh_still_queues_when_workspace_busy(self) -> None:
        queue = InlineRefreshQueue(stale_seconds=30.0)

        self.assertTrue(queue.try_start(0, manual=False, now=1.0, workspace_busy=False))
        self.assertFalse(queue.try_start(0, manual=True, now=32.0, workspace_busy=True))

        self.assertEqual(queue.in_flight, {0})
        self.assertEqual(queue.pending, {0})
        self.assertEqual(queue.started_at[0], 1.0)

    def test_stale_result_only_requeues_active_target(self) -> None:
        queue = InlineRefreshQueue(stale_seconds=30.0)
        self.assertTrue(queue.try_start(1, manual=False, now=1.0, workspace_busy=False))

        self.assertFalse(queue.complete(1, active_current=False, stale_result=True))

        self.assertFalse(queue.has_in_flight())
        self.assertFalse(queue.has_pending())


if __name__ == "__main__":
    unittest.main()
