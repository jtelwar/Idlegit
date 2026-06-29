"""State lease manager tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.ids import child_id, repo_id, workspace_id  # noqa: E402
from core.runtime.leases import LeaseConflictError, LeaseManager  # noqa: E402


class TestLeaseManager(unittest.TestCase):
    def test_mutation_lease_matches_targets_until_released(self) -> None:
        leases = LeaseManager()
        repo = _make_repo("repo")
        other = _make_repo("other")
        child = ChildRef(repo=repo, nested_path=repo.path / "vendor" / "sdk")

        lease_id = leases.acquire(repo=other)

        self.assertTrue(leases.has_leases())
        self.assertTrue(leases.has_lease_for(repos=[other]))
        self.assertFalse(leases.has_lease_for(repos=[repo]))
        self.assertFalse(leases.has_lease_for(children=[child]))

        leases.release(lease_id)

        self.assertFalse(leases.has_leases())
        self.assertFalse(leases.has_lease_for(repos=[other]))

    def test_snapshot_preserves_owner_and_targets(self) -> None:
        leases = LeaseManager()
        repo = _make_repo("repo")
        workspace = workspace_id("ws")
        rid = repo_id(workspace, repo.path)

        lease_id = leases.acquire(
            repo=repo,
            repo_id=rid,
            owner_id="job-123",
            owner_label="commit",
        )

        active = leases.snapshot()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].lease_id, lease_id)
        self.assertEqual(active[0].owner_id, "job-123")
        self.assertEqual(active[0].owner_label, "commit")
        self.assertIs(active[0].repo, repo)
        self.assertEqual(active[0].repo_id, rid)
        self.assertIsNone(active[0].child)

        leases.release(lease_id)
        leases.release(lease_id)
        self.assertEqual(leases.snapshot(), [])

    def test_mutation_lease_matches_stable_ids(self) -> None:
        leases = LeaseManager()
        workspace = workspace_id("ws")
        repo = _make_repo("repo")
        replacement = _make_repo("repo")
        rid = repo_id(workspace, repo.path)
        child = ChildRef(repo=repo, nested_path=repo.path / "vendor" / "sdk")
        cid = child_id(rid, child.nested_path, child.kind)

        leases.acquire(repo_id=rid, child_id=cid)

        self.assertTrue(leases.has_lease_for(repo_ids=[rid]))
        self.assertTrue(leases.has_lease_for(child_ids=[cid]))
        self.assertFalse(leases.has_lease_for(repos=[replacement]))

    def test_overlapping_repo_lease_is_rejected(self) -> None:
        leases = LeaseManager()
        workspace = workspace_id("ws")
        repo = _make_repo("repo")
        rid = repo_id(workspace, repo.path)

        first = leases.acquire(
            repo=repo,
            repo_id=rid,
            owner_label="commit",
        )

        with self.assertRaises(LeaseConflictError):
            leases.acquire(
                repo=repo,
                repo_id=rid,
                owner_label="push",
            )

        self.assertTrue(leases.has_lease_for(repo_ids=[rid]))
        leases.release(first)
        self.assertFalse(leases.has_lease_for(repo_ids=[rid]))

    def test_child_lease_conflicts_with_parent_repo_lease(self) -> None:
        leases = LeaseManager()
        workspace = workspace_id("ws")
        repo = _make_repo("repo")
        rid = repo_id(workspace, repo.path)
        child = ChildRef(repo=repo, nested_path=repo.path / "vendor" / "sdk")
        cid = child_id(rid, child.nested_path, child.kind)

        leases.acquire(repo=repo, repo_id=rid, owner_label="commit")

        with self.assertRaises(LeaseConflictError):
            leases.acquire(child=child, child_id=cid, owner_label="sync")

    def test_try_acquire_reports_conflict_without_raising(self) -> None:
        leases = LeaseManager()
        repo = _make_repo("repo")

        leases.acquire(repo=repo)

        self.assertEqual(leases.try_acquire(repo=repo), 0)

    def test_stale_leases_report_without_releasing(self) -> None:
        leases = LeaseManager()
        repo = _make_repo("repo")
        with mock.patch("core.runtime.leases._monotonic", return_value=10.0):
            lease_id = leases.acquire(
                repo=repo,
                owner_id="job-1",
                owner_label="commit",
                stale_after_seconds=5.0,
            )

        stale = leases.stale_leases(now=16.0)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].lease_id, lease_id)
        self.assertTrue(leases.has_leases())
        self.assertTrue(leases.has_lease_for(repos=[repo]))

        leases.release(lease_id)
        self.assertEqual(leases.stale_leases(now=30.0), [])

    def test_stale_leases_can_use_default_threshold(self) -> None:
        leases = LeaseManager()
        repo = _make_repo("repo")
        with mock.patch("core.runtime.leases._monotonic", return_value=10.0):
            leases.acquire(repo=repo)

        self.assertEqual(leases.stale_leases(now=20.0), [])
        self.assertEqual(
            len(leases.stale_leases(
                now=20.0,
                default_stale_after_seconds=5.0,
            )),
            1,
        )


if __name__ == "__main__":
    unittest.main()
