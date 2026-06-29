"""Tests for the workflow-run log viewer modal — open/dispatch logic,
key handling, and the action-menu integration that surfaces the new
`View log` item.

Feature fragments are imported directly so tests exercise the ownership
boundary that opens task-detail rows and dispatches browser/cancel jobs."""
from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402

from features.task_detail.actions import (  # noqa: E402
    dispatch_action, open_in_browser,
)
from features.task_detail.session import open_task_action_menu  # noqa: E402
from ui.modals.task_detail import handle_task_action_menu_key  # noqa: E402
from features.task_log_viewer.actions import handle_task_log_viewer_key  # noqa: E402
from features.task_log_viewer.session import open_task_log_viewer  # noqa: E402


def _running_run(s, label: str = "↗ a: Build", *,
                 run_id: int = 42, slug: str = "o/a",
                 workflow: str = "Build", url: str = "",
                 job_id=None):
    """Helper: add a task with the metadata of an active workflow run."""
    t = s.tasks.add(label)
    s.workflow_runs.create_for_task(
        t,
        repo=_make_repo("a"), slug=slug, run_id=run_id,
        workflow_name=workflow, run_url=url, job_id=job_id,
    )
    return t


def _wait_load_finished(state, load_id: str) -> tuple:
    import time
    deadline = 50
    while deadline > 0:
        snapshot = state.view_loads.snapshot(load_id)
        if not snapshot[1]:
            return snapshot
        time.sleep(0.01)
        deadline -= 1
    raise AssertionError("load did not finish")


class TestViewLogMenuItem(unittest.TestCase):
    """`View log` should surface in the task-detail menu exactly when the
    focused task has a workflow run id + slug. Other archetypes get the
    same items they had before this feature shipped."""

    def test_view_log_appears_for_running_run(self) -> None:
        s = _state(_make_repo("a"))
        t = _running_run(s)
        open_task_action_menu(s, t)
        ids = [it.id for it in s.task_action_menu.items]
        self.assertIn("view_log", ids)

    def test_view_log_appears_for_terminal_run(self) -> None:
        s = _state(_make_repo("a"))
        t = _running_run(s)
        s.tasks.update(t, "ok")
        open_task_action_menu(s, t)
        ids = [it.id for it in s.task_action_menu.items]
        self.assertIn("view_log", ids)

    def test_view_log_absent_when_no_run_id(self) -> None:
        s = _state(_make_repo("a"))
        t = s.tasks.add("housekeeping")
        open_task_action_menu(s, t)
        ids = [it.id for it in s.task_action_menu.items]
        self.assertNotIn("view_log", ids)

    def test_view_log_absent_when_no_slug(self) -> None:
        # run_id set but no slug — e.g. a malformed metadata row from
        # a parser regression. Action shouldn't surface since the gh
        # call below requires `--repo <slug>` to succeed.
        s = _state(_make_repo("a"))
        t = s.tasks.add("↗ a: Build")
        s.workflow_runs.create_for_task(t, run_id=42, workflow_name="Build")
        open_task_action_menu(s, t)
        ids = [it.id for it in s.task_action_menu.items]
        self.assertNotIn("view_log", ids)


