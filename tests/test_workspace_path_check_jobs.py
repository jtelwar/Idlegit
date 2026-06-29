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

from _helpers import make_state as _state  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.workspaces import (  # noqa: E402
    WorkspaceCreator, WorkspaceDraft, WorkspaceMenu,
)
from core.workers import kick_off_workspace_settings_save  # noqa: E402
from features.workspace_creator.session import tick_creator_checks  # noqa: E402
from features.workspace_menu.session import tick_menu_path_checks  # noqa: E402


def _wait_jobs(state) -> None:
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and all(job.terminal for job in jobs):
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class FailingThread:
    daemon = False

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("thread start failed")


class TestWorkspaceCreatorPathCheckJob(unittest.TestCase):
    def test_creator_path_check_runs_as_read_only_job(self) -> None:
        state = _state()
        with tempfile.TemporaryDirectory() as tmp:
            draft = WorkspaceDraft(path_text=tmp)
            state.workspace_creator = WorkspaceCreator(drafts=[draft])
            with mock.patch("core.workers.discover_repos",
                            return_value=[object(), object()]):
                tick_creator_checks(state)
                _wait_jobs(state)

        self.assertFalse(draft.checking)
        self.assertEqual(draft.repo_count, 2)
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "workspace-path-check")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)

    def test_creator_thread_start_failure_clears_checking(self) -> None:
        state = _state()
        draft = WorkspaceDraft(path_text="/tmp")
        state.workspace_creator = WorkspaceCreator(drafts=[draft])

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            self.assertFalse(tick_creator_checks(state))

        self.assertFalse(draft.checking)
        self.assertEqual(draft.error, "(error: thread start failed)")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)


class TestWorkspaceMenuPathCheckJob(unittest.TestCase):
    def test_menu_path_check_runs_as_read_only_job(self) -> None:
        state = _state()
        with tempfile.TemporaryDirectory() as tmp:
            draft = WorkspaceDraft(path_text=tmp)
            state.workspace_menu = WorkspaceMenu(path_drafts=[draft])
            with mock.patch("core.workers.discover_repos",
                            return_value=[object()]):
                tick_menu_path_checks(state)
                _wait_jobs(state)

        self.assertFalse(draft.checking)
        self.assertEqual(draft.repo_count, 1)
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "workspace-menu-path-check")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)

    def test_menu_thread_start_failure_clears_checking(self) -> None:
        state = _state()
        draft = WorkspaceDraft(path_text="/tmp")
        state.workspace_menu = WorkspaceMenu(path_drafts=[draft])

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            self.assertFalse(tick_menu_path_checks(state))

        self.assertFalse(draft.checking)
        self.assertEqual(draft.error, "(error: thread start failed)")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)


class TestWorkspaceSettingsSaveJob(unittest.TestCase):
    def test_settings_save_runs_as_read_only_job(self) -> None:
        state = _state()

        with mock.patch("core.config.save_workspaces") as save:
            kick_off_workspace_settings_save(
                state,
                label="save workspace settings",
                success_message="saved",
            )
            _wait_jobs(state)

        save.assert_called_once_with(
            state.workspaces,
            state.active_workspace_index,
        )
        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "workspace-settings-save")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "save workspace settings")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "saved")

    def test_settings_save_failure_invokes_failure_callback(self) -> None:
        state = _state()
        failures = []

        with mock.patch("core.config.save_workspaces",
                        side_effect=OSError("disk full")):
            kick_off_workspace_settings_save(
                state,
                on_failure=failures.append,
            )
            _wait_jobs(state)

        self.assertEqual(failures, ["could not write: disk full"])
        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "could not write: disk full")


if __name__ == "__main__":
    unittest.main()
