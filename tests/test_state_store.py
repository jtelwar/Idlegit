"""Tests for the runtime state store."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.ids import child_id, repo_id, workspace_id  # noqa: E402
from core.state.store import (  # noqa: E402
    ChildStatusSnapshot,
    ChildTopologySnapshot,
    RepoStatusSnapshot,
    StateStore,
    WorkflowIntentSnapshot,
)


class _ChildrenMustNotBeRead:
    def __iter__(self):
        raise AssertionError("parent.children should not be read")

    def __len__(self):
        raise AssertionError("parent.children should not be read")


class TestStateStore(unittest.TestCase):
    def test_ids_are_stable_from_workspace_and_paths(self) -> None:
        workspace = workspace_id("Health")
        repo_path = Path("/tmp/Health/API")
        child_path = repo_path / "SDK"

        self.assertEqual(repo_id(workspace, repo_path),
                         repo_id(workspace, Path("/tmp/Health/API")))
        self.assertEqual(child_id(repo_id(workspace, repo_path),
                                  child_path, "submodule"),
                         child_id(repo_id(workspace, repo_path),
                                  Path("/tmp/Health/API/SDK"),
                                  "submodule"))

    def test_replace_workspace_indexes_repos_and_children(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        store = StateStore()

        workspace = store.replace_workspace(
            name="ws",
            folders=[Path("/tmp/ws")],
            repos=[parent, canonical],
        )

        parent_id = store.repo_id_for(parent)
        canonical_id = store.repo_id_for(canonical)
        nested_id = store.child_id_for(child)

        self.assertEqual(workspace, workspace_id("ws"))
        self.assertIsNotNone(parent_id)
        self.assertIsNotNone(canonical_id)
        self.assertIsNotNone(nested_id)
        self.assertEqual(
            [record.repo for record in store.repo_records_for_workspace(workspace)],
            [parent, canonical],
        )
        child_record = store.child_record(nested_id)
        self.assertIsNotNone(child_record)
        assert child_record is not None
        self.assertEqual(child_record.parent_repo_id, parent_id)
        self.assertEqual(child_record.repo_id, canonical_id)
        self.assertIs(child_record.child, child)

    def test_replace_workspace_topology_does_not_read_parent_children(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = _ChildrenMustNotBeRead()
        status = ChildStatusSnapshot(
            kind="submodule",
            branch="main",
            dirty=True,
            message="explicit topology",
        )
        store = StateStore()

        workspace = store.replace_workspace_topology(
            name="ws",
            folders=[Path("/tmp/ws")],
            repos=[parent, canonical],
            children=[
                ChildTopologySnapshot(
                    parent_repo=parent,
                    child=child,
                    status=status,
                ),
            ],
        )

        parent_id = store.repo_id_for(parent)
        child_id_value = store.child_id_for(child)
        self.assertIsNotNone(parent_id)
        self.assertIsNotNone(child_id_value)
        self.assertEqual(
            [record.child for record in store.child_records_for_repo(parent_id)],
            [child],
        )
        self.assertEqual(store.child_status_by_id(child_id_value), status)
        self.assertEqual(
            [record.repo for record in store.repo_records_for_workspace(workspace)],
            [parent, canonical],
        )

    def test_replace_workspace_topology_can_update_inactive_workspace(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        store = StateStore()
        workspace_a = store.replace_workspace(
            name="A",
            folders=[Path("/tmp/a")],
            repos=[repo_a],
        )
        workspace_b = store.replace_workspace(
            name="B",
            folders=[Path("/tmp/b")],
            repos=[repo_b],
        )

        updated_a = _make_repo("a")
        result = store.replace_workspace_topology(
            name="A",
            folders=[Path("/tmp/a")],
            repos=[updated_a],
            children=[],
            activate=False,
        )

        self.assertEqual(result, workspace_a)
        self.assertEqual(store.active_workspace_id, workspace_b)
        self.assertEqual(
            [record.repo for record in store.repo_records_for_workspace(workspace_a)],
            [updated_a],
        )
        self.assertEqual(
            [record.repo for record in store.repo_records_for_workspace(workspace_b)],
            [repo_b],
        )

    def test_repo_refresh_mutex_survives_topology_replacement(self) -> None:
        old_repo = _make_repo("repo")
        new_repo = _make_repo("repo")
        store = StateStore()
        store.replace_workspace(name="ws", folders=[], repos=[old_repo])
        old_repo_id = store.repo_id_for(old_repo)

        acquired, captured_repo_id = store.acquire_repo_refresh(old_repo)
        self.assertTrue(acquired)
        self.assertEqual(captured_repo_id, old_repo_id)
        store.replace_workspace(name="ws", folders=[], repos=[new_repo])

        blocked, _blocked_repo_id = store.acquire_repo_refresh(new_repo)
        self.assertFalse(blocked)
        store.release_repo_refresh_by_id(captured_repo_id)
        reacquired, new_repo_id = store.acquire_repo_refresh(new_repo)
        self.assertTrue(reacquired)
        self.assertEqual(new_repo_id, old_repo_id)
        store.release_repo_refresh_by_id(new_repo_id)

    def test_child_refresh_mutex_survives_topology_replacement(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        replacement_parent = _make_repo("parent")
        replacement_canonical = _make_repo("canonical")
        replacement_child = ChildRef(
            repo=replacement_canonical,
            nested_path=replacement_parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        store = StateStore()
        store.replace_workspace(
            name="ws",
            folders=[],
            repos=[parent, canonical],
        )
        old_child_id = store.child_id_for(child)

        acquired, captured_child_id = store.acquire_child_refresh(child)
        self.assertTrue(acquired)
        self.assertEqual(captured_child_id, old_child_id)
        store.replace_workspace_topology(
            name="ws",
            folders=[],
            repos=[replacement_parent, replacement_canonical],
            children=[
                ChildTopologySnapshot(
                    parent_repo=replacement_parent,
                    child=replacement_child,
                    status=ChildStatusSnapshot(kind="submodule"),
                ),
            ],
        )

        blocked, _blocked_child_id = store.acquire_child_refresh(
            replacement_child)
        self.assertFalse(blocked)
        store.release_child_refresh_by_id(captured_child_id)
        reacquired, new_child_id = store.acquire_child_refresh(
            replacement_child)
        self.assertTrue(reacquired)
        self.assertEqual(new_child_id, old_child_id)
        store.release_child_refresh_by_id(new_child_id)

    def test_replace_workspace_drops_stale_repo_and_child_records(self) -> None:
        old_repo = _make_repo("old")
        old_child = ChildRef(
            repo=old_repo,
            nested_path=old_repo.path / "child",
            kind="submodule",
        )
        old_repo.children = [old_child]
        new_repo = _make_repo("new")
        store = StateStore()
        workspace = store.replace_workspace(
            name="ws",
            folders=[],
            repos=[old_repo],
        )
        old_repo_id = store.repo_id_for(old_repo)
        old_child_id = store.child_id_for(old_child)

        store.replace_workspace(name="ws", folders=[], repos=[new_repo])

        self.assertIsNone(store.repo_id_for(old_repo))
        self.assertIsNone(store.child_id_for(old_child))
        self.assertIsNone(store.repo_record(old_repo_id))
        self.assertIsNone(store.child_record(old_child_id))
        self.assertEqual(
            [record.repo for record in store.repo_records_for_workspace(workspace)],
            [new_repo],
        )

    def test_state_initializes_store_without_waking_ui(self) -> None:
        repo = _make_repo("repo")

        state = State(repos=[repo], workspace_name="ws")

        self.assertFalse(state.ui_events.is_set())
        repo_id_value = state.store.repo_id_for(repo)
        self.assertIsNotNone(repo_id_value)
        self.assertIs(state.store.repo_record(repo_id_value).repo, repo)

    def test_store_replacement_wakes_ui_after_state_init(self) -> None:
        state = State(repos=[], workspace_name="ws")
        repo = _make_repo("repo")

        state.store.replace_workspace(name="ws", folders=[], repos=[repo])

        self.assertTrue(state.ui_events.drain())

    def test_state_replace_repos_reindexes_store(self) -> None:
        old_repo = _make_repo("old")
        new_repo = _make_repo("new")
        state = State(repos=[old_repo], workspace_name="ws")
        old_repo_id = state.store.repo_id_for(old_repo)

        state.replace_repos([new_repo])

        self.assertIsNone(state.store.repo_id_for(old_repo))
        self.assertIsNone(state.store.repo_record(old_repo_id))
        new_repo_id = state.store.repo_id_for(new_repo)
        self.assertIsNotNone(new_repo_id)
        self.assertIs(state.store.repo_record(new_repo_id).repo, new_repo)

    def test_replace_workspace_publishes_repo_status_snapshot(self) -> None:
        repo = _make_repo("repo")
        repo.branch = "main"
        repo.unstaged = [("M", "README.md")]
        repo.message = "raw draft"

        state = State(repos=[repo], workspace_name="ws")

        status = state.store.repo_status(repo)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.branch, "main")
        self.assertTrue(status.dirty)
        self.assertEqual(status.message, "")

    def test_publish_row_status_updates_repo_snapshot(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")

        repo.message = "raw draft"
        repo.untracked = ["new.txt"]
        state.store.publish_row_status(repo)

        status = state.store.repo_status(repo)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.dirty)
        self.assertEqual(status.message, "")

    def test_publish_repo_status_snapshot_does_not_read_row_fields(self) -> None:
        repo = _make_repo("repo")
        repo.branch = "stale"
        repo.message = "stale draft"
        state = State(repos=[repo], workspace_name="ws")
        snapshot = RepoStatusSnapshot(
            branch="main",
            head="abc123",
            upstream="origin/main",
            ahead=2,
            behind=1,
            dirty=True,
            message="store draft",
        )

        state.store.publish_repo_status_snapshot(repo, snapshot)

        status = state.store.repo_status(repo)
        self.assertEqual(status, snapshot)
        self.assertEqual(repo.branch, "stale")
        self.assertEqual(repo.message, "stale draft")

    def test_set_row_message_updates_repo_snapshot(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")

        state.store.set_row_message(repo, "store draft")

        self.assertEqual(state.store.row_message(repo), "store draft")
        self.assertEqual(repo.message, "")

    def test_repo_workflow_intent_is_store_owned_and_copied(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")
        intent = WorkflowIntentSnapshot(
            track_workflow={"CI": True},
            then_run_after_push="Deploy",
            then_run_params_after_push={"env": "prod"},
            then_run_after_workflow={"CI": "Release"},
            then_run_params_after_workflow={"CI": {"tag": "v1"}},
        )

        state.store.set_repo_workflow_intent(repo, intent)
        intent.track_workflow["CI"] = False
        intent.then_run_params_after_workflow["CI"]["tag"] = "mutated"

        stored = state.store.repo_workflow_intent(repo)
        self.assertEqual(stored.track_workflow, {"CI": True})
        self.assertEqual(stored.then_run_after_push, "Deploy")
        self.assertEqual(stored.then_run_params_after_push, {"env": "prod"})
        self.assertEqual(stored.then_run_after_workflow, {"CI": "Release"})
        self.assertEqual(
            stored.then_run_params_after_workflow,
            {"CI": {"tag": "v1"}},
        )
        self.assertFalse(hasattr(repo, "track_workflow"))
        self.assertFalse(hasattr(repo, "then_run_after_push"))

    def test_take_repo_workflow_intent_clears_store_only(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                track_workflow={"CI": True},
                then_run_after_push="Deploy",
            ),
        )

        taken = state.store.take_repo_workflow_intent(repo)

        self.assertEqual(taken.track_workflow, {"CI": True})
        self.assertEqual(taken.then_run_after_push, "Deploy")
        self.assertTrue(state.store.repo_workflow_intent(repo).empty)
        self.assertFalse(hasattr(repo, "track_workflow"))
        self.assertFalse(hasattr(repo, "then_run_after_push"))

    def test_pop_repo_then_run_after_workflow_clears_one_store_slot(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")
        state.store.set_repo_workflow_intent(
            repo,
            WorkflowIntentSnapshot(
                then_run_after_workflow={
                    "CI": "Release",
                    "Lint": "Notify",
                },
                then_run_params_after_workflow={
                    "CI": {"tag": "v1"},
                    "Lint": {"channel": "dev"},
                },
            ),
        )

        target, params = state.store.pop_repo_then_run_after_workflow(
            repo, "CI")

        self.assertEqual(target, "Release")
        self.assertEqual(params, {"tag": "v1"})
        remaining = state.store.repo_workflow_intent(repo)
        self.assertEqual(remaining.then_run_after_workflow, {"Lint": "Notify"})
        self.assertEqual(
            remaining.then_run_params_after_workflow,
            {"Lint": {"channel": "dev"}},
        )

    def test_publish_workspace_statuses_updates_child_snapshot(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
        )
        parent.children = [child]
        state = State(repos=[parent, canonical], workspace_name="ws")

        child.dirty = True
        child.message = "raw nested draft"
        state.store.publish_workspace_statuses(state.repos)

        status = state.store.child_status(child)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.dirty)
        self.assertEqual(status.message, "")

    def test_publish_child_status_snapshot_does_not_read_row_fields(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
        )
        child.branch = "stale"
        parent.children = [child]
        state = State(repos=[parent, canonical], workspace_name="ws")
        snapshot = ChildStatusSnapshot(
            kind="submodule",
            branch="main",
            head="def456",
            upstream="origin/main",
            ahead=1,
            dirty=True,
            message="nested store draft",
            in_sync=False,
        )

        state.store.publish_child_status_snapshot(child, snapshot)

        status = state.store.child_status(child)
        self.assertEqual(status, snapshot)
        self.assertEqual(child.branch, "stale")
        self.assertEqual(child.message, "")

    def test_set_row_message_updates_child_snapshot(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
        )
        parent.children = [child]
        state = State(repos=[parent, canonical], workspace_name="ws")

        state.store.set_row_message(child, "nested store draft")

        self.assertEqual(state.store.row_message(child), "nested store draft")
        self.assertEqual(child.message, "")
        status = state.store.child_status(child)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.message, "nested store draft")

    def test_same_id_replacement_preserves_busy_state(self) -> None:
        old_repo = _make_repo("repo")
        new_repo = _make_repo("repo")
        state = State(repos=[old_repo], workspace_name="ws")

        state.store.set_repo_busy(old_repo, True)
        state.replace_repos([new_repo])

        self.assertFalse(state.store.repo_busy(old_repo))
        self.assertTrue(state.store.repo_busy(new_repo))

    def test_busy_state_requires_matching_releases(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")

        state.store.set_repo_busy(repo, True)
        state.store.set_repo_busy(repo, True)
        state.store.set_repo_busy(repo, False)

        self.assertTrue(state.store.repo_busy(repo))

        state.store.set_repo_busy(repo, False)

        self.assertFalse(state.store.repo_busy(repo))

    def test_child_busy_state_requires_matching_releases(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
        )
        parent.children = [child]
        state = State(repos=[parent, canonical], workspace_name="ws")

        state.store.set_child_busy(child, True)
        state.store.set_child_busy(child, True)
        state.store.set_child_busy(child, False)

        self.assertTrue(state.store.child_busy(child))

        state.store.set_child_busy(child, False)

        self.assertFalse(state.store.child_busy(child))

    def test_vanished_row_replacement_drops_busy_state(self) -> None:
        old_repo = _make_repo("old")
        new_repo = _make_repo("new")
        state = State(repos=[old_repo], workspace_name="ws")

        state.store.set_repo_busy(old_repo, True)
        state.replace_repos([new_repo])

        self.assertFalse(state.store.repo_busy(old_repo))
        self.assertFalse(state.store.repo_busy(new_repo))

    def test_same_id_replacement_preserves_suggesting_state(self) -> None:
        old_repo = _make_repo("repo")
        new_repo = _make_repo("repo")
        state = State(repos=[old_repo], workspace_name="ws")

        state.store.set_repo_suggesting(old_repo, True)
        state.replace_repos([new_repo])

        self.assertFalse(state.store.repo_suggesting(old_repo))
        self.assertTrue(state.store.repo_suggesting(new_repo))

    def test_vanished_row_replacement_drops_suggesting_state(self) -> None:
        old_repo = _make_repo("old")
        new_repo = _make_repo("new")
        state = State(repos=[old_repo], workspace_name="ws")

        state.store.set_repo_suggesting(old_repo, True)
        state.replace_repos([new_repo])

        self.assertFalse(state.store.repo_suggesting(old_repo))
        self.assertFalse(state.store.repo_suggesting(new_repo))


if __name__ == "__main__":
    unittest.main()
