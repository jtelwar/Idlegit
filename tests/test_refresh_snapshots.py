"""Tests for typed refresh snapshot producers."""
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
from core.git_ops import (  # noqa: E402
    apply_repo_refresh_snapshot,
    read_repo_refresh_snapshot,
    RepoRefreshSnapshot,
)
from core.state.app import State  # noqa: E402
from core import workers  # noqa: E402


class TestRefreshSnapshots(unittest.TestCase):
    def test_repo_refresh_snapshot_does_not_mutate_live_repo_until_applied(
            self) -> None:
        repo = _make_repo("repo")
        repo.branch = "stale"
        repo.message = "draft"
        status = (
            "# branch.oid abc123\x00"
            "# branch.head main\x00"
            "# branch.upstream origin/main\x00"
            "# branch.ab +2 -1\x00"
            "1 .M N... 100644 100644 100644 abc abc README.md\x00"
        )

        def fake_git(_path, args, **_kwargs):
            if args[:3] == ["status", "--porcelain=v2", "--branch"]:
                return 0, status, ""
            if args == ["remote", "get-url", "origin"]:
                return 0, "git@github.com:acme/repo.git\n", ""
            if args == ["rev-parse", "--git-dir"]:
                return 0, ".git\n", ""
            return 1, "", "unexpected"

        with mock.patch("core.git_ops.git", side_effect=fake_git), \
                mock.patch("core.git_ops.discover_workflows_local",
                           return_value=[]), \
                mock.patch("core.git_ops._read_gitmodules_submodules",
                           return_value=[]):
            snapshot = read_repo_refresh_snapshot(repo, message="draft")

        self.assertEqual(snapshot.branch, "main")
        self.assertEqual(snapshot.head, "abc123")
        self.assertEqual(snapshot.ahead, 2)
        self.assertEqual(snapshot.behind, 1)
        self.assertTrue(snapshot.dirty)
        self.assertEqual(snapshot.message, "draft")
        self.assertEqual(repo.branch, "stale")

        apply_repo_refresh_snapshot(repo, snapshot)

        self.assertEqual(repo.branch, "main")
        self.assertEqual(repo.remote_url, "github.com/acme/repo")
        self.assertEqual(repo.unstaged, [("M", "README.md")])

    def test_worker_refresh_uses_store_message_when_projection_is_empty(
            self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="ws")
        state.store.set_row_message(repo, "store draft")
        repo.message = ""

        def read_snapshot(target, *, message=""):
            self.assertIs(target, repo)
            self.assertEqual(message, "store draft")
            return RepoRefreshSnapshot(
                branch="main",
                staged=[("M", "README.md")],
                message=message,
            )

        with mock.patch.object(
                workers,
                "read_repo_refresh_snapshot",
                side_effect=read_snapshot):
            workers._refresh_repo_snapshot_into_state(state, repo)

        status = state.store.repo_status(repo)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.message, "store draft")
        self.assertEqual(repo.message, "")


if __name__ == "__main__":
    unittest.main()
