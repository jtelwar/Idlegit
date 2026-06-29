"""Tests for the per-commit push toggle on the review screen.

Push used to be a hard reflection of the workspace `auto_push` setting.
It is now a navigable per-block toggle: `auto_push` only sets its default
each time the review screen opens, and everything that runs *on push*
(workflow tracking, then-run-after-push, sibling sync) hides when it's
flipped off.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import (  # noqa: E402
    make_repo_model as _make_repo, make_state as _state,
)

# UI imports curses at module load — skip on headless CI.
try:
    import curses  # noqa: F401
    from core.state.repos import WorkflowInfo
    from core.state.review import ReviewBlock, WorkflowToggle
    from ui.review import (
        build_review_blocks,
        _collect_review_focusables,
    )
    UI_AVAILABLE = True
except Exception:  # pragma: no cover — only when curses is unusable
    UI_AVAILABLE = False


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestPushDefault(unittest.TestCase):
    """`auto_push` seeds the toggle's default, nothing more."""

    def test_default_on_when_auto_push_on(self) -> None:
        repo = _make_repo(rel="r")
        state = _state(repo, auto_push=True)
        state.store.set_row_message(repo, "hello")
        blocks = build_review_blocks(state)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(
            state.review_drafts.get_or_create(blocks[0].draft_id).push)

    def test_default_off_when_auto_push_off(self) -> None:
        repo = _make_repo(rel="r")
        state = _state(repo, auto_push=False)
        state.store.set_row_message(repo, "hello")
        blocks = build_review_blocks(state)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(
            state.review_drafts.get_or_create(blocks[0].draft_id).push)

    def test_review_blocks_use_store_workspace_rows(self) -> None:
        repo = _make_repo(rel="r")
        state = _state(repo, auto_push=True)
        state.store.set_row_message(repo, "hello")
        state.repos = []

        blocks = build_review_blocks(state)

        self.assertEqual(len(blocks), 1)
        self.assertIs(blocks[0].target_repo, repo)

    def test_workflow_state_hydrates_lazily_for_review(self) -> None:
        repo = _make_repo(rel="r")
        repo.branch = "main"
        repo.remote_url_raw = "https://github.com/acme/widgets.git"
        repo.workflows = [
            WorkflowInfo(
                name="CI",
                path=".github/workflows/ci.yml",
                dispatchable=True,
                triggers_push=True,
            )
        ]
        state = _state(repo, auto_push=True)
        state.store.set_row_message(repo, "hello")
        with mock.patch("ui.review.gh_available", return_value=True), \
             mock.patch("ui.review.merge_remote_workflow_states") as merge:
            blocks = build_review_blocks(state)

        self.assertEqual(len(blocks), 1)
        merge.assert_called_once_with(repo.workflows, "acme/widgets")
        self.assertTrue(repo.workflow_states_hydrated)
        draft = state.review_drafts.get_or_create(blocks[0].draft_id)
        self.assertEqual(
            [t.workflow_name for t in draft.workflow_toggles],
            ["CI"])

    def test_hydrated_workflow_state_is_not_requeried(self) -> None:
        repo = _make_repo(rel="r")
        repo.branch = "main"
        repo.remote_url_raw = "https://github.com/acme/widgets.git"
        repo.workflow_states_hydrated = True
        repo.workflows = [
            WorkflowInfo(
                name="CI",
                path=".github/workflows/ci.yml",
                state="active",
                dispatchable=True,
                triggers_push=True,
            )
        ]
        state = _state(repo, auto_push=True)
        state.store.set_row_message(repo, "hello")
        with mock.patch("ui.review.gh_available", return_value=True), \
             mock.patch("ui.review.merge_remote_workflow_states") as merge:
            blocks = build_review_blocks(state)

        self.assertEqual(len(blocks), 1)
        merge.assert_not_called()


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestPushFocusables(unittest.TestCase):
    """Navigation order + push-gating of the focusables list."""

    def _block_with_toggle(self, push: bool) -> tuple:
        repo = _make_repo(rel="r", message="hello")
        state = _state(repo)
        block = ReviewBlock(
            label=repo.display_name, branch="main",
            target_path=repo.path, target_repo=repo,
            draft_id=f"repo:{repo.path}",
        )
        draft = state.review_drafts.create(
            block.draft_id, message="hello", push=push)
        draft.workflow_toggles.append(
            WorkflowToggle(repo=repo, workflow_name="ci.yml",
                           draft_id=block.draft_id))
        draft.track_workflow["ci.yml"] = True
        return state, block

    def test_order_is_message_then_push_then_actions(self) -> None:
        # Pipeline / execution order top-to-bottom: the commit message,
        # then the push toggle, then the on-push workflow toggle.
        state, block = self._block_with_toggle(push=True)
        kinds = [k for _, k, _ in _collect_review_focusables(state, [block])]
        self.assertEqual(kinds[:3], ["suggest", "push", "toggle"])

    def test_push_off_hides_workflow_focusable(self) -> None:
        state, block = self._block_with_toggle(push=False)
        kinds = [k for _, k, _ in _collect_review_focusables(state, [block])]
        # Message + push remain reachable; the workflow toggle (push-only)
        # drops out.
        self.assertEqual(kinds, ["suggest", "push"])

    def test_push_on_surfaces_workflow_focusable(self) -> None:
        state, block = self._block_with_toggle(push=True)
        kinds = [k for _, k, _ in _collect_review_focusables(state, [block])]
        self.assertIn("toggle", kinds)

    def test_then_run_appears_only_when_action_tracked(self) -> None:
        # The per-action "then run" chain is focusable only while that
        # action's toggle is on. The after-push then_run (a child of the
        # push toggle, not the action) is present either way.
        state, block = self._block_with_toggle(push=True)
        draft = state.review_drafts.get_or_create(block.draft_id)
        draft.track_workflow["ci.yml"] = False
        off = [k for _, k, _ in _collect_review_focusables(state, [block])]
        draft.track_workflow["ci.yml"] = True
        on = [k for _, k, _ in _collect_review_focusables(state, [block])]
        self.assertEqual(off.count("then_run"), 1)
        self.assertEqual(on.count("then_run"), 2)

    def test_merging_block_has_no_push_focusable(self) -> None:
        # A merge-in-progress block is skipped at commit time, so it
        # offers neither a push toggle nor a message row to land on.
        repo = _make_repo(rel="r", message="hello")
        state = _state(repo)
        block = ReviewBlock(
            label=repo.display_name, branch="main",
            target_path=repo.path, target_repo=repo,
            draft_id=f"repo:{repo.path}",
            merging=True)
        state.review_drafts.create(block.draft_id, message="hello", push=True)
        kinds = [k for _, k, _ in _collect_review_focusables(state, [block])]
        self.assertNotIn("push", kinds)
        self.assertNotIn("suggest", kinds)


if __name__ == "__main__":
    unittest.main()
