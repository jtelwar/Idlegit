"""Smart-sync sentinel lifecycle tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.smart_sync.lifecycle import SmartSyncLifecycle  # noqa: E402


class TestSmartSyncLifecycle(unittest.TestCase):
    def test_acquire_finish_releases_row_state_and_terminalizes_rows(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        subtree_repo = _make_repo("subtree")
        nested = parent.path / "vendor" / "canonical"
        canonical_child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        subtree_child = ChildRef(
            repo=subtree_repo,
            nested_path=parent.path / "vendor" / "subtree",
            branch="main",
            kind="subtree",
        )
        parent.children = [canonical_child, subtree_child]
        canonical.siblings = [(parent, nested)]
        state = State(repos=[parent, canonical, subtree_repo], workspace_name="A")
        header = state.tasks.add("smart-sync (2)")
        job = state.job_registry.start(JobSpec(
            kind="smart-sync",
            label=header.label,
            local_mutation=True,
            repo_keys=(str(parent.path), str(canonical.path)),
            child_keys=(str(canonical_child.nested_path), str(subtree_child.nested_path)),
        ))
        lifecycle = SmartSyncLifecycle(
            state, header, job, [canonical], [(parent, subtree_child)])

        lifecycle.acquire()

        self.assertTrue(state.store.repo_busy(canonical))
        self.assertTrue(state.store.child_busy(canonical_child))
        self.assertTrue(state.store.child_busy(subtree_child))
        self.assertFalse(state.leases.has_lease_for(repos=[canonical]))
        self.assertFalse(state.leases.has_lease_for(children=[subtree_child]))
        self.assertTrue(state.job_registry.has_active_local_mutation_for(
            repo_keys=(str(canonical.path),)))
        self.assertTrue(state.job_registry.has_active_local_mutation_for(
            child_keys=(str(subtree_child.nested_path),)))

        lifecycle.record_canonical_result(canonical, 0)
        lifecycle.record_subtree_result(subtree_child, False)
        lifecycle.finish(job, ok_total=1, fail_total=1)

        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(canonical_child))
        self.assertFalse(state.store.child_busy(subtree_child))
        self.assertFalse(state.leases.has_lease_for(repos=[canonical]))
        self.assertFalse(state.leases.has_lease_for(children=[subtree_child]))
        self.assertEqual(header.status, "warn")
        self.assertEqual(header.message, "1 ok / 1 failed")
        self.assertEqual(job.status, JobStatus.WARN)
        canonical_task = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ↳ smart-sync canonical")
        subtree_task = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ⊕ smart-sync subtree")
        self.assertEqual(canonical_task.status, "ok")
        self.assertEqual(subtree_task.status, "warn")
        self.assertIs(state.job_registry.job_for_task(header), job)
        self.assertIs(state.job_registry.job_for_task(canonical_task), job)
        self.assertIs(state.job_registry.job_for_task(subtree_task), job)

    def test_thread_start_failure_releases_row_state_and_marks_sentinels_failed(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        state = State(repos=[parent, canonical], workspace_name="A")
        header = state.tasks.add("smart-sync (1)")
        job = state.job_registry.start(JobSpec(
            kind="smart-sync",
            label=header.label,
            local_mutation=True,
            repo_keys=(str(canonical.path),),
        ))
        lifecycle = SmartSyncLifecycle(state, header, job, [canonical], [])

        lifecycle.acquire()
        lifecycle.fail_thread_start("thread start failed")

        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(child))
        self.assertFalse(state.leases.has_lease_for(repos=[canonical]))
        self.assertEqual(header.status, "fail")
        sentinel = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ↳ smart-sync canonical")
        self.assertEqual(sentinel.status, "fail")
        self.assertEqual(sentinel.message, "thread start failed")

    def test_finish_failure_uses_computed_outcome_not_header_readback(self) -> None:
        canonical = _make_repo("canonical")
        state = State(repos=[canonical], workspace_name="A")
        header = state.tasks.add("smart-sync (1)")
        job = state.job_registry.start(JobSpec(
            kind="smart-sync",
            label=header.label,
            local_mutation=True,
            repo_keys=(str(canonical.path),),
        ))
        lifecycle = SmartSyncLifecycle(state, header, job, [canonical], [])

        lifecycle.acquire()
        lifecycle.record_canonical_result(canonical, 1)
        lifecycle.finish(job, ok_total=0, fail_total=1)

        self.assertEqual(header.status, "fail")
        self.assertEqual(header.message, "1 failed")
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "1 failed")


if __name__ == "__main__":
    unittest.main()
