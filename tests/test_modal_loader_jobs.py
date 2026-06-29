from __future__ import annotations

import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_state as _state  # noqa: E402
from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.action_target import TargetState  # noqa: E402
from core.state.app_menu import AppMenu  # noqa: E402
from core.state.action_menu import ActionMenu, CommitEntry, FileEntry  # noqa: E402
from core.state.review import ReviewBlock  # noqa: E402
from core.ssh import SshToolsStatus  # noqa: E402
from core.workers import (  # noqa: E402
    kick_off_app_menu_status_refresh,
    kick_off_check_for_updates,
    kick_off_open_task_log,
    kick_off_review_files_load,
)
from features.action_menu.loaders import (  # noqa: E402
    kick_off_action_menu_commits_page,
)
from features.action_menu.projection import (  # noqa: E402
    enter_submenu_for,
)
from features.action_menu.session import (  # noqa: E402
    close_action_menu,
    open_action_menu,
)
from features.diff_viewer.session import open_diff_viewer  # noqa: E402


def _wait_jobs(state, expected: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if len(jobs) == expected and all(job.terminal for job in jobs):
            return
        time.sleep(0.01)
    raise AssertionError("jobs did not finish")


def _loading(state, load_id: str) -> bool:
    _lines, loading, _error = state.view_loads.snapshot(load_id)
    return loading


class TestAppMenuTaskLogOpenJob(unittest.TestCase):
    def test_open_task_log_runs_as_read_only_job(self) -> None:
        state = _state()
        with tempfile.TemporaryDirectory() as tmp:
            state.task_log_path = Path(tmp) / "tasks.log"
            with mock.patch("core.task_log.open_task_log",
                            return_value=True):
                kick_off_open_task_log(state)
                _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "open-task-log")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "opened")


class TestAppMenuStatusJob(unittest.TestCase):
    def test_status_refresh_runs_as_read_only_job(self) -> None:
        state = _state()
        menu = AppMenu()
        state.app_menu = menu
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.log"
            path.write_text("one\ntwo\n")
            state.task_log_path = path
            with (
                mock.patch(
                    "core.ssh.ssh_tools_status",
                    return_value=SshToolsStatus(
                        has_ssh_agent=True,
                        has_ssh_add=True,
                        has_ssh_keygen=True,
                        agent_running=True,
                        keys_loaded=2,
                    ),
                ),
                mock.patch("core.ssh.agent_status_label",
                           return_value="running"),
                mock.patch("core.ssh.keys_loaded_label",
                           return_value="2 loaded"),
            ):
                kick_off_app_menu_status_refresh(state, menu)
                _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "app-menu-status")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertFalse(menu.ssh_status_checking)
        self.assertFalse(menu.task_log_checking)
        self.assertEqual(menu.ssh_status, "running")
        self.assertEqual(menu.ssh_keys, "2 loaded")
        self.assertIn("2 lines", menu.task_log_size)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.label, "load app menu status")
        self.assertEqual(task.status, "ok")


class TestUpdateCheckJob(unittest.TestCase):
    def test_update_check_runs_as_read_only_job(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"tag_name":"v9.9.9"}'

        state = _state()
        menu = AppMenu()
        with mock.patch("urllib.request.urlopen", return_value=Response()):
            kick_off_check_for_updates(state, menu)
            _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "update-check")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertEqual(menu.update_check, "done")
        self.assertEqual(menu.latest_version, "v9.9.9")

    def test_update_check_network_error_marks_job_warning(self) -> None:
        state = _state()
        menu = AppMenu()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            kick_off_check_for_updates(state, menu)
            _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.WARN)
        self.assertEqual(menu.update_check, "failed")
        self.assertIn("network:", menu.update_check_error)

    def test_update_check_thread_failure_clears_checking_state(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        menu = AppMenu()
        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_check_for_updates(state, menu)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        self.assertEqual(menu.update_check, "failed")
        self.assertEqual(menu.update_check_error, "thread start failed")


class TestDiffViewerLoaderJobs(unittest.TestCase):
    def test_open_diff_viewer_uses_read_only_jobs_for_tabs(self) -> None:
        state = _state()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("hello\n", encoding="utf-8")
            with mock.patch("core.workers.query_file_log",
                            return_value=["abc123 2026-06-21 test"]):
                open_diff_viewer(
                    state, root, "repo", "file.txt", untracked=True)
                _wait_jobs(state, 3)

        viewer = state.diff_viewer
        self.assertIsNotNone(viewer)
        assert viewer is not None
        for load_id in (
                viewer.diff_load_id,
                viewer.log_load_id,
                viewer.blame_load_id):
            _lines, loading, _error = state.view_loads.snapshot(load_id)
            self.assertFalse(loading)
        jobs = state.job_registry.snapshot()
        self.assertEqual(
            [job.spec.kind for job in jobs],
            ["diff-viewer-diff", "diff-viewer-log", "diff-viewer-blame"],
        )
        self.assertTrue(all(not job.spec.local_mutation for job in jobs))
        self.assertTrue(all(job.status == JobStatus.OK for job in jobs))

    def test_thread_start_failure_clears_all_loading_flags(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        with mock.patch("core.runtime.threads.threading.Thread",
                        FailingThread):
            open_diff_viewer(
                state, Path("/tmp"), "repo", "file.txt", untracked=True)

        viewer = state.diff_viewer
        self.assertIsNotNone(viewer)
        assert viewer is not None
        for load_id in (
                viewer.diff_load_id,
                viewer.log_load_id,
                viewer.blame_load_id):
            lines, loading, error = state.view_loads.snapshot(load_id)
            self.assertFalse(loading)
            self.assertEqual(lines, ["thread start failed"])
            self.assertEqual(error, "thread start failed")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job.status == JobStatus.FAIL for job in jobs))


