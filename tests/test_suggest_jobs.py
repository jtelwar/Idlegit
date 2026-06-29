"""Suggestion worker job lifecycle tests."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.review import ReviewBlock  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.workers import (  # noqa: E402
    kick_off_bulk_suggest,
    kick_off_review_suggest,
    kick_off_suggest_for,
)


def _wait_for_job_terminal(state, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and jobs[0].terminal:
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestSuggestionJobs(unittest.TestCase):
    def test_repo_suggestion_finishes_read_only_job(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch(
                "core.workers.suggest_commit_message",
                return_value="suggested message"):
            kick_off_suggest_for(state, repo)

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_suggesting(repo))
        self.assertEqual(state.store.row_message(repo), "suggested message")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "suggest")
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.status, JobStatus.OK)
        self.assertFalse(state.job_registry.has_active_local_mutation())

    def test_store_busy_repo_does_not_start_suggestion(self) -> None:
        repo = _make_repo("r")
        repo.staged = [("M", "x")]
        state = _state(repo)
        state.store.set_repo_busy(repo, True)

        with mock.patch("core.workers.suggest_commit_message") as suggest:
            kick_off_suggest_for(state, repo)

        suggest.assert_not_called()
        self.assertEqual(state.job_registry.snapshot(), [])
        self.assertFalse(state.store.repo_suggesting(repo))

    def test_store_busy_child_does_not_start_suggestion(self) -> None:
        parent = _make_repo("parent")
        child_repo = _make_repo("child")
        child = ChildRef(
            repo=child_repo,
            nested_path=Path("/tmp/parent/child"),
            kind="submodule",
            dirty=True,
        )
        parent.children = [child]
        state = _state(parent, child_repo)
        state.store.set_child_busy(child, True)

        with mock.patch("core.workers.suggest_commit_message_at") as suggest:
            kick_off_suggest_for(state, child)

        suggest.assert_not_called()
        self.assertEqual(state.job_registry.snapshot(), [])
        self.assertFalse(state.store.child_suggesting(child))

    def test_late_store_busy_repo_does_not_receive_suggestion_result(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        def suggest_side_effect(*_args, **_kwargs):
            state.store.set_repo_busy(repo, True)
            return "suggested message"

        with mock.patch(
                "core.workers.suggest_commit_message",
                side_effect=suggest_side_effect):
            kick_off_suggest_for(state, repo)

        _wait_for_job_terminal(state)
        self.assertEqual(state.store.row_message(repo), "")
        self.assertFalse(state.store.repo_suggesting(repo))

    def test_bulk_suggest_skips_store_busy_rows(self) -> None:
        repo = _make_repo("r")
        repo.staged = [("M", "repo.txt")]
        parent = _make_repo("parent")
        child_repo = _make_repo("child")
        child = ChildRef(
            repo=child_repo,
            nested_path=Path("/tmp/parent/child"),
            kind="submodule",
            dirty=True,
        )
        parent.children = [child]
        state = _state(repo, parent, child_repo)
        state.store.set_repo_busy(repo, True)
        state.store.set_child_busy(child, True)

        with mock.patch("core.workers.kick_off_suggest_for") as kick:
            kick_off_bulk_suggest(state)

        kick.assert_not_called()

    def test_bulk_suggest_uses_store_message_snapshot(self) -> None:
        repo = _make_repo("r")
        repo.staged = [("M", "repo.txt")]
        state = _state(repo)
        state.store.set_row_message(repo, "store draft")
        repo.message = ""

        with mock.patch("core.workers.kick_off_suggest_for") as kick:
            kick_off_bulk_suggest(state)

        kick.assert_not_called()

    def test_bulk_suggest_uses_store_workspace_rows(self) -> None:
        repo = _make_repo("r")
        repo.staged = [("M", "repo.txt")]
        state = _state(repo)
        state.repos = []

        with mock.patch("core.workers.kick_off_suggest_for") as kick:
            kick_off_bulk_suggest(state)

        kick.assert_called_once_with(state, repo)

    def test_active_repo_suggestion_does_not_block_mutation_gate(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        suggestion_started = threading.Event()
        release_suggestion = threading.Event()

        def suggest_side_effect(*_args, **_kwargs):
            suggestion_started.set()
            self.assertTrue(release_suggestion.wait(timeout=2.0))
            return "suggested message"

        with mock.patch(
                "core.workers.suggest_commit_message",
                side_effect=suggest_side_effect):
            kick_off_suggest_for(state, repo)
            self.assertTrue(suggestion_started.wait(timeout=2.0))
            self.assertTrue(state.store.repo_suggesting(repo))
            self.assertFalse(state.job_registry.has_active_local_mutation())
            release_suggestion.set()

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_suggesting(repo))

    def test_repo_suggestion_failure_finishes_job_failed(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch(
                "core.workers.suggest_commit_message",
                side_effect=RuntimeError("suggest failed")):
            kick_off_suggest_for(state, repo)

        _wait_for_job_terminal(state)
        self.assertFalse(state.store.repo_suggesting(repo))
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "suggest failed")

    def test_repo_suggestion_thread_failure_clears_suggesting(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_suggest_for(state, repo)

        self.assertFalse(state.store.repo_suggesting(repo))
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")

    def test_child_suggestion_thread_failure_clears_suggesting(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        parent = _make_repo("parent")
        child_repo = _make_repo("child")
        child = ChildRef(
            repo=child_repo,
            nested_path=Path("/tmp/parent/child"),
            kind="submodule",
        )
        parent.children = [child]
        state = _state(parent, child_repo)

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_suggest_for(state, child)

        self.assertFalse(state.store.child_suggesting(child))
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")

    def test_review_suggestion_updates_block_and_target_repo(self) -> None:
        repo = _make_repo("r")
        state = _state(repo)
        block = ReviewBlock(
            label="r",
            branch="main",
            target_path=repo.path,
            draft_id=f"repo:{repo.path}",
            target_repo=repo,
        )
        state.review_drafts.set_files(
            block.draft_id,
            [],
            {"a.txt": True, "b.txt": False},
        )

        with mock.patch(
                "core.workers.suggest_commit_message_for_paths",
                return_value="review message") as suggest:
            kick_off_review_suggest(state, block)

        _wait_for_job_terminal(state)
        suggest.assert_called_once_with(
            repo.path,
            ["a.txt"],
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
        )
        draft = state.review_drafts.get(block.draft_id)
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertFalse(draft.suggesting)
        self.assertEqual(draft.message, "review message")
        self.assertEqual(state.store.row_message(repo), "review message")
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.spec.kind, "review-suggest")
        self.assertEqual(job.spec.repo_keys, (str(repo.path),))
        self.assertFalse(job.spec.local_mutation)
        self.assertEqual(job.status, JobStatus.OK)

    def test_review_suggestion_thread_failure_clears_suggesting(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("r")
        state = _state(repo)
        block = ReviewBlock(
            label="r",
            branch="main",
            target_path=repo.path,
            draft_id=f"repo:{repo.path}",
            target_repo=repo,
        )
        state.review_drafts.set_files(
            block.draft_id,
            [],
            {"a.txt": True},
        )

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_review_suggest(state, block)

        draft = state.review_drafts.get_or_create(block.draft_id)
        self.assertFalse(draft.suggesting)
        job = state.job_registry.snapshot()[0]
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
