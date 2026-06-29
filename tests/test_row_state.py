"""Row refresh state helper tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state  # noqa: E402
from core.models import ChildRef  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from core.state.pickers import BranchPicker, RemoteBranchPicker  # noqa: E402
from core.state.row_state import (  # noqa: E402
    set_canonical_tree_refreshing,
    set_child_refreshing,
    set_repo_refreshing,
)
from core.state.selectors import (  # noqa: E402
    child_row_state,
    local_mutation_active_for,
    read_only_child_busy,
    read_only_child_busy_predicate,
    repo_row_state,
    view_load_activity_active,
)
from core.state.selectors import read_only_row_busy_active  # noqa: E402
from core.runtime.tasks import Task  # noqa: E402
from core.state.views import CommitViewModal, DiffViewer, TaskLogViewer  # noqa: E402


class TestRowStateHelpers(unittest.TestCase):
    def test_repo_and_child_refreshing_helpers_set_store_busy_state(self) -> None:
        repo = _make_repo("repo")
        child = ChildRef(repo=repo, nested_path=repo.path / "child")
        repo.children = [child]
        state = make_state(repo)

        set_repo_refreshing(state, repo, True)
        set_child_refreshing(state, child, True)

        self.assertTrue(state.store.repo_busy(repo))
        self.assertTrue(state.store.child_busy(child))

        set_repo_refreshing(state, repo, False)
        set_child_refreshing(state, child, False)

        self.assertFalse(state.store.repo_busy(repo))
        self.assertFalse(state.store.child_busy(child))

    def test_canonical_tree_refreshing_marks_nested_submodule_rows(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        other = _make_repo("other")
        nested = parent.path / "vendor" / "canonical"
        matching = ChildRef(repo=canonical, nested_path=nested, kind="submodule")
        subtree = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "tree",
            kind="subtree",
        )
        unrelated = ChildRef(
            repo=other,
            nested_path=parent.path / "vendor" / "other",
            kind="submodule",
        )
        parent.children = [matching, subtree, unrelated]
        canonical.siblings = [(parent, nested)]
        state = make_state(parent, canonical, other)
        parent.children = []

        set_canonical_tree_refreshing(state, canonical, True)

        self.assertTrue(state.store.repo_busy(canonical))
        self.assertTrue(state.store.child_busy(matching))
        self.assertFalse(state.store.child_busy(subtree))
        self.assertFalse(state.store.child_busy(unrelated))

        set_canonical_tree_refreshing(state, canonical, False)

        self.assertFalse(state.store.repo_busy(canonical))
        self.assertFalse(state.store.child_busy(matching))

    def test_row_selectors_read_suggesting_from_store(self) -> None:
        repo = _make_repo("repo")
        child = ChildRef(
            repo=repo,
            nested_path=repo.path / "child",
            kind="submodule",
        )
        repo.children = [child]
        state = make_state(repo)

        state.store.set_repo_suggesting(repo, True)
        state.store.set_child_suggesting(child, True)

        self.assertTrue(repo_row_state(state, repo).suggesting)
        self.assertTrue(child_row_state(state, child).suggesting)

    def test_read_only_row_busy_active_reads_store_busy_state(self) -> None:
        repo = _make_repo("repo")
        child = ChildRef(
            repo=repo,
            nested_path=repo.path / "child",
            kind="submodule",
        )
        repo.children = [child]
        state = make_state(repo)

        self.assertFalse(read_only_row_busy_active(state))
        state.store.set_child_busy(child, True)

        self.assertTrue(read_only_row_busy_active(state))

    def test_read_only_row_busy_active_uses_store_workspace_rows(self) -> None:
        repo = _make_repo("repo")
        state = make_state(repo)
        state.repos = []

        state.store.set_repo_busy(repo, True)

        self.assertTrue(read_only_row_busy_active(state))

    def test_read_only_row_busy_active_uses_store_child_records(self) -> None:
        repo = _make_repo("repo")
        child = ChildRef(
            repo=repo,
            nested_path=repo.path / "child",
            kind="submodule",
        )
        repo.children = [child]
        state = make_state(repo)
        repo.children = []

        state.store.set_child_busy(child, True)

        self.assertTrue(read_only_row_busy_active(state, [repo]))

    def test_local_mutation_can_include_store_child_records(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        state = make_state(parent, canonical)
        parent.children = []
        lease_id = state.leases.acquire(child=child)
        self.addCleanup(state.leases.release, lease_id)

        self.assertFalse(local_mutation_active_for(state, repos=[parent]))
        self.assertTrue(local_mutation_active_for(
            state,
            repos=[parent],
            include_repo_children=True,
        ))

    def test_repo_row_state_does_not_expand_to_child_mutations(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        state = make_state(parent, canonical)
        lease_id = state.leases.acquire(child=child)
        self.addCleanup(state.leases.release, lease_id)

        self.assertFalse(repo_row_state(state, parent).busy)
        self.assertTrue(child_row_state(state, child).busy)

    def test_child_busy_selector_predicate_reads_store_busy_state(self) -> None:
        repo = _make_repo("repo")
        child = ChildRef(
            repo=repo,
            nested_path=repo.path / "child",
            kind="submodule",
        )
        repo.children = [child]
        state = make_state(repo)
        predicate = read_only_child_busy_predicate(state)

        self.assertFalse(read_only_child_busy(state, child))
        self.assertFalse(predicate(child))

        state.store.set_child_busy(child, True)

        self.assertTrue(read_only_child_busy(state, child))
        self.assertTrue(predicate(child))

    def test_view_load_activity_reads_active_modal_load_records(self) -> None:
        state = make_state()
        state.action_menu = ActionMenu("repo", Path("/tmp/repo"))
        state.action_menu.state_load_id = "action:state"

        self.assertFalse(view_load_activity_active(state))

        state.view_loads.create("action:state")

        self.assertTrue(view_load_activity_active(state))

        state.view_loads.finish("action:state", [])

        self.assertFalse(view_load_activity_active(state))

    def test_view_load_activity_collects_all_modal_load_id_shapes(self) -> None:
        state = make_state()
        state.action_menu = ActionMenu("repo", Path("/tmp/repo"))
        state.action_menu.state_load_id = "action:state"
        state.action_menu.inventory_load_id = "action:inventory"
        state.action_menu.tree_load_id = "action:tree"
        state.action_menu.commits_load_id = "action:commits"
        state.commit_view_modal = CommitViewModal(
            "repo", Path("/tmp/repo"), "abc123")
        state.commit_view_modal.tags_load_id = "commit:tags"
        state.commit_view_modal.details_load_id = "commit:details"
        state.commit_view_modal.files_load_id = "commit:files"
        state.commit_view_modal.reflog_load_id = "commit:reflog"
        state.diff_viewer = DiffViewer(
            "README.md", Path("/tmp/repo"), "repo")
        state.diff_viewer.diff_load_id = "diff:diff"
        state.diff_viewer.log_load_id = "diff:log"
        state.diff_viewer.blame_load_id = "diff:blame"
        state.task_log_viewer = TaskLogViewer(
            Task("workflow"), "owner/repo", 123, "task-log")
        state.branch_picker = BranchPicker("repo", Path("/tmp/repo"))
        state.branch_picker.load_id = "branch"
        state.remote_branch_picker = RemoteBranchPicker(
            "repo", Path("/tmp/repo"))
        state.remote_branch_picker.load_id = "remote-branch"

        state.view_loads.create("remote-branch")

        self.assertTrue(view_load_activity_active(state))

        state.view_loads.finish("remote-branch", [])

        self.assertFalse(view_load_activity_active(state))


if __name__ == "__main__":
    unittest.main()
