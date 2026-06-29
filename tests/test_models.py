"""Tests for pure model dataclasses and state-owned task projections."""
from __future__ import annotations

import sys
import threading
import unittest
from unittest import mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.action_menu import FileEntry  # noqa: E402
from core.state.repos import ChildRef, Repo, WorkflowInfo  # noqa: E402
from core.state.review import WorkflowToggle  # noqa: E402
from core.state.workspaces import SubtreeSpec  # noqa: E402
from core.state.selectors import (  # noqa: E402
    active_workspace_child_rows,
    active_workspace_repo_rows,
)
from core.runtime.tasks import (  # noqa: E402
    TASK_AUTO_REMOVE_PROGRESS_SECONDS, Tasks,
)


class TestRepoBasics(unittest.TestCase):
    def test_display_name_named_repo(self) -> None:
        r = _make_repo("Upskill.Health.API")
        self.assertEqual(r.display_name, "Upskill.Health.API")

    def test_display_name_root(self) -> None:
        r = Repo(rel=".", path=Path("/tmp/Workspace.Root"))
        self.assertEqual(r.display_name, "Workspace.Root (root)")

    def test_is_dirty_false_when_empty(self) -> None:
        r = _make_repo()
        self.assertFalse(r.is_dirty)

    def test_is_dirty_with_staged(self) -> None:
        r = _make_repo()
        r.staged = [("M", "foo.cs")]
        self.assertTrue(r.is_dirty)

    def test_is_dirty_with_unstaged(self) -> None:
        r = _make_repo()
        r.unstaged = [("M", "foo.cs")]
        self.assertTrue(r.is_dirty)

    def test_is_dirty_with_untracked(self) -> None:
        r = _make_repo()
        r.untracked = ["new.cs"]
        self.assertTrue(r.is_dirty)


