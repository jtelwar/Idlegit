"""Smart-sync threaded runner tests."""

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
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.smart_sync.lifecycle import SmartSyncLifecycle  # noqa: E402
from core.smart_sync.runner import (  # noqa: E402
    SmartSyncRunConfig,
    build_smart_sync_work_plan,
    run_smart_sync_job,
)


class TestSmartSyncRunner(unittest.TestCase):
    def test_work_plan_collects_precise_repo_and_child_targets(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        subtree_repo = _make_repo("subtree")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        subtree = ChildRef(
            repo=subtree_repo,
            nested_path=parent.path / "vendor" / "subtree",
            branch="main",
            kind="subtree",
        )
        parent.children = [child, subtree]
        canonical.siblings = [(parent, nested)]
        state = State(repos=[parent, canonical, subtree_repo], workspace_name="A")

        plan = build_smart_sync_work_plan(state)

        self.assertEqual(plan.snapshot_repos, [parent, canonical, subtree_repo])
        self.assertEqual(plan.canonicals, [canonical])
        self.assertEqual(plan.subtree_items, [(parent, subtree)])
        self.assertEqual(
            plan.repo_keys,
            (str(canonical.path), str(parent.path)),
        )
        self.assertEqual(
            plan.child_keys,
            (str(nested), str(subtree.nested_path)),
        )
        self.assertEqual(plan.work_count, 2)

    def test_work_plan_adds_recursive_submodule_checkouts_for_smart_sync(self) -> None:
        app = _make_repo("app")
        sdk = _make_repo("sdk", remote_url="git@example.com:org/sdk.git")
        models = _make_repo("models", remote_url="git@example.com:org/models.git")
        sdk_in_app = app.path / "vendor" / "sdk"
        models_in_sdk = sdk.path / "vendor" / "models"
        models_in_app_sdk = sdk_in_app / "vendor" / "models"
        sdk_child = ChildRef(repo=sdk, nested_path=sdk_in_app, branch="main")
        models_child = ChildRef(repo=models, nested_path=models_in_sdk, branch="main")
        app.children = [sdk_child]
        sdk.children = [models_child]
        sdk.siblings = [(app, sdk_in_app)]
        models.siblings = [(sdk, models_in_sdk)]
        state = State(repos=[app, sdk, models], workspace_name="A")

        def nested_submodules(path: Path):
            if path == sdk_in_app:
                return [(models.remote_url, models_in_app_sdk)]
            return []

        plan = build_smart_sync_work_plan(
            state,
            nested_submodules_fn=nested_submodules,
        )

        self.assertIn(sdk, plan.canonicals)
        self.assertIn(models, plan.canonicals)
        self.assertIn((sdk, models_in_sdk), models.siblings)
        recursive_edges = [
            (parent, path)
            for parent, path in models.siblings
            if path == models_in_app_sdk
        ]
        self.assertEqual(len(recursive_edges), 1)
        recursive_parent, _ = recursive_edges[0]
        self.assertTrue(recursive_parent.synthetic)
        self.assertEqual(recursive_parent.path, sdk_in_app)
        self.assertEqual(
            plan.child_keys,
            (
                str(sdk_in_app),
                str(models_in_sdk),
                str(models_in_app_sdk),
            ),
        )

    def test_runner_executes_steps_cleanup_and_terminalizes_lifecycle(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        subtree_repo = _make_repo("subtree")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        subtree = ChildRef(
            repo=subtree_repo,
            nested_path=parent.path / "vendor" / "subtree",
            branch="main",
            kind="subtree",
        )
        parent.children = [child, subtree]
        canonical.siblings = [(parent, nested)]
        state = State(repos=[parent, canonical, subtree_repo], workspace_name="A")
        state.auto_push_submodule_parent = True
        header = state.tasks.add("smart-sync (2)")
        job = state.job_registry.start(JobSpec(
            kind="smart-sync",
            label=header.label,
            local_mutation=True,
            repo_keys=(str(parent.path), str(canonical.path)),
            child_keys=(str(child.nested_path), str(subtree.nested_path)),
        ))
        lifecycle = SmartSyncLifecycle(
            state, header, job, [canonical], [(parent, subtree)])
        lifecycle.acquire()
        events = []

        config = SmartSyncRunConfig(
            state=state,
            snapshot_repos=[parent, canonical, subtree_repo],
            snapshot_subtrees=[],
            canonicals=[canonical],
            subtree_items=[(parent, subtree)],
            lifecycle=lifecycle,
            align_canonical=lambda _state, _canonical: events.append("align") or (1, 0),
            propagate_parents=lambda _state, _canonicals: events.append("propagate"),
            refresh_repo=lambda repo: events.append(f"refresh:{repo.rel}"),
            sync_subtree=lambda _path, _prefix, _remote, _branch:
                events.append("subtree") or (True, "subtree ok"),
            link_siblings=lambda repos, _subtrees:
                events.append(f"link:{','.join(repo.rel for repo in repos)}"),
            first_line=lambda text: text.splitlines()[0] if text else "",
        )

        cleanup_jobs = []

        def run_cleanup(registry, cleanup_job, target):
            cleanup_jobs.append(cleanup_job)
            self.assertEqual(job.status, JobStatus.OK)
            self.assertFalse(state.store.repo_busy(canonical))
            self.assertFalse(state.store.child_busy(child))
            self.assertFalse(state.store.child_busy(subtree))
            target(cleanup_job)
            if not cleanup_job.terminal:
                registry.finish(cleanup_job, JobStatus.OK)
            return object()

        with mock.patch("core.smart_sync.runner.start_job_thread",
                        side_effect=run_cleanup):
            run_smart_sync_job(job, config)

        self.assertIn("align", events)
        self.assertIn("propagate", events)
        self.assertIn("subtree", events)
        self.assertIn("link:parent,canonical,subtree", events)
        self.assertEqual(header.status, "ok")
        self.assertEqual(header.message, "2 synced")
        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(len(cleanup_jobs), 1)
        self.assertEqual(cleanup_jobs[0].spec.kind, "smart-sync-cleanup")
        self.assertFalse(cleanup_jobs[0].spec.local_mutation)
        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(child))
        self.assertFalse(state.store.child_busy(subtree))
        self.assertFalse(state.tasks.has_running())
        cleanup_task = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ↳ smart-sync refresh cleanup")
        self.assertIs(state.job_registry.job_for_task(cleanup_task), cleanup_jobs[0])

    def test_cleanup_failure_warns_without_reopening_smart_sync_mutation(self) -> None:
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
            repo_keys=(str(parent.path), str(canonical.path)),
            child_keys=(str(child.nested_path),),
        ))
        lifecycle = SmartSyncLifecycle(state, header, job, [canonical], [])
        lifecycle.acquire()

        config = SmartSyncRunConfig(
            state=state,
            snapshot_repos=[parent, canonical],
            snapshot_subtrees=[],
            canonicals=[canonical],
            subtree_items=[],
            lifecycle=lifecycle,
            align_canonical=lambda _state, _canonical: (1, 0),
            propagate_parents=lambda _state, _canonicals: None,
            refresh_repo=lambda _repo: None,
            sync_subtree=lambda _path, _prefix, _remote, _branch:
                (True, "subtree ok"),
            link_siblings=lambda _repos, _subtrees:
                (_ for _ in ()).throw(RuntimeError("link failed")),
            first_line=lambda text: text.splitlines()[0] if text else "",
        )

        def run_cleanup(registry, cleanup_job, target):
            self.assertEqual(job.status, JobStatus.OK)
            self.assertFalse(state.job_registry.has_active_local_mutation())
            target(cleanup_job)
            if not cleanup_job.terminal:
                registry.finish(cleanup_job, JobStatus.OK)
            return object()

        with mock.patch("core.smart_sync.runner.start_job_thread",
                        side_effect=run_cleanup):
            run_smart_sync_job(job, config)

        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].status, JobStatus.OK)
        self.assertEqual(jobs[1].spec.kind, "smart-sync-cleanup")
        self.assertEqual(jobs[1].status, JobStatus.WARN)
        self.assertFalse(jobs[1].spec.local_mutation)
        self.assertFalse(state.job_registry.has_active_local_mutation())
        cleanup_task = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ↳ smart-sync refresh cleanup")
        self.assertEqual(cleanup_task.status, "warn")
        self.assertEqual(cleanup_task.message, "1 link failed")
        self.assertIs(state.job_registry.job_for_task(cleanup_task), jobs[1])

    def test_cancellation_releases_locks_and_skips_cleanup(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        state = State(repos=[parent, canonical], workspace_name="A")
        state.auto_push_submodule_parent = True
        header = state.tasks.add("smart-sync (1)")
        job = state.job_registry.start(JobSpec(
            kind="smart-sync",
            label=header.label,
            local_mutation=True,
            repo_keys=(str(parent.path), str(canonical.path)),
            child_keys=(str(child.nested_path),),
        ))
        lifecycle = SmartSyncLifecycle(state, header, job, [canonical], [])
        lifecycle.acquire()
        events = []

        def align(_state, _canonical):
            events.append("align")
            job.cancel_event.set()
            return 1, 0

        config = SmartSyncRunConfig(
            state=state,
            snapshot_repos=[parent, canonical],
            snapshot_subtrees=[],
            canonicals=[canonical],
            subtree_items=[],
            lifecycle=lifecycle,
            align_canonical=align,
            propagate_parents=lambda _state, _canonicals:
                events.append("propagate"),
            refresh_repo=lambda repo: events.append(f"refresh:{repo.rel}"),
            sync_subtree=lambda _path, _prefix, _remote, _branch:
                events.append("subtree") or (True, "subtree ok"),
            link_siblings=lambda _repos, _subtrees:
                events.append("cleanup"),
            first_line=lambda text: text.splitlines()[0] if text else "",
        )

        with mock.patch("core.smart_sync.runner.start_job_thread") as cleanup:
            run_smart_sync_job(job, config)

        cleanup.assert_not_called()
        self.assertEqual(events, ["align", "refresh:canonical"])
        self.assertEqual(job.status, JobStatus.CANCELLED)
        self.assertEqual(job.message, "cancelled")
        self.assertEqual(header.status, "warn")
        self.assertEqual(header.message, "cancelled")
        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(child))
        self.assertFalse(state.tasks.has_running())


if __name__ == "__main__":
    unittest.main()
