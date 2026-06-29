"""RefreshClaim ownership tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model, make_state  # noqa: E402
from core.runtime.claims import CanonicalTreeClaim, RefreshClaim  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402


class TestRefreshClaim(unittest.TestCase):
    def test_repo_claim_updates_store_busy(self) -> None:
        repo = make_repo_model("repo")
        state = make_state(repo)
        claim = RefreshClaim(state, repo=repo)

        self.assertTrue(claim.acquire())
        self.assertTrue(state.store.repo_busy(repo))

        claim.release()

        self.assertFalse(state.store.repo_busy(repo))

    def test_repo_claim_returns_false_when_lock_is_busy(self) -> None:
        repo = make_repo_model("repo")
        state = make_state(repo)
        existing = RefreshClaim(state, repo=repo)
        self.assertTrue(existing.acquire())

        contender = RefreshClaim(state, repo=repo)

        self.assertFalse(contender.acquire())
        self.assertFalse(contender.acquired)
        self.assertTrue(state.store.repo_busy(repo))

        existing.release()

    def test_repo_claim_returns_false_when_store_busy_without_lock(self) -> None:
        repo = make_repo_model("repo")
        state = make_state(repo)
        state.store.set_repo_busy(repo, True)

        contender = RefreshClaim(state, repo=repo)

        self.assertFalse(contender.acquire())
        self.assertFalse(contender.acquired)
        self.assertTrue(state.store.repo_busy(repo))
        acquired, repo_id = state.store.acquire_repo_refresh(repo)
        self.assertTrue(acquired)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_child_claim_returns_false_when_store_busy_without_lock(self) -> None:
        parent = make_repo_model("parent")
        canonical = make_repo_model("child")
        child = ChildRef(repo=canonical, nested_path=Path("/tmp/parent/child"))
        parent.children = [child]
        state = make_state(parent, canonical)
        state.store.set_child_busy(child, True)

        contender = RefreshClaim(state, child=child)

        self.assertFalse(contender.acquire())
        self.assertFalse(contender.acquired)
        self.assertTrue(state.store.child_busy(child))
        acquired, child_id = state.store.acquire_child_refresh(child)
        self.assertTrue(acquired)
        state.store.release_child_refresh_by_id(child_id)

    def test_child_claim_updates_store_busy(self) -> None:
        parent = make_repo_model("parent")
        canonical = make_repo_model("child")
        child = ChildRef(repo=canonical, nested_path=Path("/tmp/parent/child"))
        parent.children = [child]
        state = make_state(parent, canonical)
        claim = RefreshClaim(state, child=child)

        self.assertTrue(claim.acquire())
        self.assertTrue(state.store.child_busy(child))

        claim.release()

        self.assertFalse(state.store.child_busy(child))

    def test_release_is_idempotent(self) -> None:
        repo = make_repo_model("repo")
        state = make_state(repo)
        claim = RefreshClaim(state, repo=repo)

        with claim:
            self.assertTrue(state.store.repo_busy(repo))

        claim.release()
        self.assertFalse(state.store.repo_busy(repo))


class TestCanonicalTreeClaim(unittest.TestCase):
    def test_claim_marks_canonical_and_submodule_rows_busy(self) -> None:
        parent = make_repo_model("parent")
        canonical = make_repo_model("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=Path("/tmp/parent/canonical"),
            kind="submodule",
        )
        parent.children = [child]
        canonical.siblings = [(parent, child.nested_path)]
        state = make_state(parent, canonical)
        claim = CanonicalTreeClaim(state, canonical)

        self.assertTrue(claim.acquire())

        self.assertTrue(state.store.repo_busy(canonical))
        self.assertTrue(state.store.child_busy(child))

        claim.release()

        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(child))


if __name__ == "__main__":
    unittest.main()