class TestTaskDetailActionJobs(unittest.TestCase):
    def test_cancel_run_dispatch_uses_read_only_job(self) -> None:
        s = _state(_make_repo("a"))
        t = _running_run(s)
        open_task_action_menu(s, t)

        with mock.patch("features.task_detail.actions.cancel_run",
                        return_value=(True, "cancelled")):
            dispatch_action(s, "cancel_run")

        deadline = 100
        while deadline > 0:
            import time
            jobs = s.job_registry.snapshot()
            if jobs and jobs[0].terminal:
                break
            time.sleep(0.01)
            deadline -= 1

        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "workflow-cancel")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "cancelled")

    def test_cancel_run_thread_start_failure_adds_failed_task(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        s = _state(_make_repo("a"))
        t = _running_run(s)
        open_task_action_menu(s, t)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            dispatch_action(s, "cancel_run")

        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")

    def test_cancel_pipeline_requests_owning_job_cancel(self) -> None:
        s = _state(_make_repo("a"))
        job = s.job_registry.start(JobSpec(
            kind="commit-batch",
            label="commit workers",
            local_mutation=True,
        ))
        task = s.tasks.add("a: working")
        s.job_registry.link_task(job, task)

        open_task_action_menu(s, task)
        ids = [it.id for it in s.task_action_menu.items]
        self.assertIn("cancel_pipeline", ids)

        dispatch_action(s, "cancel_pipeline")

        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(job.terminal)
        self.assertIsNone(s.task_action_menu)

    def test_terminal_cancel_job_hides_cancel_pipeline_action(self) -> None:
        s = _state(_make_repo("a"))
        job = s.job_registry.start(JobSpec(
            kind="commit-batch",
            label="commit workers",
            local_mutation=True,
        ))
        s.job_registry.finish(job, JobStatus.OK)
        task = s.tasks.add("a: working")
        s.job_registry.link_task(job, task)

        open_task_action_menu(s, task)

        ids = [it.id for it in s.task_action_menu.items]
        self.assertNotIn("cancel_pipeline", ids)

    def test_open_browser_uses_read_only_job(self) -> None:
        s = _state(_make_repo("a"))
        with mock.patch("features.task_detail.actions.webbrowser.open",
                        return_value=True):
            open_in_browser(s, "https://example.test/run")

        deadline = 100
        while deadline > 0:
            import time
            jobs = s.job_registry.snapshot()
            if jobs and jobs[0].terminal:
                break
            time.sleep(0.01)
            deadline -= 1

        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "open-browser")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "opened")

    def test_open_browser_warning_marks_job_warning(self) -> None:
        s = _state(_make_repo("a"))
        with mock.patch("features.task_detail.actions.webbrowser.open",
                        return_value=False):
            open_in_browser(s, "https://example.test/run")

        deadline = 100
        while deadline > 0:
            import time
            jobs = s.job_registry.snapshot()
            if jobs and jobs[0].terminal:
                break
            time.sleep(0.01)
            deadline -= 1

        jobs = s.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.WARN)
        self.assertEqual(jobs[0].message, "no browser available")
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.status, "warn")
        self.assertEqual(task.message, "no browser available")


class TestOpenTaskLogViewer(unittest.TestCase):
    """`open_task_log_viewer` installs a TaskLogViewer on State and
    spawns a loader thread (mocked here). Tests cover the field
    population, the no-op short-circuits, and the failed-run path
    that flips `only_failed` to True."""

    def setUp(self) -> None:
        # Patch the loader so the daemon thread doesn't actually call
        # `gh` while the unit test runs.
        self._fetch_patcher = mock.patch(
            "core.workers.fetch_run_log",
            return_value=(True, ["log line 1", "log line 2"], ""))
        self._fetch = self._fetch_patcher.start()
        self.addCleanup(self._fetch_patcher.stop)

    def test_open_populates_viewer_fields(self) -> None:
        s = _state(_make_repo("a"))
        t = _running_run(s, run_id=99, slug="o/a",
                         workflow="Build", job_id=7)
        open_task_log_viewer(s, t)
        viewer = s.task_log_viewer
        self.assertIsNotNone(viewer)
        self.assertIs(viewer.task, t)
        self.assertEqual(viewer.run_id, 99)
        self.assertEqual(viewer.slug, "o/a")
        self.assertEqual(viewer.workflow_name, "Build")
        self.assertEqual(viewer.job_id, 7)
        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "task-log-load")
        self.assertFalse(jobs[0].spec.local_mutation)

    def test_open_uses_failed_filter_when_task_failed(self) -> None:
        s = _state(_make_repo("a"))
        t = _running_run(s)
        s.tasks.update(t, "fail")
        open_task_log_viewer(s, t)
        self.assertTrue(s.task_log_viewer.only_failed)

    def test_open_uses_full_log_for_non_failed_tasks(self) -> None:
        s = _state(_make_repo("a"))
        for status in ("running", "ok", "warn"):
            with self.subTest(status=status):
                t = _running_run(s)
                if status != "running":
                    s.tasks.update(t, status)
                s.task_log_viewer = None
                open_task_log_viewer(s, t)
                self.assertFalse(s.task_log_viewer.only_failed)

    def test_open_no_op_without_run_id(self) -> None:
        s = _state(_make_repo("a"))
        t = s.tasks.add("housekeeping")
        open_task_log_viewer(s, t)
        self.assertIsNone(s.task_log_viewer)

    def test_dispatch_view_log_opens_viewer(self) -> None:
        # End-to-end: open the action menu, dispatch the `view_log`
        # action id, the viewer slot is now populated.
        s = _state(_make_repo("a"))
        t = _running_run(s)
        open_task_action_menu(s, t)
        for i, item in enumerate(s.task_action_menu.items):
            if item.id == "view_log":
                s.task_action_menu.selected = i
                break
        handle_task_action_menu_key(s, curses.KEY_ENTER)
        self.assertIsNotNone(s.task_log_viewer)
        # Detail modal stays open so dismissing the viewer reveals it.
        self.assertIsNotNone(s.task_action_menu)

    def test_thread_start_failure_clears_loading(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        s = _state(_make_repo("a"))
        t = _running_run(s)
        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            open_task_log_viewer(s, t)

        viewer = s.task_log_viewer
        self.assertIsNotNone(viewer)
        assert viewer is not None
        lines, loading, error = s.view_loads.snapshot(viewer.load_id)
        self.assertFalse(loading)
        self.assertEqual(lines, [])
        self.assertEqual(error, "thread start failed")
        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)