class TestStateSelectableRows(unittest.TestCase):
    def test_commit_suggestion_defaults_are_five(self) -> None:
        s = State(repos=[], workspace_name="ws")
        self.assertEqual(s.suggest_added, 5)
        self.assertEqual(s.suggest_updated, 5)
        self.assertEqual(s.suggest_deleted, 5)

    def test_empty_repo_list_yields_no_body_rows(self) -> None:
        # The 3 commit/sync toggles that used to live as body rows
        # 0..2 moved into the workspace menu — selectable_rows() now
        # contains body rows only.
        s = State(repos=[], workspace_name="ws")
        rows = s.selectable_rows()
        self.assertEqual(rows, [])
        self.assertEqual(s.total_rows, 0)

    def test_repos_only(self) -> None:
        repos = [_make_repo("a"), _make_repo("b")]
        s = State(repos=repos, workspace_name="ws")
        rows = s.selectable_rows()
        self.assertEqual([r[0] for r in rows], ["repo", "repo"])
        self.assertEqual(s.total_rows, 2)

    def test_children_interleaved(self) -> None:
        a = _make_repo("a")
        b = _make_repo("b")
        c = _make_repo("c")
        a.children = [
            ChildRef(repo=c, nested_path=Path("/tmp/a/c"), kind="submodule"),
        ]
        b.children = [
            ChildRef(repo=c, nested_path=Path("/tmp/b/c"), kind="submodule"),
            ChildRef(repo=c, nested_path=Path("/tmp/b/c2"), kind="subtree"),
        ]
        s = State(repos=[a, b], workspace_name="ws")
        rows = s.selectable_rows()
        kinds = [r[0] for r in rows]
        self.assertEqual(kinds,
                         ["repo", "child", "repo", "child", "child"])
        self.assertEqual(s.total_rows, 5)

    def test_active_workspace_rows_read_store_membership(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        s = State(repos=[parent, canonical], workspace_name="ws")

        s.repos = []

        self.assertEqual(active_workspace_repo_rows(s), [parent, canonical])
        self.assertEqual(active_workspace_child_rows(s), [(parent, child)])
        self.assertEqual(
            s.selectable_rows(),
            [("repo", parent, None), ("child", parent, child),
             ("repo", canonical, None)],
        )

    def test_restore_body_focus_by_path_not_index(self) -> None:
        """After refresh the list can gain submodule rows; index-only
        clamping used to leave the cursor on the wrong row or snap to 0."""
        a = _make_repo("a")
        b = _make_repo("b")
        s = State(repos=[a, b], workspace_name="ws", selected=1)
        key = s.body_focus_key()
        self.assertEqual(key, ("repo", b.path))
        # Simulate refresh: same repos but `a` gained a child row above `b`.
        c = _make_repo("c")
        a.children = [
            ChildRef(repo=c, nested_path=Path("/tmp/a/c"), kind="submodule"),
        ]
        s.replace_repos([a, b])
        s.restore_body_focus(key)
        self.assertIs(s.current_repo, b)
        self.assertEqual(s.selected, 2)

    def test_restore_body_focus_keeps_pseudo_rows(self) -> None:
        s = State(repos=[_make_repo("a")], workspace_name="ws", selected=-1)
        self.assertIsNone(s.body_focus_key())
        s.restore_body_focus(None)
        self.assertEqual(s.selected, -1)

    def test_current_repo_returns_focused(self) -> None:
        a = _make_repo("a")
        b = _make_repo("b")
        s = State(repos=[a, b], workspace_name="ws", selected=0)
        self.assertIs(s.current_repo, a)
        s.selected = 1
        self.assertIs(s.current_repo, b)
        s.selected = -1  # workspace row
        self.assertIsNone(s.current_repo)

    def test_current_child_returns_parent_and_ref(self) -> None:
        a = _make_repo("a")
        c = _make_repo("c")
        ref = ChildRef(repo=c, nested_path=Path("/tmp/a/c"), kind="submodule")
        a.children = [ref]
        s = State(repos=[a], workspace_name="ws", selected=1)
        result = s.current_child
        self.assertIsNotNone(result)
        parent, child = result
        self.assertIs(parent, a)
        self.assertIs(child, ref)

    def test_current_child_none_on_repo_row(self) -> None:
        a = _make_repo("a")
        s = State(repos=[a], workspace_name="ws", selected=0)
        self.assertIsNone(s.current_child)


class TestStateHasMessages(unittest.TestCase):
    def test_no_messages(self) -> None:
        s = State(repos=[_make_repo("a"), _make_repo("b")], workspace_name="ws")
        self.assertFalse(s.has_messages)

    def test_repo_with_message(self) -> None:
        a = _make_repo("a")
        s = State(repos=[a], workspace_name="ws")
        s.store.set_row_message(a, "fix")
        self.assertTrue(s.has_messages)

    def test_whitespace_only_does_not_count(self) -> None:
        a = _make_repo("a")
        a.message = "   \t\n  "
        s = State(repos=[a], workspace_name="ws")
        self.assertFalse(s.has_messages)

    def test_child_with_message_counts(self) -> None:
        parent = _make_repo("parent")
        c = _make_repo("c")
        ref = ChildRef(repo=c, nested_path=Path("/tmp/p/c"), kind="submodule")
        parent.children = [ref]
        s = State(repos=[parent], workspace_name="ws")
        s.store.set_row_message(ref, "fix nested")
        self.assertTrue(s.has_messages)

    def test_has_messages_reads_store_snapshot_not_raw_repo(self) -> None:
        a = _make_repo("a")
        s = State(repos=[a], workspace_name="ws")

        a.message = "raw only"

        self.assertFalse(s.has_messages)
        s.store.publish_row_status(a)
        self.assertFalse(s.has_messages)
        s.store.set_row_message(a, "store draft")
        self.assertTrue(s.has_messages)

    def test_has_messages_reads_store_snapshot_not_raw_child(self) -> None:
        parent = _make_repo("parent")
        c = _make_repo("c")
        ref = ChildRef(repo=c, nested_path=Path("/tmp/p/c"), kind="submodule")
        parent.children = [ref]
        s = State(repos=[parent, c], workspace_name="ws")

        ref.message = "raw nested"

        self.assertFalse(s.has_messages)
        s.store.publish_row_status(ref)
        self.assertFalse(s.has_messages)
        s.store.set_row_message(ref, "store nested")
        self.assertTrue(s.has_messages)


class TestStateOnWorkspaceRow(unittest.TestCase):
    def test_sentinel_minus_one_is_workspace_row(self) -> None:
        # Body rows occupy 0..N-1 directly now (no toggle prelude).
        # selected = -1 is the title-row workspace selector.
        s = State(repos=[_make_repo("a")], workspace_name="ws")
        s.selected = -1
        self.assertTrue(s.on_workspace_row)
        s.selected = 0
        self.assertFalse(s.on_workspace_row)


class TestSubtreeSpec(unittest.TestCase):
    def test_field_set(self) -> None:
        spec = SubtreeSpec(name="x", parent="p", source="s", prefix="vendor/x")
        self.assertEqual(spec.name, "x")
        self.assertEqual(spec.parent, "p")
        self.assertEqual(spec.source, "s")
        self.assertEqual(spec.prefix, "vendor/x")


class TestTasksQueue(unittest.TestCase):
    def test_add_returns_running_task(self) -> None:
        tasks = Tasks()
        t = tasks.add("commit foo")
        self.assertEqual(t.label, "commit foo")
        self.assertEqual(t.status, "running")
        self.assertEqual(t.message, "")
        self.assertEqual(len(tasks.snapshot()), 1)

    def test_update_sets_status_and_message(self) -> None:
        tasks = Tasks()
        t = tasks.add("commit foo")
        tasks.update(t, "ok")
        self.assertEqual(t.status, "ok")
        tasks.update(t, "fail", "remote rejected")
        self.assertEqual(t.status, "fail")
        self.assertEqual(t.message, "remote rejected")

    def test_task_changes_fire_change_callback(self) -> None:
        changes = []
        tasks = Tasks()
        tasks.on_change = lambda: changes.append("changed")

        t = tasks.add("commit foo")
        tasks.update(t, "ok")
        tasks.remove(t)

        self.assertEqual(changes, ["changed", "changed", "changed"])

    def test_state_wires_task_and_job_changes_to_ui_events(self) -> None:
        state = State(repos=[], workspace_name="ws")

        self.assertFalse(state.ui_events.is_set())
        state.tasks.add("work")
        self.assertTrue(state.ui_events.drain())
        self.assertFalse(state.ui_events.drain())

        job = state.job_registry.start(JobSpec(kind="refresh", label="refresh"))
        self.assertTrue(state.ui_events.drain())
        state.job_registry.finish(job, JobStatus.OK)
        self.assertTrue(state.ui_events.drain())

    def test_update_empty_message_does_not_clobber(self) -> None:
        tasks = Tasks()
        t = tasks.add("commit foo")
        tasks.update(t, "running", "in progress")
        tasks.update(t, "ok")  # status flip with empty message
        self.assertEqual(t.status, "ok")
        self.assertEqual(t.message, "in progress")

    def test_has_running(self) -> None:
        tasks = Tasks()
        self.assertFalse(tasks.has_running())
        a = tasks.add("a")
        self.assertTrue(tasks.has_running())
        tasks.update(a, "ok")
        self.assertFalse(tasks.has_running())

    def test_prune_completed_keeps_running(self) -> None:
        tasks = Tasks()
        a = tasks.add("a")
        b = tasks.add("b")
        c = tasks.add("c")
        tasks.update(a, "ok")
        tasks.update(c, "fail")
        tasks.prune_completed()
        snap = tasks.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertIs(snap[0], b)

    def test_snapshot_returns_a_copy(self) -> None:
        tasks = Tasks()
        tasks.add("a")
        snap = tasks.snapshot()
        tasks.add("b")  # mutate after snapshot
        self.assertEqual(len(snap), 1)
        self.assertEqual(len(tasks.snapshot()), 2)

class TestTasksThreadSafety(unittest.TestCase):
    def test_concurrent_add_and_update(self) -> None:
        tasks = Tasks()
        n_workers = 16
        per_worker = 50

        def work(idx: int) -> None:
            for j in range(per_worker):
                t = tasks.add(f"w{idx}-task{j}")
                tasks.update(t, "ok" if (j % 2 == 0) else "fail",
                             f"detail {idx}-{j}")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n_workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        snap = tasks.snapshot()
        self.assertEqual(len(snap), n_workers * per_worker)
        # Every entry should have been moved off "running" by its updater.
        self.assertFalse(any(t.status == "running" for t in snap))


class TestTaskFinishedAt(unittest.TestCase):
    def test_started_at_set_on_construction(self) -> None:
        tasks = Tasks()
        t = tasks.add("a")
        self.assertGreater(t.started_at, 0)
        self.assertIsNone(t.finished_at)

    def test_finished_at_stamps_on_terminal_transition(self) -> None:
        tasks = Tasks()
        t = tasks.add("a")
        tasks.update(t, "ok")
        self.assertIsNotNone(t.finished_at)
        self.assertGreaterEqual(t.finished_at, t.started_at)

    def test_finished_at_unchanged_on_subsequent_terminal_updates(self) -> None:
        # If a worker accidentally updates a finished task again, the
        # auto-remove window should keep ticking from the *first* terminal
        # transition, not reset.
        tasks = Tasks()
        t = tasks.add("a")
        tasks.update(t, "ok")
        first = t.finished_at
        # Simulate later update with a different status.
        tasks.update(t, "fail", "late failure")
        self.assertEqual(t.finished_at, first)

    def test_set_label_mutates_in_place(self) -> None:
        tasks = Tasks()
        t = tasks.add("step 1")
        tasks.set_label(t, "step 2")
        self.assertEqual(t.label, "step 2")

    def test_clear_message_removes_stale_placeholder_text(self) -> None:
        tasks = Tasks()
        t = tasks.add("step")
        tasks.update(t, "pending", "waiting on parent")
        tasks.update(t, "running", "")
        self.assertEqual(t.message, "waiting on parent")
        tasks.clear_message(t)
        self.assertEqual(t.message, "")


class TestTaskPendingStatus(unittest.TestCase):
    """The 'pending' status is non-terminal: chained then-run
    placeholders sit in pending while waiting on a parent run, and the
    spinner / redraw loop must keep ticking until they transition."""

    def test_has_running_treats_pending_as_active(self) -> None:
        tasks = Tasks()
        t = tasks.add("chained")
        tasks.update(t, "pending", "waiting on parent")
        self.assertEqual(t.status, "pending")
        self.assertTrue(tasks.has_running())
        self.assertTrue(tasks.has_visible_activity())
        self.assertTrue(tasks.has_pending_followups())

    def test_pending_to_running_does_not_stamp_finished_at(self) -> None:
        # Both are non-terminal; only the first transition INTO a
        # terminal status (ok/fail/warn) should stamp finished_at.
        tasks = Tasks()
        t = tasks.add("chained")
        tasks.update(t, "pending")
        tasks.update(t, "running")
        self.assertIsNone(t.finished_at)

    def test_pending_to_terminal_stamps_finished_at(self) -> None:
        tasks = Tasks()
        t = tasks.add("chained")
        tasks.update(t, "pending")
        tasks.update(t, "warn", "skipped — parent didn't succeed")
        self.assertIsNotNone(t.finished_at)
        self.assertGreaterEqual(t.finished_at, t.started_at)


class TestTasksPruneAged(unittest.TestCase):
    def test_negative_interval_is_noop(self) -> None:
        tasks = Tasks()
        t = tasks.add("a")
        tasks.update(t, "ok")
        # Force finished_at to a stale value to confirm it's still kept.
        t.finished_at = 0.0
        n = tasks.prune_aged(-1)
        self.assertEqual(n, 0)
        self.assertEqual(len(tasks.snapshot()), 1)

    def test_keeps_running_tasks_indefinitely(self) -> None:
        tasks = Tasks()
        tasks.add("running1")
        tasks.add("running2")
        n = tasks.prune_aged(0)
        self.assertEqual(n, 0)
        self.assertEqual(len(tasks.snapshot()), 2)

    def test_prunes_success_tasks_after_wait_and_progress(self) -> None:
        tasks = Tasks()
        old = tasks.add("old")
        recent = tasks.add("recent")
        with mock.patch("core.runtime.tasks._monotonic", return_value=100.0):
            tasks.update(old, "ok")
        with mock.patch("core.runtime.tasks._monotonic", return_value=104.0):
            tasks.update(recent, "ok")
        with mock.patch("core.runtime.tasks._monotonic", return_value=105.0):
            n = tasks.prune_aged(2.0)
        self.assertEqual(n, 1)
        snap = tasks.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertIs(snap[0], recent)

    def test_zero_interval_still_allows_progress_window(self) -> None:
        # 0 means "start removing on next tick"; the 3s progress
        # animation still gets time to render before the row drops.
        tasks = Tasks()
        t = tasks.add("a")
        with mock.patch("core.runtime.tasks._monotonic", return_value=100.0):
            tasks.update(t, "ok")
        with mock.patch("core.runtime.tasks._monotonic", return_value=100.0):
            n = tasks.prune_aged(0)
        self.assertEqual(n, 0)
        self.assertEqual(len(tasks.snapshot()), 1)
        with mock.patch(
                "core.runtime.tasks._monotonic",
                return_value=100.0 + TASK_AUTO_REMOVE_PROGRESS_SECONDS):
            n = tasks.prune_aged(0)
        self.assertEqual(n, 1)
        self.assertEqual(len(tasks.snapshot()), 0)

    def test_prune_aged_keeps_failed_and_warning_tasks(self) -> None:
        tasks = Tasks()
        failed = tasks.add("failed")
        warning = tasks.add("warning")
        tasks.update(failed, "fail")
        tasks.update(warning, "warn")
        failed.finished_at = 0.0
        warning.finished_at = 0.0
        self.assertEqual(tasks.prune_aged(0), 0)
        self.assertEqual(tasks.snapshot(), [failed, warning])

    def test_has_pending_auto_remove(self) -> None:
        tasks = Tasks()
        # Negative window: never pending.
        t = tasks.add("a")
        tasks.update(t, "ok")
        self.assertFalse(tasks.has_pending_auto_remove(-1))
        with mock.patch(
                "core.runtime.tasks._monotonic",
                return_value=t.finished_at):
            self.assertFalse(tasks.has_pending_auto_remove(60))
        with mock.patch(
                "core.runtime.tasks._monotonic",
                return_value=t.finished_at + 60.1):
            self.assertTrue(tasks.has_pending_auto_remove(60))
        with mock.patch(
                "core.runtime.tasks._monotonic",
                return_value=(
                    t.finished_at + 60
                    + TASK_AUTO_REMOVE_PROGRESS_SECONDS + 0.1)):
            self.assertFalse(tasks.has_pending_auto_remove(60))

    def test_has_pending_auto_remove_skips_running(self) -> None:
        tasks = Tasks()
        tasks.add("a")  # still running
        self.assertFalse(tasks.has_pending_auto_remove(60))


class TestWorkflowDataStructures(unittest.TestCase):
    def test_workflow_info_defaults(self) -> None:
        wf = WorkflowInfo(name="CI", path=".github/workflows/ci.yml")
        self.assertEqual(wf.name, "CI")
        self.assertEqual(wf.state, "")
        self.assertFalse(wf.dispatchable)

    def test_workflow_info_dispatchable(self) -> None:
        wf = WorkflowInfo(name="Release",
                          path=".github/workflows/release.yml",
                          state="active", dispatchable=True)
        self.assertTrue(wf.dispatchable)

    def test_repo_workflows_default_empty(self) -> None:
        r = _make_repo("a")
        self.assertEqual(r.workflows, [])

    def test_repo_does_not_own_workflow_intent(self) -> None:
        r = _make_repo("a")
        self.assertFalse(hasattr(r, "track_workflow"))
        self.assertFalse(hasattr(r, "then_run_after_push"))
        self.assertFalse(hasattr(r, "then_run_after_workflow"))
        self.assertFalse(hasattr(r, "then_run_params_after_push"))
        self.assertFalse(hasattr(r, "then_run_params_after_workflow"))

    def test_workflow_toggle_keeps_repo_reference(self) -> None:
        r = _make_repo("a")
        toggle = WorkflowToggle(repo=r, workflow_name="CI", line_index=5)
        self.assertIs(toggle.repo, r)
        self.assertEqual(toggle.workflow_name, "CI")
        self.assertEqual(toggle.line_index, 5)


class TestTaskHierarchy(unittest.TestCase):
    """`Task.parent` + `Tasks.children_of` give the task-detail modal a
    way to enumerate sub-tasks of a workflow run / push step without
    parsing label whitespace."""

    def test_parent_defaults_to_none(self) -> None:
        tasks = Tasks()
        t = tasks.add("standalone")
        self.assertIsNone(t.parent)

    def test_add_with_parent_records_back_pointer(self) -> None:
        tasks = Tasks()
        run = tasks.add("workflow run")
        job = tasks.add("  ↳ build", parent=run)
        self.assertIs(job.parent, run)

    def test_children_of_returns_only_direct_children(self) -> None:
        tasks = Tasks()
        run = tasks.add("workflow run")
        a = tasks.add("  ↳ build", parent=run)
        b = tasks.add("  ↳ test", parent=run)
        tasks.add("commit foo")  # unrelated
        children = tasks.children_of(run)
        # Order matches the underlying items list; Task isn't hashable
        # (default-eq dataclass), so compare by identity.
        self.assertEqual(len(children), 2)
        self.assertIs(children[0], a)
        self.assertIs(children[1], b)
        self.assertEqual(tasks.children_of(a), [])

    def test_children_of_empty_when_task_has_no_kids(self) -> None:
        tasks = Tasks()
        t = tasks.add("solo")
        self.assertEqual(tasks.children_of(t), [])


class TestWorkflowRunRegistry(unittest.TestCase):
    def test_state_wires_runtime_registries_to_ui_events(self) -> None:
        state = State(repos=[], workspace_name="ws")
        task = state.tasks.add("run")
        state.ui_events.drain()

        state.workflow_runs.create_for_task(task, slug="o/r", run_id=42)
        self.assertTrue(state.ui_events.drain())

        state.workflow_followups.create_for_task(task, target="Deploy")
        self.assertTrue(state.ui_events.drain())

        state.review_drafts.create("repo:/tmp/repo")
        self.assertTrue(state.ui_events.drain())

        state.view_loads.create("view:diff")
        self.assertTrue(state.ui_events.drain())

    def test_create_for_task_creates_subject_and_updates_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        task = state.tasks.add("run")
        repo = Repo("repo", Path("/tmp/repo"))

        record = state.workflow_runs.create_for_task(
            task,
            repo=repo,
            slug="o/r",
            run_id=42,
            workflow_name="Build",
        )

        self.assertEqual(task.subject_kind, "workflow-run")
        self.assertEqual(task.subject_id, record.record_id)
        self.assertIs(state.workflow_runs.get(record.record_id), record)
        self.assertIs(state.workflow_runs.record_for_task(task), record)
        self.assertIs(record.repo, repo)
        self.assertEqual(record.slug, "o/r")
        self.assertEqual(record.run_id, 42)
        self.assertEqual(record.workflow_name, "Build")

        state.workflow_runs.update(record.record_id,
                                   run_url="https://example/runs/42")
        self.assertEqual(record.run_url, "https://example/runs/42")

    def test_remove_drops_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        task = state.tasks.add("run")
        record = state.workflow_runs.create_for_task(
            task, slug="o/r", run_id=42)

        state.workflow_runs.remove(record.record_id)

        self.assertIsNone(state.workflow_runs.get(record.record_id))
        self.assertIsNone(state.workflow_runs.record_for_task(task))


class TestWorkflowFollowupRegistry(unittest.TestCase):
    def test_create_for_task_creates_subject_and_updates_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        task = state.tasks.add("then run")
        repo = Repo("repo", Path("/tmp/repo"))

        record = state.workflow_followups.create_for_task(
            task,
            repo=repo,
            parent_workflow="Build",
            target="Deploy",
        )

        self.assertEqual(task.subject_kind, "workflow-followup")
        self.assertEqual(task.subject_id, record.record_id)
        self.assertIs(state.workflow_followups.get(record.record_id), record)
        self.assertIs(state.workflow_followups.record_for_task(task), record)
        self.assertIs(record.repo, repo)
        self.assertEqual(record.parent_workflow, "Build")
        self.assertEqual(record.target, "Deploy")

        state.workflow_followups.update(record.record_id, target="Release")
        self.assertEqual(record.target, "Release")

    def test_remove_drops_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        task = state.tasks.add("then run")
        record = state.workflow_followups.create_for_task(
            task, target="Deploy")

        state.workflow_followups.remove(record.record_id)

        self.assertIsNone(state.workflow_followups.get(record.record_id))
        self.assertIsNone(state.workflow_followups.record_for_task(task))


class TestReviewDraftRegistry(unittest.TestCase):
    def test_set_files_updates_draft_state(self) -> None:
        state = State(repos=[], workspace_name="ws")
        files = [
            FileEntry(path="a.txt", x="M", y=" "),
            FileEntry(path="b.txt", untracked=True),
        ]

        record = state.review_drafts.set_files(
            "repo:/tmp/repo",
            files,
            {"a.txt": True, "b.txt": False},
        )

        self.assertEqual(record.draft_id, "repo:/tmp/repo")
        self.assertEqual(record.files, files)
        self.assertEqual(record.staged_paths, {
            "a.txt": True,
            "b.txt": False,
        })
        self.assertFalse(record.files_loading)
        self.assertEqual(
            state.review_drafts.snapshot_staged("repo:/tmp/repo"),
            {"a.txt": True, "b.txt": False},
        )

    def test_set_staged_and_set_all_staged_update_existing_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        files = [FileEntry(path="a.txt"), FileEntry(path="b.txt")]
        state.review_drafts.set_files(
            "repo:/tmp/repo",
            files,
            {"a.txt": False, "b.txt": True},
        )

        state.review_drafts.set_staged("repo:/tmp/repo", "a.txt", True)
        state.review_drafts.set_all_staged("repo:/tmp/repo", False)

        self.assertEqual(
            state.review_drafts.snapshot_staged("repo:/tmp/repo"),
            {"a.txt": False, "b.txt": False},
        )

    def test_push_and_amend_are_draft_owned(self) -> None:
        state = State(repos=[], workspace_name="ws")
        record = state.review_drafts.create(
            "repo:/tmp/repo", push=False, amend=True)

        self.assertFalse(record.push)
        self.assertTrue(record.amend)

    def test_suggesting_is_draft_owned(self) -> None:
        state = State(repos=[], workspace_name="ws")

        state.review_drafts.set_suggesting("repo:/tmp/repo", True)
        record = state.review_drafts.get_or_create("repo:/tmp/repo")

        self.assertTrue(record.suggesting)
        state.review_drafts.set_suggesting("repo:/tmp/repo", False)
        self.assertFalse(record.suggesting)

        state.review_drafts.set_push("repo:/tmp/repo", True)
        state.review_drafts.set_amend("repo:/tmp/repo", False)

        updated = state.review_drafts.get("repo:/tmp/repo")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertTrue(updated.push)
        self.assertFalse(updated.amend)

    def test_message_is_draft_owned(self) -> None:
        state = State(repos=[], workspace_name="ws")
        record = state.review_drafts.create(
            "repo:/tmp/repo", message="initial")

        self.assertEqual(record.message, "initial")

        state.review_drafts.set_message("repo:/tmp/repo", "updated")

        updated = state.review_drafts.get("repo:/tmp/repo")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.message, "updated")

    def test_workflow_intent_is_draft_owned(self) -> None:
        state = State(repos=[], workspace_name="ws")

        state.review_drafts.set_track_workflow("repo:/tmp/repo", "CI", True)
        state.review_drafts.set_then_run("repo:/tmp/repo", "", "Deploy")
        state.review_drafts.set_then_run("repo:/tmp/repo", "CI", "Release")
        state.review_drafts.set_then_run_param(
            "repo:/tmp/repo", "", "environment", "staging")
        state.review_drafts.set_then_run_param(
            "repo:/tmp/repo", "CI", "tag", "v1")

        record = state.review_drafts.get("repo:/tmp/repo")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.track_workflow, {"CI": True})
        self.assertEqual(record.then_run_after_push, "Deploy")
        self.assertEqual(record.then_run_after_workflow, {"CI": "Release"})
        self.assertEqual(record.then_run_params_after_push, {
            "environment": "staging",
        })
        self.assertEqual(record.then_run_params_after_workflow, {
            "CI": {"tag": "v1"},
        })

        state.review_drafts.clear_workflow_intent("repo:/tmp/repo")

        self.assertEqual(record.track_workflow, {})
        self.assertEqual(record.then_run_after_push, "")
        self.assertEqual(record.then_run_after_workflow, {})
        self.assertEqual(record.then_run_params_after_push, {})
        self.assertEqual(record.then_run_params_after_workflow, {})


