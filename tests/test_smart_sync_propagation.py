"""Smart-sync parent propagation service tests."""

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
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core import workers  # noqa: E402
from core.smart_sync.propagation import (  # noqa: E402
    cascade_propagate_to_parents,
    ff_submodule_checkout_to,
)


class TestSmartSyncPropagation(unittest.TestCase):
    def test_cascade_uses_injected_safe_operations_and_recurses(self) -> None:
        canonical = _make_repo("canonical")
        parent = _make_repo("parent")
        grandparent = _make_repo("grandparent")
        nested_parent = grandparent.path / "vendor" / "parent"
        child = ChildRef(repo=parent, nested_path=nested_parent, branch="main")
        grandparent.children = [child]
        canonical.siblings = [(parent, parent.path / "vendor" / "canonical")]
        parent.siblings = [(grandparent, nested_parent)]
        state = State(repos=[canonical, parent, grandparent], workspace_name="A")
        propagated = []
        fast_forwarded = []

        def propagate_parent(_state, repo, label):
            propagated.append(label)
            return "abc123" if repo is parent else ""

        def ff_submodule(path: Path, branch: str, target_sha: str) -> bool:
            fast_forwarded.append((path, branch, target_sha))
            return True

        cascade_propagate_to_parents(
            state,
            [canonical],
            find_child_at=lambda _parent, _path: child,
            propagate_parent=propagate_parent,
            ff_submodule=ff_submodule,
            git_fn=lambda _path, _args: (0, "main\n", ""),
        )

        self.assertEqual(propagated, ["parent", "grandparent"])
        self.assertEqual(fast_forwarded, [(nested_parent, "main", "abc123")])
        task = next(
            t for t in state.tasks.snapshot()
            if t.label == "  ↳ propagate parent: align in grandparent")
        self.assertEqual(task.status, "ok")
        self.assertFalse(state.tasks.has_running())

    def test_cascade_checks_same_parent_path_only_once(self) -> None:
        canonical_a = _make_repo("canonical-a")
        canonical_b = _make_repo("canonical-b")
        parent_a = _make_repo("parent")
        parent_b = _make_repo("parent")
        canonical_a.siblings = [(parent_a, parent_a.path / "vendor" / "a")]
        canonical_b.siblings = [(parent_b, parent_b.path / "vendor" / "b")]
        state = State(
            repos=[canonical_a, canonical_b, parent_a],
            workspace_name="A",
        )
        propagated = []

        def propagate_parent(_state, repo, label):
            propagated.append((repo, label))
            return ""

        cascade_propagate_to_parents(
            state,
            [canonical_a, canonical_b],
            find_child_at=lambda _parent, _path: None,
            propagate_parent=propagate_parent,
            ff_submodule=lambda _path, _branch, _target_sha: True,
            git_fn=lambda _path, _args: (0, "main\n", ""),
        )

        self.assertEqual(propagated, [(parent_a, "parent")])
        self.assertFalse(state.tasks.has_running())

    def test_cascade_skips_transient_recursive_parents(self) -> None:
        canonical = _make_repo("models")
        transient_parent = _make_repo("sdk-in-app", synthetic=True)
        real_parent = _make_repo("sdk")
        canonical.siblings = [
            (transient_parent, transient_parent.path / "vendor" / "models"),
            (real_parent, real_parent.path / "vendor" / "models"),
        ]
        state = State(repos=[canonical, real_parent], workspace_name="A")
        propagated = []

        def propagate_parent(_state, repo, label):
            propagated.append((repo, label))
            return ""

        cascade_propagate_to_parents(
            state,
            [canonical],
            find_child_at=lambda _parent, _path: None,
            propagate_parent=propagate_parent,
            ff_submodule=lambda _path, _branch, _target_sha: True,
            git_fn=lambda _path, _args: (0, "main\n", ""),
        )

        self.assertEqual(propagated, [(real_parent, "sdk")])

    def test_ff_submodule_checkout_allows_matching_pointer_only_dirt(self) -> None:
        repo = Path("/repo/sdk")
        calls = []

        def git_fn(path: Path, args: list[str]):
            calls.append((path, tuple(args)))
            if args == ["fetch", "origin", "main"]:
                return 0, "", ""
            if args == ["branch", "--show-current"]:
                return 0, "main\n", ""
            if args == ["status", "--porcelain=v1"]:
                return 0, " M vendor/models\n", ""
            if path == repo / "vendor" / "models" and args == ["rev-parse", "HEAD"]:
                return 0, "new-models\n", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "old-sdk\n", ""
            if args == ["merge-base", "--is-ancestor", "HEAD", "new-sdk"]:
                return 0, "", ""
            if args == ["rev-parse", "new-sdk:vendor/models"]:
                return 0, "new-models\n", ""
            if args == ["merge", "--ff-only", "new-sdk"]:
                return 0, "", ""
            return 1, "", "unexpected"

        ok = ff_submodule_checkout_to(
            repo,
            "main",
            "new-sdk",
            git_fn=git_fn,
            submodule_paths_fn=lambda _path: ["vendor/models"],
        )

        self.assertTrue(ok)
        self.assertIn((repo, ("merge", "--ff-only", "new-sdk")), calls)

    def test_worker_propagation_uses_store_publishing_refresh(self) -> None:
        parent = _make_repo("parent")
        state = State(repos=[parent], workspace_name="A")

        with mock.patch.object(
                workers,
                "propagate_submodule_bump",
                return_value="abc123") as propagate:
            result = workers._propagate_submodule_bump(state, parent, "parent")

        self.assertEqual(result, "abc123")
        propagate.assert_called_once()
        with mock.patch.object(
                workers, "_refresh_repo_snapshot_into_state") as refresh:
            propagate.call_args.kwargs["refresh_fn"](parent)
        refresh.assert_called_once_with(state, parent)


if __name__ == "__main__":
    unittest.main()