class TestTaskLogViewerKeyHandler(unittest.TestCase):
    """Keystrokes on an open viewer: close gestures clear the slot;
    scroll keys move `viewer.scroll`; End jumps past the last line so
    the draw-time clamp lands it at the bottom."""

    def setUp(self) -> None:
        self._fetch_patcher = mock.patch(
            "core.workers.fetch_run_log",
            return_value=(True, [f"line {i}" for i in range(50)], ""))
        self._fetch_patcher.start()
        self.addCleanup(self._fetch_patcher.stop)
        self.s = _state(_make_repo("a"))
        t = _running_run(self.s)
        open_task_log_viewer(self.s, t)
        self.s.view_loads.finish(
            self.s.task_log_viewer.load_id,
            [f"line {i}" for i in range(50)],
        )

    def test_esc_closes_viewer(self) -> None:
        handle_task_log_viewer_key(self.s, 27)
        self.assertIsNone(self.s.task_log_viewer)

    def test_enter_closes_viewer(self) -> None:
        handle_task_log_viewer_key(self.s, curses.KEY_ENTER)
        self.assertIsNone(self.s.task_log_viewer)

    def test_tab_closes_viewer(self) -> None:
        handle_task_log_viewer_key(self.s, 9)
        self.assertIsNone(self.s.task_log_viewer)

    def test_close_removes_view_load_record(self) -> None:
        viewer = self.s.task_log_viewer
        handle_task_log_viewer_key(self.s, 27)
        self.assertEqual(self.s.view_loads.snapshot(viewer.load_id),
                         ([], True, ""))

    def test_down_arrow_scrolls(self) -> None:
        handle_task_log_viewer_key(self.s, curses.KEY_DOWN)
        self.assertEqual(self.s.task_log_viewer.scroll, 1)

    def test_up_arrow_clamps_at_zero(self) -> None:
        handle_task_log_viewer_key(self.s, curses.KEY_UP)
        self.assertEqual(self.s.task_log_viewer.scroll, 0)

    def test_pgdn_jumps_by_ten(self) -> None:
        handle_task_log_viewer_key(self.s, curses.KEY_NPAGE)
        self.assertEqual(self.s.task_log_viewer.scroll, 10)

    def test_pgup_clamps_at_zero(self) -> None:
        self.s.task_log_viewer.scroll = 3
        handle_task_log_viewer_key(self.s, curses.KEY_PPAGE)
        self.assertEqual(self.s.task_log_viewer.scroll, 0)

    def test_home_jumps_to_start(self) -> None:
        self.s.task_log_viewer.scroll = 25
        handle_task_log_viewer_key(self.s, curses.KEY_HOME)
        self.assertEqual(self.s.task_log_viewer.scroll, 0)

    def test_end_jumps_past_last_line(self) -> None:
        # End sets scroll to len(lines) — the draw layer clamps it to
        # the visible window. 50 lines → scroll lands at 50.
        handle_task_log_viewer_key(self.s, curses.KEY_END)
        self.assertEqual(self.s.task_log_viewer.scroll, 50)


class TestLoaderErrorPath(unittest.TestCase):
    """When `gh` fails, the viewer's `error` field should be populated
    and `lines` left empty so the draw layer surfaces the failure."""

    def test_fetch_failure_lands_error(self) -> None:
        with mock.patch(
                "core.workers.fetch_run_log",
                return_value=(False, [], "gh CLI not on PATH")):
            s = _state(_make_repo("a"))
            t = _running_run(s)
            open_task_log_viewer(s, t)
            viewer = s.task_log_viewer
            lines, loading, error = _wait_load_finished(s, viewer.load_id)
        self.assertFalse(loading)
        self.assertEqual(error, "gh CLI not on PATH")
        self.assertEqual(lines, [])

    def test_empty_log_lands_placeholder_text(self) -> None:
        with mock.patch(
                "core.workers.fetch_run_log",
                return_value=(True, [], "")):
            s = _state(_make_repo("a"))
            t = _running_run(s)
            open_task_log_viewer(s, t)
            viewer = s.task_log_viewer
            lines, loading, error = _wait_load_finished(s, viewer.load_id)
        self.assertFalse(loading)
        self.assertEqual(error, "")
        self.assertEqual(lines, ["(no log output yet)"])


if __name__ == "__main__":
    unittest.main()