class TestActionMenuLoaderJobs(unittest.TestCase):
    def test_open_action_menu_uses_read_only_jobs_for_loaders(self) -> None:
        repo = _make_repo("a", branch="main")
        state = _state(repo)
        target_state = TargetState(
            branch="main",
            upstream=None,
            ahead=0,
            behind=0,
            has_origin=False,
            merging=False,
            dirty=False,
            recent_commits=[],
        )
        with (
            mock.patch("features.action_menu.loaders.list_stashes",
                       return_value=[]),
            mock.patch("features.action_menu.loaders.list_remotes",
                       return_value=[]),
            mock.patch("features.action_menu.loaders.query_target_state",
                       return_value=target_state),
            mock.patch("features.action_menu.loaders.query_working_tree",
                       return_value=[]),
            mock.patch("features.action_menu.loaders.load_commits",
                       return_value=([], True)),
        ):
            open_action_menu(state)
            _wait_jobs(state, 4)

        jobs = state.job_registry.snapshot()
        self.assertEqual(
            [job.spec.kind for job in jobs],
            [
                "action-menu-state-load",
                "action-menu-inventory-load",
                "action-menu-tree-load",
                "action-menu-commits-load",
            ],
        )
        self.assertTrue(all(not job.spec.local_mutation for job in jobs))
        self.assertTrue(all(job.status == JobStatus.OK for job in jobs))
        self.assertFalse(_loading(state, state.action_menu.state_load_id))
        self.assertFalse(_loading(state, state.action_menu.inventory_load_id))
        self.assertFalse(_loading(state, state.action_menu.tree_load_id))
        self.assertFalse(_loading(state, state.action_menu.commits_load_id))
        self.assertEqual(state.action_menu.items[-2].label, "stashes (0)")
        self.assertEqual(state.action_menu.items[-1].label, "remotes (0)")

    def test_open_action_menu_does_not_load_inventory_synchronously(self) -> None:
        import features.action_menu.loaders as action_menu_loaders

        repo = _make_repo("a", branch="main")
        state = _state(repo)
        submitted = []

        def fake_submit(registry, spec, target, *, thread_factory=None):
            job = registry.start(spec)
            submitted.append((spec, target))
            return job, object()

        with (
            mock.patch.object(action_menu_loaders, "submit_job",
                              side_effect=fake_submit),
            mock.patch.object(action_menu_loaders, "list_stashes") as stashes,
            mock.patch.object(action_menu_loaders, "list_remotes") as remotes,
        ):
            open_action_menu(state)

        stashes.assert_not_called()
        remotes.assert_not_called()
        self.assertEqual(
            [spec.kind for spec, _target in submitted],
            [
                "action-menu-state-load",
                "action-menu-inventory-load",
                "action-menu-tree-load",
                "action-menu-commits-load",
            ],
        )
        self.assertTrue(_loading(state, state.action_menu.inventory_load_id))
        self.assertEqual(state.action_menu.items[-2].label, "stashes (...)")
        self.assertEqual(state.action_menu.items[-1].label, "remotes (...)")

    def test_open_action_menu_thread_failure_clears_loading_flags(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state(_make_repo("a", branch="main"))
        with mock.patch("core.runtime.threads.threading.Thread",
                        FailingThread):
            open_action_menu(state)

        self.assertFalse(_loading(state, state.action_menu.state_load_id))
        self.assertFalse(_loading(state, state.action_menu.inventory_load_id))
        self.assertFalse(_loading(state, state.action_menu.tree_load_id))
        self.assertFalse(_loading(state, state.action_menu.commits_load_id))
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(job.status == JobStatus.FAIL for job in jobs))
        self.assertEqual(state.action_menu.items[-2].label, "stashes (0)")
        self.assertEqual(state.action_menu.items[-1].label, "remotes (0)")

    def test_close_action_menu_cancels_loader_records(self) -> None:
        import features.action_menu.loaders as action_menu_loaders

        state = _state(_make_repo("a", branch="main"))

        def fake_submit(registry, spec, target, *, thread_factory=None):
            job = registry.start(spec)
            return job, object()

        with mock.patch.object(action_menu_loaders, "submit_job",
                               side_effect=fake_submit):
            open_action_menu(state)

        menu = state.action_menu
        self.assertIsNotNone(menu)
        assert menu is not None
        load_ids = [
            menu.state_load_id,
            menu.inventory_load_id,
            menu.tree_load_id,
            menu.commits_load_id,
        ]

        close_action_menu(state)

        self.assertIsNone(state.action_menu)
        self.assertTrue(all(state.view_loads.is_cancelled(load_id)
                            for load_id in load_ids))

    def test_submenu_entry_uses_cached_inventory_without_git(self) -> None:
        import features.action_menu.loaders as action_menu_loaders

        state = _state(_make_repo("a", branch="main"))
        submitted = []

        def fake_submit(registry, spec, target, *, thread_factory=None):
            job = registry.start(spec)
            submitted.append((spec, target))
            return job, object()

        with mock.patch.object(action_menu_loaders, "submit_job",
                               side_effect=fake_submit):
            open_action_menu(state)
        menu = state.action_menu
        menu.stashes = [("stash@{0}", "On main: WIP")]
        menu.stash_count = 1
        stash_item = next(item for item in menu.items
                          if item.id == "stashes_submenu")

        with (
            mock.patch.object(action_menu_loaders, "list_stashes") as stashes,
            mock.patch.object(action_menu_loaders, "list_remotes") as remotes,
        ):
            enter_submenu_for(menu, stash_item)

        stashes.assert_not_called()
        remotes.assert_not_called()
        self.assertEqual(menu.submenu_stack[-1].name, "stashes")
        self.assertTrue(any(item.id == "stash:stash@{0}"
                            for item in menu.submenu_stack[-1].items))

    def test_lazy_commits_page_uses_read_only_job(self) -> None:
        state = _state()
        menu = ActionMenu(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            commits_full=[
                CommitEntry(
                    sha="abc123",
                    subject="first",
                    relative="now",
                ),
            ],
            commits_exhausted=False,
        )
        next_page = [
            CommitEntry(
                sha="def456",
                subject="second",
                relative="now",
            ),
        ]
        with mock.patch("features.action_menu.loaders.load_commits",
                        return_value=(next_page, True)):
            kick_off_action_menu_commits_page(state, menu)
            _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "action-menu-commits-page")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertEqual([c.sha for c in menu.commits_full],
                         ["abc123", "def456"])
        self.assertTrue(menu.commits_exhausted)
        self.assertFalse(_loading(state, menu.commits_load_id))


