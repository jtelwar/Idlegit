"""Tests for the optional tasks.log writer and its Tasks hook.

The logger is best-effort by design — it must never raise into the
worker threads that mutate Tasks. These tests pin down:
  - Path resolution (default vs user-supplied, ~/ expansion, relative
    paths anchored at user_state_dir)
  - Append behaviour on terminal transitions
  - No write on non-terminal (running → pending) updates
  - Idempotency: a second update with the same terminal status doesn't
    re-fire the sink (would double-log otherwise)
  - Rotation: when the line cap is exceeded, oldest lines drop first
  - clear_task_log truncates to empty
  - size + line-count helpers reflect the file state
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config, task_log  # noqa: E402
from core.runtime.tasks import Task, Tasks  # noqa: E402
from core.task_log import (  # noqa: E402
    clear_task_log,
    default_task_log_path,
    format_size,
    format_task_line,
    log_task_event,
    resolve_task_log_path,
    task_log_line_count,
    task_log_size_bytes,
)


class _ResetLoggerCache(unittest.TestCase):
    """Module-level cache (`_line_count`, `_last_path`) bleeds across
    tests because the logger is a singleton. Reset between fixtures so
    each test sees a fresh slate."""

    def setUp(self) -> None:
        task_log._reset_cache_for_tests()
        self.addCleanup(task_log._reset_cache_for_tests)


class TestResolveTaskLogPath(_ResetLoggerCache):
    def test_empty_string_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "USER_STATE_DIR_OVERRIDE",
                                   Path(d)):
                with mock.patch.object(task_log, "user_state_dir",
                                       return_value=Path(d)):
                    self.assertEqual(
                        resolve_task_log_path(""),
                        Path(d) / "tasks.log")

    def test_absolute_path_passes_through(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            abs_path = Path(d) / "custom" / "my.log"
            self.assertEqual(resolve_task_log_path(str(abs_path)), abs_path)

    def test_tilde_expanded(self) -> None:
        # ~/idlegit.log resolves through Path.expanduser
        expanded = resolve_task_log_path("~/idlegit-test.log")
        self.assertTrue(expanded.is_absolute())
        self.assertEqual(expanded.name, "idlegit-test.log")

    def test_relative_path_anchored_at_user_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(task_log, "user_state_dir",
                                   return_value=Path(d)):
                self.assertEqual(
                    resolve_task_log_path("nested/file.log"),
                    Path(d) / "nested" / "file.log")


class TestFormatTaskLine(_ResetLoggerCache):
    def test_includes_status_label_and_message(self) -> None:
        task = Task(label="smart-sync (3)", status="ok",
                    message="all aligned")
        line = format_task_line(task)
        self.assertIn("ok", line)
        self.assertIn("smart-sync (3)", line)
        self.assertIn("all aligned", line)

    def test_no_message_omits_dash(self) -> None:
        task = Task(label="cleanup", status="warn")
        line = format_task_line(task)
        self.assertIn("warn", line)
        self.assertIn("cleanup", line)
        # The em-dash separator only appears when a message exists.
        self.assertNotIn(" — ", line)


class TestLogTaskEvent(_ResetLoggerCache):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="idlegit-tasklog-"))
        self.path = self.tmp / "tasks.log"
        self.addCleanup(lambda: __import__("shutil").rmtree(
            str(self.tmp), ignore_errors=True))

    def test_appends_one_line(self) -> None:
        task = Task(label="t1", status="ok")
        self.assertTrue(log_task_event(self.path, 0, task))
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n"), 1)
        self.assertIn("t1", text)

    def test_creates_parent_directory(self) -> None:
        nested = self.tmp / "deep" / "nested" / "tasks.log"
        task = Task(label="t2", status="fail", message="boom")
        self.assertTrue(log_task_event(nested, 0, task))
        self.assertTrue(nested.exists())

    def test_appends_multiple_lines(self) -> None:
        for i in range(5):
            log_task_event(self.path, 0, Task(label=f"t{i}", status="ok"))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 5)
        for i in range(5):
            self.assertIn(f"t{i}", lines[i])

    def test_rotation_drops_oldest_when_cap_exceeded(self) -> None:
        # Cap of 3 — after 5 appends, only the last 3 survive.
        for i in range(5):
            log_task_event(self.path, 3, Task(label=f"t{i}", status="ok"))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        # The oldest two ("t0", "t1") got dropped; t2..t4 remain in order.
        self.assertIn("t2", lines[0])
        self.assertIn("t3", lines[1])
        self.assertIn("t4", lines[2])

    def test_zero_or_negative_cap_means_unlimited(self) -> None:
        for i in range(8):
            log_task_event(self.path, 0, Task(label=f"t{i}", status="ok"))
        for i in range(8, 12):
            log_task_event(self.path, -1, Task(label=f"t{i}", status="ok"))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 12)


class TestClearAndSize(_ResetLoggerCache):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="idlegit-tasklog-"))
        self.path = self.tmp / "tasks.log"
        self.addCleanup(lambda: __import__("shutil").rmtree(
            str(self.tmp), ignore_errors=True))

    def test_clear_truncates_existing_log(self) -> None:
        for i in range(4):
            log_task_event(self.path, 0, Task(label=f"t{i}", status="ok"))
        self.assertGreater(task_log_size_bytes(self.path), 0)
        self.assertTrue(clear_task_log(self.path))
        self.assertEqual(task_log_size_bytes(self.path), 0)
        self.assertEqual(task_log_line_count(self.path), 0)

    def test_clear_missing_file_is_noop_success(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertTrue(clear_task_log(self.path))

    def test_size_zero_when_missing(self) -> None:
        self.assertEqual(task_log_size_bytes(self.path), 0)

    def test_line_count_matches_writes(self) -> None:
        for i in range(7):
            log_task_event(self.path, 0, Task(label=f"t{i}", status="warn"))
        self.assertEqual(task_log_line_count(self.path), 7)


class TestFormatSize(unittest.TestCase):
    def test_bytes(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(512), "512 B")

    def test_kilobytes(self) -> None:
        self.assertTrue(format_size(2048).endswith("KB"))

    def test_megabytes(self) -> None:
        self.assertTrue(format_size(5 * 1024 * 1024).endswith("MB"))


class TestTasksOnFinishedHook(_ResetLoggerCache):
    """The Tasks-side contract: callback fires once per task on the
    first transition into a terminal status, never inside the lock, and
    failures swallowed (the logger is best-effort). These tests don't
    touch the file — they exercise the wiring."""

    def test_callback_fires_on_terminal_transition(self) -> None:
        tasks = Tasks()
        fired: list = []
        tasks.on_finished = lambda task: fired.append(task)
        t = tasks.add("work")
        tasks.update(t, "ok", "done")
        self.assertEqual(len(fired), 1)
        self.assertIs(fired[0], t)

    def test_callback_does_not_fire_on_running_update(self) -> None:
        tasks = Tasks()
        fired: list = []
        tasks.on_finished = lambda task: fired.append(task)
        t = tasks.add("work")
        tasks.update(t, "running", "still going")
        self.assertEqual(fired, [])

    def test_callback_fires_once_when_already_terminal(self) -> None:
        # Re-updating a task that's already terminal must NOT re-fire
        # the sink — would double-log when workers patch a task's
        # message after the fact.
        tasks = Tasks()
        fired: list = []
        tasks.on_finished = lambda task: fired.append(task)
        t = tasks.add("work")
        tasks.update(t, "ok")
        tasks.update(t, "ok", "extra detail")
        self.assertEqual(len(fired), 1)

    def test_callback_exception_does_not_propagate(self) -> None:
        # A bad sink mustn't crash the worker.
        tasks = Tasks()
        tasks.on_finished = lambda task: (_ for _ in ()).throw(
            RuntimeError("boom"))
        t = tasks.add("work")
        try:
            tasks.update(t, "fail", "x")
        except Exception:
            self.fail("update() must swallow on_finished failures")

    def test_no_callback_when_not_wired(self) -> None:
        # Default state: on_finished=None must remain a no-op path.
        tasks = Tasks()
        t = tasks.add("work")
        tasks.update(t, "ok")
        self.assertEqual(t.status, "ok")


class TestWireUnwireHelpers(_ResetLoggerCache):
    """`wire_task_log` / `unwire_task_log` are the shared install /
    teardown points used by both startup (idlegit.run) and the runtime
    app-menu toggle. The contract: wiring touches the file (so the
    very next 'Open log file' lands on something real), unwiring
    leaves the file alone (historical entries stay readable)."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="idlegit-tasklog-wire-"))
        self.path = self.tmp / "tasks.log"
        self.addCleanup(lambda: __import__("shutil").rmtree(
            str(self.tmp), ignore_errors=True))

    def _state_with_path(self, enabled: bool = True):
        from core.state.app import State
        s = State(repos=[], workspace_name="ws")
        s.task_log_enabled = enabled
        s.task_log_path = self.path
        s.task_log_max_lines = 0
        return s

    def test_wire_installs_callback_and_touches_file(self) -> None:
        from core.task_log import wire_task_log
        s = self._state_with_path()
        self.assertIsNone(s.tasks.on_finished)
        self.assertFalse(self.path.exists())
        wire_task_log(s)
        self.assertIsNotNone(s.tasks.on_finished)
        self.assertTrue(self.path.exists())

    def test_wire_creates_parent_dir(self) -> None:
        from core.task_log import wire_task_log
        nested = self.tmp / "deep" / "tasks.log"
        s = self._state_with_path()
        s.task_log_path = nested
        wire_task_log(s)
        self.assertTrue(nested.parent.is_dir())
        self.assertTrue(nested.exists())

    def test_unwire_removes_callback_but_keeps_file(self) -> None:
        from core.task_log import unwire_task_log, wire_task_log
        s = self._state_with_path()
        wire_task_log(s)
        # Write a line so the file has actual content to preserve.
        log_task_event(self.path, 0, Task(label="t", status="ok"))
        size_before = self.path.stat().st_size
        self.assertGreater(size_before, 0)
        unwire_task_log(s)
        self.assertIsNone(s.tasks.on_finished)
        # File still there, unchanged.
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_size, size_before)

    def test_wire_is_idempotent(self) -> None:
        # Calling wire twice must not produce two callbacks / two
        # write paths — just replace the lambda.
        from core.task_log import wire_task_log
        s = self._state_with_path()
        wire_task_log(s)
        first = s.tasks.on_finished
        wire_task_log(s)
        second = s.tasks.on_finished
        # Different lambda instances (each wire installs a fresh
        # closure), but both end up writing to the same file.
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        t = Task(label="x", status="ok")
        second(t)  # type: ignore[misc]
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\n"), 1)


class TestDefaultTaskLogPath(_ResetLoggerCache):
    def test_under_user_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(task_log, "user_state_dir",
                                   return_value=Path(d)):
                self.assertEqual(
                    default_task_log_path(), Path(d) / "tasks.log")


if __name__ == "__main__":
    unittest.main()
