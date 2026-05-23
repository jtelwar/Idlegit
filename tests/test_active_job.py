"""Tests for the active-job tracking that gates refresh paths.

Covers:
  - `Tasks.repo_has_active_job` / `child_has_active_job` — return True
    only for non-terminal tasks tagged with `holds_repo` / `holds_child`,
    and only when the tag points at the same instance.
  - Tag does NOT count workflow-polling tasks (those set `meta.repo`
    but never `meta.holds_repo`) so a long workflow run doesn't
    block refresh of its repo.
  - `link_siblings` preserves a busy old `ChildRef` (refreshing=True)
    instead of minting a fresh instance with the spinner flag cleared
    and a new lock identity.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.models import ChildRef  # noqa: E402
from core.git_ops import link_siblings  # noqa: E402


class TestRepoHasActiveJob(unittest.TestCase):
    """`repo_has_active_job` is the explicit "running job operating on
    repo" check — refresh paths consult it to skip repos with a live
    commit / push / smart-sync worker."""

    def test_no_tasks_means_no_active_job(self) -> None:
        repo = _make_repo("a")
        s = _state(repo)
        self.assertFalse(s.tasks.repo_has_active_job(repo))

    def test_tagged_running_task_counts(self) -> None:
        repo = _make_repo("a")
        s = _state(repo)
        t = s.tasks.add("a: working")
        s.tasks.set_meta(t, holds_repo=repo)
        self.assertTrue(s.tasks.repo_has_active_job(repo))

    def test_terminal_tagged_task_does_not_count(self) -> None:
        repo = _make_repo("a")
        s = _state(repo)
        t = s.tasks.add("a: working")
        s.tasks.set_meta(t, holds_repo=repo)
        s.tasks.update(t, "ok")
        self.assertFalse(s.tasks.repo_has_active_job(repo))

    def test_tag_for_different_repo_does_not_count(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        s = _state(repo_a, repo_b)
        t = s.tasks.add("b: working")
        s.tasks.set_meta(t, holds_repo=repo_b)
        self.assertFalse(s.tasks.repo_has_active_job(repo_a))
        self.assertTrue(s.tasks.repo_has_active_job(repo_b))

    def test_workflow_polling_task_does_not_count(self) -> None:
        # `_poll_run` sets meta.repo to identify WHICH repo the run is
        # for, but leaves holds_repo None — polling doesn't operate on
        # the local working tree, so refresh should NOT skip during
        # workflow runs (could run for an hour).
        repo = _make_repo("a")
        s = _state(repo)
        t = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(t, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        self.assertFalse(s.tasks.repo_has_active_job(repo))

    def test_pending_tagged_task_counts(self) -> None:
        # `pending` is non-terminal; a queued then-run that holds the
        # repo should keep refresh away just like a running task.
        repo = _make_repo("a")
        s = _state(repo)
        t = s.tasks.add("a: queued")
        s.tasks.update(t, "pending")
        s.tasks.set_meta(t, holds_repo=repo)
        self.assertTrue(s.tasks.repo_has_active_job(repo))


class TestChildHasActiveJob(unittest.TestCase):
    """`child_has_active_job` mirrors the repo version for submodule
    child rows — set by `commit_worker_for_child` so refresh doesn't
    drop the spinner mid-push of a nested checkout."""

    def test_tagged_child_counts(self) -> None:
        parent = _make_repo("p")
        canonical = _make_repo("c")
        child = ChildRef(repo=canonical, nested_path=parent.path / "c",
                         kind="submodule")
        parent.children = [child]
        s = _state(parent, canonical)
        t = s.tasks.add("c (in p): working")
        s.tasks.set_meta(t, holds_repo=parent, holds_child=child)
        self.assertTrue(s.tasks.child_has_active_job(child))

    def test_other_child_does_not_count(self) -> None:
        parent = _make_repo("p")
        canon_a = _make_repo("a")
        canon_b = _make_repo("b")
        child_a = ChildRef(repo=canon_a, nested_path=parent.path / "a",
                           kind="submodule")
        child_b = ChildRef(repo=canon_b, nested_path=parent.path / "b",
                           kind="submodule")
        parent.children = [child_a, child_b]
        s = _state(parent, canon_a, canon_b)
        t = s.tasks.add("a (in p): working")
        s.tasks.set_meta(t, holds_repo=parent, holds_child=child_a)
        self.assertTrue(s.tasks.child_has_active_job(child_a))
        self.assertFalse(s.tasks.child_has_active_job(child_b))


class TestLinkSiblingsPreservesBusyChild(unittest.TestCase):
    """`link_siblings` rebuilds each parent's `children` list on every
    inline-refresh + smart-sync pass. A submodule that's mid-push
    (commit_worker_for_child holds its ChildRef.refresh_lock + sets
    refreshing=True) MUST keep its original ChildRef instance — minting
    a fresh one drops the spinner and splits the lock identity."""

    def _make_parent_with_submodule(self):
        # Construct a parent + canonical pair where the parent's
        # nested_subs declares the canonical as a submodule. Both
        # repos share their `remote_url` slot via the url_to_repo
        # map in `_link_siblings_locked`.
        canonical = _make_repo("canon")
        canonical.remote_url = "github.com/o/canon"
        parent = _make_repo("p")
        parent.remote_url = "github.com/o/p"
        sub_path = parent.path / "canon"
        parent.nested_subs = [("github.com/o/canon", sub_path)]
        return parent, canonical, sub_path

    def test_busy_child_instance_preserved(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        # First pass — fresh ChildRef, refreshing=False.
        self.assertEqual(len(parent.children), 1)
        old_child = parent.children[0]
        old_lock = old_child.refresh_lock
        # Simulate commit_worker_for_child claiming the lock + flag.
        self.assertTrue(old_child.try_acquire_refresh())
        try:
            # Rebuild while the lock is held.
            link_siblings([parent, canonical], subtrees=None)
            self.assertEqual(len(parent.children), 1)
            new_child = parent.children[0]
            # Same instance, same lock object — the in-flight worker
            # holds this lock and would lose interlock with anything
            # checking against a fresh ChildRef.
            self.assertIs(new_child, old_child)
            self.assertIs(new_child.refresh_lock, old_lock)
            self.assertTrue(new_child.refreshing)
        finally:
            old_child.release_refresh()

    def test_idle_child_rebuilt_with_fresh_instance(self) -> None:
        # Counterpart to the busy case: when refreshing=False on the
        # old ref, link_siblings is free to mint a new ChildRef — no
        # need to preserve the instance because no worker holds its
        # lock. Confirms the preservation path is gated on the busy
        # flag, not unconditional.
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        old_child = parent.children[0]
        self.assertFalse(old_child.refreshing)
        link_siblings([parent, canonical], subtrees=None)
        new_child = parent.children[0]
        # Different ChildRef instance — the rebuild went through.
        self.assertIsNot(new_child, old_child)


if __name__ == "__main__":
    unittest.main()