class TestViewLoadRegistry(unittest.TestCase):
    def test_finish_records_lines_details_and_clears_loading(self) -> None:
        state = State(repos=[], workspace_name="ws")

        state.view_loads.create("view:diff")
        record = state.view_loads.finish(
            "view:diff", ["line 1"], details={"tab": "diff"})

        self.assertEqual(record.lines, ["line 1"])
        self.assertEqual(record.details, {"tab": "diff"})
        self.assertFalse(record.loading)
        self.assertEqual(state.view_loads.snapshot("view:diff"),
                         (["line 1"], False, ""))
        self.assertEqual(state.view_loads.details("view:diff"),
                         {"tab": "diff"})

    def test_cancel_marks_record_cancelled_and_not_loading(self) -> None:
        state = State(repos=[], workspace_name="ws")
        state.view_loads.create("view:diff")

        state.view_loads.cancel("view:diff")

        self.assertTrue(state.view_loads.is_cancelled("view:diff"))
        self.assertEqual(state.view_loads.snapshot("view:diff"),
                         ([], False, ""))

    def test_remove_many_cancels_and_drops_records(self) -> None:
        state = State(repos=[], workspace_name="ws")
        state.view_loads.create("view:diff")
        state.view_loads.create("view:log")

        state.view_loads.remove_many(["view:diff", "view:log"])

        self.assertFalse(state.view_loads.any_loading([
            "view:diff", "view:log"]))
        self.assertTrue(state.view_loads.is_cancelled("view:diff"))
        self.assertEqual(state.view_loads.snapshot("view:diff"),
                         ([], True, ""))

    def test_late_finish_after_remove_does_not_recreate_record(self) -> None:
        state = State(repos=[], workspace_name="ws")
        state.view_loads.create("view:diff")

        state.view_loads.remove_many(["view:diff"])
        record = state.view_loads.finish("view:diff", ["stale line"])

        self.assertTrue(record.cancel_event.is_set())
        self.assertEqual(state.view_loads.snapshot("view:diff"),
                         ([], True, ""))

    def test_create_clears_sticky_cancellation_for_reused_load_id(self) -> None:
        state = State(repos=[], workspace_name="ws")
        state.view_loads.create("view:diff")
        state.view_loads.remove_many(["view:diff"])

        state.view_loads.create("view:diff")

        self.assertFalse(state.view_loads.is_cancelled("view:diff"))
        self.assertEqual(state.view_loads.snapshot("view:diff"),
                         ([], True, ""))


if __name__ == "__main__":
    unittest.main()
