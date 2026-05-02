"""Tests for the dataclasses + Tasks queue in models.py — no git, no
curses. State and friends are pure value objects."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    ChildRef, Repo, State, SubtreeSpec, TaskMetadata, Tasks, WorkflowInfo,
    WorkflowToggle,
)


def _make_repo(rel: str = "myrepo", **kwargs) -> Repo:
    """Tiny Repo factory — no filesystem needed; the path is a phantom."""
    return Repo(rel=rel, path=Path(f"/tmp/{rel}"), **kwargs)


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
        a.message = "fix"
        s = State(repos=[a], workspace_name="ws")
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
        ref.message = "fix nested"
        parent.children = [ref]
        s = State(repos=[parent], workspace_name="ws")
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

    def test_prunes_tasks_older_than_interval(self) -> None:
        tasks = Tasks()
        old = tasks.add("old")
        recent = tasks.add("recent")
        tasks.update(old, "ok")
        tasks.update(recent, "ok")
        # Force the timestamps explicitly.
        old.finished_at = 0.0
        # `recent.finished_at` was just set by update() — leave it as-is.
        n = tasks.prune_aged(2.0)
        self.assertEqual(n, 1)
        snap = tasks.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertIs(snap[0], recent)

    def test_zero_interval_prunes_all_finished(self) -> None:
        # 0 means "remove on next tick" — anything completed should go.
        tasks = Tasks()
        t = tasks.add("a")
        tasks.update(t, "ok")
        n = tasks.prune_aged(0)
        # The just-finished task was finished ≈now, but elapsed >= 0 so
        # `< 0` is false and it gets pruned.
        self.assertEqual(n, 1)
        self.assertEqual(len(tasks.snapshot()), 0)

    def test_has_pending_auto_remove(self) -> None:
        tasks = Tasks()
        # Negative window: never pending.
        t = tasks.add("a")
        tasks.update(t, "ok")
        self.assertFalse(tasks.has_pending_auto_remove(-1))
        # Positive window: pending while inside.
        self.assertTrue(tasks.has_pending_auto_remove(60))
        # Force aging out.
        t.finished_at = 0.0
        self.assertFalse(tasks.has_pending_auto_remove(0.5))

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
        self.assertEqual(r.track_workflow, {})

    def test_repo_track_workflow_is_per_repo(self) -> None:
        a = _make_repo("a")
        b = _make_repo("b")
        a.track_workflow["CI"] = True
        # The dataclass field default_factory must give each Repo its own
        # dict (not a shared reference) — regression guard.
        self.assertNotIn("CI", b.track_workflow)

    def test_workflow_toggle_holds_repo_reference(self) -> None:
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


class TestTasksMetadata(unittest.TestCase):
    """`Tasks.set_meta` / `get_meta` keep workflow-tracking metadata off
    plain tasks but available to the task-detail modal when relevant."""

    def test_get_meta_returns_none_when_unset(self) -> None:
        tasks = Tasks()
        t = tasks.add("plain")
        self.assertIsNone(tasks.get_meta(t))

    def test_set_meta_creates_then_updates(self) -> None:
        tasks = Tasks()
        t = tasks.add("run")
        m = tasks.set_meta(t, run_id=42, workflow_name="CI")
        self.assertIsInstance(m, TaskMetadata)
        self.assertEqual(m.run_id, 42)
        self.assertEqual(m.workflow_name, "CI")
        # Subsequent set_meta updates the same entry.
        tasks.set_meta(t, run_url="https://example/runs/42")
        m2 = tasks.get_meta(t)
        self.assertIs(m2, m)
        self.assertEqual(m2.run_id, 42)  # preserved
        self.assertEqual(m2.run_url, "https://example/runs/42")  # added

    def test_remove_drops_metadata_entry(self) -> None:
        tasks = Tasks()
        t = tasks.add("run")
        tasks.set_meta(t, run_id=99)
        self.assertIsNotNone(tasks.get_meta(t))
        tasks.remove(t)
        self.assertIsNone(tasks.get_meta(t))

    def test_prune_aged_drops_metadata(self) -> None:
        tasks = Tasks()
        t = tasks.add("run")
        tasks.update(t, "ok")
        tasks.set_meta(t, run_id=7)
        # Force the timestamp into the past so prune_aged hits it.
        t.finished_at = 0.0
        tasks.prune_aged(0.1)
        self.assertIsNone(tasks.get_meta(t))

    def test_prune_completed_drops_metadata(self) -> None:
        tasks = Tasks()
        t = tasks.add("run")
        tasks.update(t, "ok")
        tasks.set_meta(t, run_id=7)
        tasks.prune_completed()
        self.assertIsNone(tasks.get_meta(t))


if __name__ == "__main__":
    unittest.main()
