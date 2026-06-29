"""WorkerClaim ownership tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model, make_state  # noqa: E402
from core.runtime.leases import LeaseConflictError  # noqa: E402
from core.runtime.claims import CanonicalTreeClaim, WorkerClaim  # noqa: E402
from core.runtime.jobs import JobSpec  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.store import ChildStatusSnapshot, ChildTopologySnapshot  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from core.state.app import State  # noqa: E402


class TestWorkerClaim(unittest.TestCase):
    def test_releases_acquired_repo_lock(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)

        with WorkerClaim(state, repo=repo, acquire_repo=True):
            self.assertTrue(state.store.repo_busy(repo))
            acquired, _repo_id = state.store.acquire_repo_refresh(repo)
            self.assertFalse(acquired)

        self.assertFalse(state.store.repo_busy(repo))
        acquired, repo_id = state.store.acquire_repo_refresh(repo)
        self.assertTrue(acquired)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_canonical_tree_claim_releases_captured_child_after_workspace_switch(self) -> None:
        parent = make_repo_model("parent")
        canonical = make_repo_model("canonical")
        other = make_repo_model("other")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        workspace_a = Workspace(
            name="A",
            folders=[parent.path.parent],
            cached_repos=[parent, canonical],
        )
        workspace_b = Workspace(
            name="B",
            folders=[other.path.parent],
            cached_repos=[other],
        )
        state = State(
            repos=[parent, canonical],
            workspace_name="A",
            workspaces=[workspace_a, workspace_b],
            active_workspace_index=0,
        )
        claim = CanonicalTreeClaim(state, canonical)

        claim.acquire()
        self.assertTrue(state.store.child_busy(child))
        state.active_workspace_index = 1
        state.replace_repos([other], workspace=workspace_b)
        claim.release()
        state.store.replace_workspace_topology(
            name="A",
            folders=workspace_a.folders,
            repos=[parent, canonical],
            children=[
                ChildTopologySnapshot(
                    parent_repo=parent,
                    child=child,
                    status=ChildStatusSnapshot(kind="submodule"),
                ),
            ],
            activate=False,
        )

        self.assertFalse(state.store.child_busy(child))

    def test_child_failure_releases_parent_claim(self) -> None:
        parent = make_repo_model("parent")
        child_repo = make_repo_model("child")
        child = ChildRef(repo=child_repo, nested_path=Path("/tmp/child"))
        parent.children = [child]
        state = make_state(parent, child_repo)
        child_acquired, child_id = state.store.acquire_child_refresh(child)
        self.assertTrue(child_acquired)

        try:
            with self.assertRaises(RuntimeError):
                with WorkerClaim(
                        state, repo=parent, child=child,
                        acquire_repo=True, acquire_child=True):
                    pass
            self.assertFalse(state.store.repo_busy(parent))
            acquired, repo_id = state.store.acquire_repo_refresh(parent)
            self.assertTrue(acquired)
            state.store.release_repo_refresh_by_id(repo_id)
        finally:
            state.store.release_child_refresh_by_id(child_id)

    def test_repo_acquire_refuses_store_busy_without_lock(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)
        state.store.set_repo_busy(repo, True)

        with self.assertRaises(RuntimeError):
            with WorkerClaim(state, repo=repo, acquire_repo=True):
                pass

        self.assertTrue(state.store.repo_busy(repo))
        acquired, repo_id = state.store.acquire_repo_refresh(repo)
        self.assertTrue(acquired)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_child_store_busy_failure_releases_parent_claim(self) -> None:
        parent = make_repo_model("parent")
        child_repo = make_repo_model("child")
        child = ChildRef(repo=child_repo, nested_path=Path("/tmp/child"))
        parent.children = [child]
        state = make_state(parent, child_repo)
        state.store.set_child_busy(child, True)

        with self.assertRaises(RuntimeError):
            with WorkerClaim(
                    state, repo=parent, child=child,
                    acquire_repo=True, acquire_child=True):
                pass

        self.assertFalse(state.store.repo_busy(parent))
        acquired, repo_id = state.store.acquire_repo_refresh(parent)
        self.assertTrue(acquired)
        state.store.release_repo_refresh_by_id(repo_id)

    def test_child_failure_restores_marked_parent_flag(self) -> None:
        parent = make_repo_model("parent")
        child_repo = make_repo_model("child")
        child = ChildRef(repo=child_repo, nested_path=Path("/tmp/child"))
        parent.children = [child]
        state = make_state(parent, child_repo)
        child_acquired, child_id = state.store.acquire_child_refresh(child)
        self.assertTrue(child_acquired)

        try:
            with self.assertRaises(RuntimeError):
                with WorkerClaim(
                        state, repo=parent, child=child,
                        acquire_child=True):
                    pass
            self.assertFalse(state.store.repo_busy(parent))
        finally:
            state.store.release_child_refresh_by_id(child_id)

    def test_links_cancel_job_to_task_in_registry(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)
        job = state.job_registry.start(JobSpec(
            kind="commit-batch",
            label="commit workers",
            local_mutation=True,
        ))
        task = state.tasks.add("a: working")

        with WorkerClaim(
                state, repo=repo, task=task, cancel_job_id=job.job_id,
                mark_repo=False):
            self.assertIs(state.job_registry.job_for_task(task), job)

    def test_mark_only_claim_releases_only_its_own_store_busy_count(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)

        with WorkerClaim(state, repo=repo):
            self.assertTrue(state.store.repo_busy(repo))

        self.assertFalse(state.store.repo_busy(repo))

        state.store.set_repo_busy(repo, True)
        with WorkerClaim(state, repo=repo):
            self.assertTrue(state.store.repo_busy(repo))
        self.assertTrue(state.store.repo_busy(repo))

        state.store.set_repo_busy(repo, False)
        self.assertFalse(state.store.repo_busy(repo))

    def test_overlapping_mutation_claim_is_rejected_and_first_busy_remains(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)
        first = WorkerClaim(state, repo=repo)
        second = WorkerClaim(state, repo=repo)

        first.__enter__()

        with self.assertRaises(LeaseConflictError):
            second.__enter__()
        self.assertTrue(state.store.repo_busy(repo))

        first.__exit__(None, None, None)

        self.assertFalse(state.store.repo_busy(repo))

    def test_claim_without_task_counts_as_local_mutation(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)

        with WorkerClaim(state, repo=repo):
            self.assertTrue(state.leases.has_leases())

        self.assertFalse(state.leases.has_leases())

    def test_exit_is_idempotent_for_mutation_claim(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)
        claim = WorkerClaim(state, repo=repo)

        claim.__enter__()
        self.assertTrue(state.leases.has_leases())
        claim.__exit__(None, None, None)
        claim.__exit__(None, None, None)

        self.assertFalse(state.leases.has_leases())
        self.assertFalse(state.store.repo_busy(repo))

    def test_exit_is_idempotent_for_acquired_locks(self) -> None:
        parent = make_repo_model("parent")
        child_repo = make_repo_model("child")
        child = ChildRef(repo=child_repo, nested_path=Path("/tmp/child"))
        parent.children = [child]
        state = make_state(parent, child_repo)
        claim = WorkerClaim(
            state,
            repo=parent,
            child=child,
            acquire_repo=True,
            acquire_child=True,
        )

        claim.__enter__()
        self.assertTrue(state.store.repo_busy(parent))
        self.assertTrue(state.store.child_busy(child))
        claim.__exit__(None, None, None)
        claim.__exit__(None, None, None)

        self.assertFalse(state.store.repo_busy(parent))
        self.assertFalse(state.store.child_busy(child))
        self.assertFalse(claim.repo_acquired)
        self.assertFalse(claim.child_acquired)
        repo_acquired, repo_id = state.store.acquire_repo_refresh(parent)
        self.assertTrue(repo_acquired)
        state.store.release_repo_refresh_by_id(repo_id)
        child_acquired, child_id = state.store.acquire_child_refresh(child)
        self.assertTrue(child_acquired)
        state.store.release_child_refresh_by_id(child_id)

    def test_claim_exposes_owner_and_targets(self) -> None:
        repo = make_repo_model("a")
        state = make_state(repo)
        claim = WorkerClaim(
            state, repo=repo, owner_id="job-123",
            owner_label="commit")

        self.assertEqual(claim.owner_id, "job-123")
        self.assertEqual(claim.owner_label, "commit")
        self.assertEqual(claim.target_repos, [repo])
        self.assertEqual(claim.target_children, [])

        with claim:
            active = state.leases.snapshot()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].owner_id, "job-123")
            self.assertEqual(active[0].owner_label, "commit")
            self.assertIs(active[0].repo, repo)
            self.assertEqual(active[0].repo_id, state.store.repo_id_for(repo))
            self.assertIsNone(active[0].child)

        self.assertEqual(state.leases.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
