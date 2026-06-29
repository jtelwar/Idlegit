"""Tests for preserving busy child rows while relinking siblings.

Covers:
  - `link_siblings` preserves a busy old `ChildRef` when the state-backed
    busy predicate reports ownership, instead of minting a fresh instance
    outside the store-owned row identity.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import (  # noqa: E402
    assert_child_refresh_blocked,
    held_child_refresh,
    make_repo_model as _make_repo,
)
import core.git_ops as go  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.git_ops import (  # noqa: E402
    apply_link_siblings_snapshot,
    link_siblings,
    read_link_siblings_snapshot,
)
from core.state.selectors import read_only_child_busy_predicate  # noqa: E402


class TestLinkSiblingsPreservesBusyChild(unittest.TestCase):
    """`link_siblings` rebuilds each parent's `children` list on every
    inline-refresh + smart-sync pass. A submodule that's mid-push
    (commit_worker_for_child owns the row in StateStore) MUST keep its
    original ChildRef instance so the spinner and store row identity stay
    attached to the in-flight worker."""

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

    def _indexed_state(self, parent, canonical) -> State:
        return State(repos=[parent, canonical], workspace_name="ws")

    def _link_with_state(self, state: State, parent, canonical) -> None:
        link_siblings(
            [parent, canonical],
            subtrees=None,
            busy_child_predicate=read_only_child_busy_predicate(state),
        )

    def _start_thread(self, target):
        errors = []

        def wrapped() -> None:
            try:
                target()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=wrapped)
        thread.start()
        return thread, errors

    def _join_thread(self, thread: threading.Thread, errors: list) -> None:
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_busy_child_instance_preserved(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        # First pass — fresh idle ChildRef.
        self.assertEqual(len(parent.children), 1)
        old_child = parent.children[0]
        state.store.set_child_busy(old_child, True)
        with held_child_refresh(state, old_child):
            # Rebuild while state says the child row is busy.
            self._link_with_state(state, parent, canonical)
            self.assertEqual(len(parent.children), 1)
            new_child = parent.children[0]
            self.assertIs(new_child, old_child)
            self.assertTrue(state.store.child_busy(new_child))
            assert_child_refresh_blocked(self, state, new_child)

    def test_read_link_siblings_snapshot_does_not_mutate_projection(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()

        snapshot = read_link_siblings_snapshot([parent, canonical], subtrees=None)

        self.assertEqual(parent.children, [])
        self.assertEqual(parent.siblings, [])
        self.assertEqual(len(snapshot.children_for(parent)), 1)

        apply_link_siblings_snapshot(snapshot)

        self.assertEqual(len(parent.children), 1)
        self.assertEqual(parent.children[0].nested_path, sub_path)

    def test_store_lock_without_busy_predicate_does_not_preserve_child(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        old_child = parent.children[0]

        with held_child_refresh(state, old_child):
            link_siblings(
                [parent, canonical],
                subtrees=None,
                busy_child_predicate=lambda _child: False,
            )
            new_child = parent.children[0]
            self.assertIsNot(new_child, old_child)

    def test_idle_child_rebuilt_with_fresh_instance(self) -> None:
        # Counterpart to the busy case: when the old ref is not store-busy,
        # link_siblings is free to mint a new ChildRef. Confirms the
        # preservation path is gated on store busy state, not unconditional.
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        old_child = parent.children[0]
        self.assertFalse(state.store.child_busy(old_child))
        self._link_with_state(state, parent, canonical)
        new_child = parent.children[0]
        # Different ChildRef instance — the rebuild went through.
        self.assertIsNot(new_child, old_child)

    def test_child_becoming_busy_during_population_is_preserved(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        old_child = parent.children[0]

        entered = threading.Event()
        release = threading.Event()

        def populate(ref: ChildRef) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=2.0))

        def relink() -> None:
            self._link_with_state(state, parent, canonical)

        with mock.patch.object(go, "_populate_child_ref", side_effect=populate):
            t, errors = self._start_thread(relink)
            self.assertTrue(entered.wait(timeout=2.0))
            with held_child_refresh(state, old_child):
                state.store.set_child_busy(old_child, True)
                release.set()
                self._join_thread(t, errors)
                self.assertEqual(len(parent.children), 1)
                new_child = parent.children[0]
                self.assertIs(new_child, old_child)
                self.assertTrue(state.store.child_busy(new_child))
                assert_child_refresh_blocked(self, state, new_child)

    def test_latest_dirty_child_message_preserved_after_population(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        old_child = parent.children[0]
        old_child.message = "before"

        entered = threading.Event()
        release = threading.Event()

        def populate(ref: ChildRef) -> None:
            entered.set()
            self.assertTrue(release.wait(timeout=2.0))
            ref.dirty = True

        def relink() -> None:
            self._link_with_state(state, parent, canonical)

        with mock.patch.object(go, "_populate_child_ref", side_effect=populate):
            t, errors = self._start_thread(relink)
            self.assertTrue(entered.wait(timeout=2.0))
            old_child.message = "after"
            release.set()
            self._join_thread(t, errors)

        self.assertEqual(len(parent.children), 1)
        new_child = parent.children[0]
        self.assertIsNot(new_child, old_child)
        self.assertTrue(new_child.dirty)
        self.assertEqual(new_child.message, "after")

    def test_dirty_child_message_can_be_preserved_from_store_lookup(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        link_siblings([parent, canonical], subtrees=None)
        state = self._indexed_state(parent, canonical)
        old_child = parent.children[0]
        old_child.message = ""
        state.store.set_row_message(old_child, "store draft")

        def populate(ref: ChildRef) -> None:
            ref.dirty = True

        with mock.patch.object(go, "_populate_child_ref", side_effect=populate):
            snapshot = read_link_siblings_snapshot(
                [parent, canonical],
                subtrees=None,
                child_message_lookup=state.store.row_message,
            )
        apply_link_siblings_snapshot(snapshot)

        self.assertEqual(len(parent.children), 1)
        new_child = parent.children[0]
        self.assertIsNot(new_child, old_child)
        self.assertTrue(new_child.dirty)
        self.assertEqual(new_child.message, "store draft")

    def test_stale_structure_draft_retries_before_swap(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        other = _make_repo("other")
        other.remote_url = "github.com/o/other"
        other_path = parent.path / "other"

        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def populate(ref: ChildRef) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                self.assertTrue(release.wait(timeout=2.0))

        def relink() -> None:
            link_siblings([parent, canonical, other], subtrees=None)

        with mock.patch.object(go, "_populate_child_ref", side_effect=populate):
            t, errors = self._start_thread(relink)
            self.assertTrue(entered.wait(timeout=2.0))
            parent.nested_subs = [("github.com/o/other", other_path)]
            release.set()
            self._join_thread(t, errors)

        self.assertEqual(len(parent.children), 1)
        ref = parent.children[0]
        self.assertIs(ref.repo, other)
        self.assertEqual(ref.nested_path, other_path)
        self.assertEqual(canonical.siblings, [])
        self.assertEqual(len(other.siblings), 1)

    def test_concurrent_relinks_do_not_duplicate_child_or_sibling(self) -> None:
        parent, canonical, sub_path = self._make_parent_with_submodule()
        entered = threading.Barrier(2)
        release = threading.Event()

        def populate(ref: ChildRef) -> None:
            entered.wait(timeout=2.0)
            self.assertTrue(release.wait(timeout=2.0))

        def relink() -> None:
            link_siblings([parent, canonical], subtrees=None)

        with mock.patch.object(go, "_populate_child_ref", side_effect=populate):
            t1, errors1 = self._start_thread(relink)
            t2, errors2 = self._start_thread(relink)
            release.set()
            self._join_thread(t1, errors1)
            self._join_thread(t2, errors2)

        self.assertEqual(len(parent.children), 1)
        self.assertEqual(len(canonical.siblings), 1)


if __name__ == "__main__":
    unittest.main()
