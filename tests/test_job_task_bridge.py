"""Job task bridge tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.jobs import (  # noqa: E402
    JobSpec,
    JobStatus,
    JobTaskBridge,
    JobRegistry,
)
from core.runtime.tasks import Tasks  # noqa: E402


class TestJobTaskBridge(unittest.TestCase):
    def test_fail_outcome_overrides_warn(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="action", label="repo: push"))
        tasks = Tasks()
        bridge = JobTaskBridge(tasks)

        warn_task = bridge.add("repo: pull")
        bridge.update(warn_task, "warn", "cancelled")
        fail_task = bridge.add("repo: push")
        bridge.update(fail_task, "fail", "push failed")

        self.assertTrue(bridge.finish_failed_or_warned_job(registry, job))
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "push failed")

    def test_warn_outcome_finishes_job_warn(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="action", label="repo: checkout"))
        tasks = Tasks()
        bridge = JobTaskBridge(tasks)

        task = bridge.add("repo: checkout main")
        bridge.update(task, "warn", "would orphan commits")

        self.assertTrue(bridge.finish_failed_or_warned_job(registry, job))
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "would orphan commits")

    def test_ok_outcome_leaves_job_running_for_runner_default(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="action", label="repo: fetch"))
        tasks = Tasks()
        bridge = JobTaskBridge(tasks)

        task = bridge.add("repo: fetch")
        bridge.update(task, "ok")

        self.assertFalse(bridge.finish_failed_or_warned_job(registry, job))
        self.assertEqual(job.status, JobStatus.RUNNING)

    def test_bound_bridge_links_created_rows_to_job(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="smart-sync", label="smart-sync"))
        tasks = Tasks()
        bridge = JobTaskBridge(tasks, registry, job)

        parent = bridge.add("smart-sync")
        child = bridge.add("  ↳ smart-sync repo", parent=parent)

        self.assertIs(registry.job_for_task(parent), job)
        self.assertIs(registry.job_for_task(child), job)
        self.assertEqual(job.task_keys, (id(parent), id(child)))

    def test_bound_bridge_can_attach_existing_visible_header(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="smart-sync", label="smart-sync"))
        tasks = Tasks()
        header = tasks.add("smart-sync")
        bridge = JobTaskBridge(tasks, registry, job)

        self.assertTrue(bridge.attach(header))

        self.assertIs(registry.job_for_task(header), job)

    def test_bridge_requires_registry_and_job_together(self) -> None:
        registry = JobRegistry()
        tasks = Tasks()

        with self.assertRaises(ValueError):
            JobTaskBridge(tasks, registry=registry)

    def test_bound_bridge_links_rows_after_job_terminalizes(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="commit-batch", label="commit"))
        registry.finish(job, JobStatus.FAIL, "thread start failed")
        tasks = Tasks()
        bridge = JobTaskBridge(tasks, registry, job)

        task = bridge.add("commit supervisor")

        self.assertIs(registry.job_for_task(task), job)


if __name__ == "__main__":
    unittest.main()
