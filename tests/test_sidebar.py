"""Pure sidebar helper tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_state  # noqa: E402
from core.runtime.tasks import (  # noqa: E402
    TASK_AUTO_REMOVE_PROGRESS_SECONDS, Task, Tasks,
)
from ui.sidebar import (  # noqa: E402
    draw_sidebar, _removal_progress, _removal_progress_glyph,
)


class TestSidebarRemovalProgress(unittest.TestCase):
    def test_success_task_progress_starts_after_delay(self) -> None:
        task = Task("done", status="ok", finished_at=100.0)
        self.assertEqual(_removal_progress(task, 6.0, 105.9), 0.0)
        self.assertEqual(_removal_progress(task, 6.0, 106.0), 0.0)
        self.assertAlmostEqual(
            _removal_progress(
                task, 6.0, 106.0 + TASK_AUTO_REMOVE_PROGRESS_SECONDS / 2),
            0.5,
        )
        self.assertEqual(
            _removal_progress(
                task, 6.0, 106.0 + TASK_AUTO_REMOVE_PROGRESS_SECONDS),
            1.0,
        )

    def test_progress_only_applies_to_success_tasks(self) -> None:
        task = Task("failed", status="fail", finished_at=100.0)
        self.assertEqual(_removal_progress(task, 6.0, 200.0), 0.0)

    def test_progress_glyph_fills_to_block(self) -> None:
        self.assertEqual(_removal_progress_glyph(0.0), "")
        self.assertEqual(_removal_progress_glyph(0.01), "◰")
        self.assertEqual(_removal_progress_glyph(0.5), "◲")
        self.assertEqual(_removal_progress_glyph(1.0), "◼")

    def test_removal_animation_does_not_draw_row_wide_bar(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.calls = []

            def getmaxyx(self) -> tuple[int, int]:
                return 20, 80

            def addstr(self, *args) -> None:
                self.calls.append(args)

            def chgat(self, *_args) -> None:
                raise AssertionError("row-wide removal bar should not draw")

        tasks = Tasks()
        task = tasks.add("done")
        tasks.update(task, "ok")
        state = make_state(tasks=tasks)

        with mock.patch("ui.sidebar.curses.color_pair", return_value=0), \
             mock.patch("ui.sidebar.time.monotonic",
                        return_value=(
                            task.finished_at + 6.0
                            + TASK_AUTO_REMOVE_PROGRESS_SECONDS / 2)):
            draw_sidebar(FakeScreen(), state, 30, 40)


if __name__ == "__main__":
    unittest.main()
