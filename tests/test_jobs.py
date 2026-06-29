"""Job registry and runner tests."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.jobs import (  # noqa: E402
    Job,
    JobRegistry,
    JobSpec,
    JobStatus,
    submit_job,
)
from core.runtime.task_actions import task_can_remove  # noqa: E402
from core.runtime.tasks import Tasks  # noqa: E402


class TestJobRegistry(unittest.TestCase):
    def test_start_assigns_incrementing_ids(self) -> None:
        registry = JobRegistry()

        a = registry.start(JobSpec(kind="refresh", label="a"))
        b = registry.start(JobSpec(kind="commit", label="b"))

        self.assertEqual(a.job_id, 1)
        self.assertEqual(b.job_id, 2)
        self.assertEqual(registry.snapshot(), [a, b])

    def test_finish_is_terminal_once(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="commit", label="commit"))

        self.assertTrue(registry.finish(job, JobStatus.OK, "done"))
        self.assertFalse(registry.finish(job, JobStatus.FAIL, "late"))

        self.assertEqual(job.status, JobStatus.OK)
        self.assertEqual(job.message, "done")
        self.assertIsNotNone(job.finished_at)

    def test_change_callback_fires_on_start_finish_and_cancel(self) -> None:
        changes = []
        registry = JobRegistry(on_change=lambda: changes.append("changed"))
        job = registry.start(JobSpec(kind="commit", label="commit"))

        registry.request_cancel(job)
        registry.finish(job, JobStatus.CANCELLED)
        registry.finish(job, JobStatus.FAIL, "late")

        self.assertEqual(changes, ["changed", "changed", "changed"])

    def test_warning_is_terminal(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="tag", label="tag"))

        self.assertTrue(registry.finish(job, JobStatus.WARN, "local only"))
        self.assertTrue(job.terminal)
        self.assertFalse(registry.finish(job, JobStatus.OK, "late"))
        self.assertEqual(job.status, JobStatus.WARN)

    def test_active_local_mutation_tracks_non_terminal_mutating_jobs(self) -> None:
        registry = JobRegistry()
        refresh = registry.start(
            JobSpec(kind="refresh", label="refresh", local_mutation=False))
        commit = registry.start(
            JobSpec(kind="commit", label="commit", local_mutation=True))

        self.assertTrue(registry.has_active_local_mutation())
        self.assertEqual(registry.active_local_mutation_jobs(), [commit])

        registry.finish(refresh, JobStatus.OK)
        self.assertTrue(registry.has_active_local_mutation())

        registry.finish(commit, JobStatus.OK)
        self.assertFalse(registry.has_active_local_mutation())
        self.assertEqual(registry.active_local_mutation_jobs(), [])

    def test_active_local_mutation_matches_child_targets_to_parent_repo(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(
            kind="child-push",
            label="child",
            local_mutation=True,
            child_keys=("/workspace/repo/vendor/sdk",),
        ))

        self.assertTrue(registry.has_active_local_mutation_for(
            repo_keys=("/workspace/repo",)))
        self.assertFalse(registry.has_active_local_mutation_for(
            repo_keys=("/workspace/other",)))

        registry.finish(job, JobStatus.OK)
        self.assertFalse(registry.has_active_local_mutation_for(
            repo_keys=("/workspace/repo",)))

    def test_active_local_mutation_matches_parent_repo_to_child_targets(self) -> None:
        registry = JobRegistry()
        registry.start(JobSpec(
            kind="commit",
            label="commit",
            local_mutation=True,
            repo_keys=("/workspace/repo",),
        ))

        self.assertTrue(registry.has_active_local_mutation_for(
            child_keys=("/workspace/repo/vendor/sdk",)))
        self.assertFalse(registry.has_active_local_mutation_for(
            child_keys=("/workspace/other/vendor/sdk",)))

    def test_request_cancel_sets_event_without_terminalizing(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="push", label="push"))

        self.assertTrue(registry.request_cancel(job))
        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(job.terminal)

        registry.finish(job, JobStatus.CANCELLED)
        self.assertFalse(registry.request_cancel(job))

    def test_get_and_request_cancel_by_id(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="push", label="push"))

        self.assertIs(registry.get(job.job_id), job)
        self.assertIsNone(registry.get(999))
        self.assertTrue(registry.request_cancel_by_id(job.job_id))
        self.assertTrue(job.cancel_event.is_set())
        self.assertFalse(registry.request_cancel_by_id(999))

        registry.finish(job, JobStatus.CANCELLED)
        self.assertFalse(registry.request_cancel_by_id(job.job_id))

    def test_task_links_are_owned_by_job_registry(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="push", label="push"))
        task = object()

        self.assertTrue(registry.link_task(job, task))
        self.assertIs(registry.job_for_task(task), job)
        self.assertTrue(registry.link_task(job, task))
        self.assertEqual(job.task_keys, (id(task),))

        registry.finish(job, JobStatus.OK)
        self.assertFalse(registry.link_task(job, object()))

    def test_link_task_by_id_returns_false_for_missing_job(self) -> None:
        registry = JobRegistry()
        self.assertFalse(registry.link_task_by_id(42, object()))

    def test_job_for_task_prefers_active_owner_after_reuse(self) -> None:
        registry = JobRegistry()
        task = object()
        old_job = registry.start(JobSpec(kind="workflow-poll", label="poll"))
        registry.link_task(old_job, task)
        registry.finish(old_job, JobStatus.OK)
        new_job = registry.start(JobSpec(kind="workflow-dispatch", label="dispatch"))
        registry.link_task(new_job, task)

        self.assertIs(registry.job_for_task(task), new_job)

    def test_job_for_task_uses_latest_terminal_owner_after_reuse(self) -> None:
        registry = JobRegistry()
        task = object()
        old_job = registry.start(JobSpec(kind="workflow-poll", label="poll"))
        registry.link_task(old_job, task)
        registry.finish(old_job, JobStatus.OK)
        new_job = registry.start(JobSpec(kind="workflow-dispatch", label="dispatch"))
        registry.link_task(new_job, task)
        registry.finish(new_job, JobStatus.FAIL, "failed")

        self.assertIs(registry.job_for_task(task), new_job)

    def test_stale_jobs_report_active_jobs_without_finishing_them(self) -> None:
        registry = JobRegistry()
        stale = registry.start(JobSpec(
            kind="push",
            label="push repo",
            stale_after_seconds=5.0,
        ))
        fresh = registry.start(JobSpec(kind="refresh", label="refresh"))
        stale.started_at = 10.0
        fresh.started_at = 18.0

        self.assertEqual(registry.stale_jobs(now=20.0), [stale])
        self.assertFalse(stale.terminal)
        self.assertEqual(stale.status, JobStatus.RUNNING)

        registry.finish(stale, JobStatus.OK)
        self.assertEqual(registry.stale_jobs(now=30.0), [])

    def test_stale_jobs_can_use_default_threshold(self) -> None:
        registry = JobRegistry()
        job = registry.start(JobSpec(kind="refresh", label="refresh"))
        job.started_at = 10.0

        self.assertEqual(registry.stale_jobs(now=13.0), [])
        self.assertEqual(
            registry.stale_jobs(
                now=20.0,
                default_stale_after_seconds=5.0,
            ),
            [job],
        )

    def test_task_remove_projection_uses_active_job_over_task_status(self) -> None:
        class State:
            def __init__(self) -> None:
                self.job_registry = JobRegistry()

        state = State()
        tasks = Tasks()
        task = tasks.add("repo: working")
        tasks.update(task, "ok")
        job = state.job_registry.start(JobSpec(kind="commit", label="commit"))
        state.job_registry.link_task(job, task)

        self.assertFalse(task_can_remove(state, task))

    def test_task_remove_projection_uses_terminal_job_over_task_status(self) -> None:
        class State:
            def __init__(self) -> None:
                self.job_registry = JobRegistry()

        state = State()
        tasks = Tasks()
        task = tasks.add("repo: working")
        job = state.job_registry.start(JobSpec(kind="commit", label="commit"))
        state.job_registry.link_task(job, task)
        state.job_registry.finish(job, JobStatus.OK)

        self.assertTrue(task_can_remove(state, task))


class TestSubmitJob(unittest.TestCase):
    def _join(self, thread: threading.Thread) -> None:
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_successful_worker_finishes_ok(self) -> None:
        registry = JobRegistry()

        job, thread = submit_job(
            registry,
            JobSpec(kind="refresh", label="refresh"),
            lambda _job: None,
        )

        self.assertIsNotNone(thread)
        self._join(thread)
        self.assertEqual(job.status, JobStatus.OK)

    def test_worker_exception_finishes_failed(self) -> None:
        registry = JobRegistry()

        def boom(_job: Job) -> None:
            raise RuntimeError("boom")

        job, thread = submit_job(
            registry,
            JobSpec(kind="commit", label="commit"),
            boom,
        )

        self.assertIsNotNone(thread)
        self._join(thread)
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "boom")

    def test_cancelled_worker_finishes_cancelled(self) -> None:
        registry = JobRegistry()

        def cancel(job: Job) -> None:
            registry.request_cancel(job)

        job, thread = submit_job(
            registry,
            JobSpec(kind="push", label="push"),
            cancel,
        )

        self.assertIsNotNone(thread)
        self._join(thread)
        self.assertEqual(job.status, JobStatus.CANCELLED)

    def test_thread_start_failure_finishes_failed(self) -> None:
        registry = JobRegistry()

        def failing_factory(_target, _name):
            raise RuntimeError("thread start failed")

        job, thread = submit_job(
            registry,
            JobSpec(kind="remote-edit", label="remote"),
            lambda _job: None,
            thread_factory=failing_factory,
        )

        self.assertIsNone(thread)
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