class TestReviewFilesLoaderJobs(unittest.TestCase):
    class ParkedThread:
        target = None

        def __init__(self, target, name) -> None:
            self.target = target
            self.name = name
            self.daemon = False
            TestReviewFilesLoaderJobs.ParkedThread.target = target

        def start(self) -> None:
            return None

    def test_review_files_load_runs_as_read_only_job(self) -> None:
        state = _state()
        block = ReviewBlock(
            label="repo",
            branch="main",
            target_path=Path("/tmp/repo"),
            draft_id="repo:/tmp/repo",
            auto_stage=False,
        )
        state.review_drafts.create(block.draft_id)
        files = [
            FileEntry(path="staged.txt", x="M", y=" "),
            FileEntry(path="new.txt", untracked=True),
        ]
        with mock.patch("core.workers.query_working_tree", return_value=files):
            kick_off_review_files_load(state, [block])
            _wait_jobs(state, 1)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "review-files-load")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        draft = state.review_drafts.get(block.draft_id)
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.files, files)
        self.assertEqual(draft.staged_paths, {
            "staged.txt": True,
            "new.txt": False,
        })
        self.assertFalse(draft.files_loading)
        self.assertFalse(_loading(state, draft.files_load_id))

    def test_review_files_cancel_prevents_late_publish(self) -> None:
        state = _state()
        block = ReviewBlock(
            label="repo",
            branch="main",
            target_path=Path("/tmp/repo"),
            draft_id="repo:/tmp/repo",
        )
        state.review_drafts.create(block.draft_id)
        with mock.patch("core.runtime.threads.threading.Thread", self.ParkedThread), \
                mock.patch("core.workers.query_working_tree") as query:
            kick_off_review_files_load(state, [block])

        draft = state.review_drafts.get_or_create(block.draft_id)
        state.view_loads.remove_many([draft.files_load_id])
        assert self.ParkedThread.target is not None
        self.ParkedThread.target()

        query.assert_not_called()
        self.assertTrue(state.view_loads.is_cancelled(draft.files_load_id))
        self.assertEqual(draft.files, [])

    def test_review_files_thread_failure_clears_loading(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        blocks = [
            ReviewBlock(
                label="repo",
                branch="main",
                target_path=Path("/tmp/repo"),
                draft_id="repo:/tmp/repo",
            ),
            ReviewBlock(
                label="repo2",
                branch="main",
                target_path=Path("/tmp/repo2"),
                draft_id="repo:/tmp/repo2",
            ),
        ]
        for block in blocks:
            state.review_drafts.create(block.draft_id)
        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_review_files_load(state, blocks)

        self.assertTrue(all(
            not state.review_drafts.get_or_create(block.draft_id).files_loading
            for block in blocks))
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job.status == JobStatus.FAIL for job in jobs))


if __name__ == "__main__":
    unittest.main()
