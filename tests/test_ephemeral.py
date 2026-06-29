"""Tests for the launched-from-cwd ephemeral-workspace detection.

Covers the three helpers in `core.ephemeral` plus the persistence
side: ephemeral workspaces must round-trip through `save_workspaces`
WITHOUT being written to disk, and `Workspace.display_name` must
wrap the name in square brackets so the UI surfaces signal
"transient" at a glance."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo  # noqa: E402
from core import config  # noqa: E402
from core.config import load_workspaces, save_workspaces  # noqa: E402
from core.ephemeral import (  # noqa: E402
    build_ephemeral_workspace,
    find_git_repo_root,
    repo_covered_by_workspace,
)
from core.state.workspaces import Workspace  # noqa: E402


class TestFindGitRepoRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_cwd_is_a_repo(self) -> None:
        # `make_repo(tmp, 'foo')` initialises tmp/foo as a real git repo.
        # Comparing on the resolved form so a /var → /private/var
        # symlink (macOS tempdir) doesn't false-fail the assertion.
        repo = make_repo(self.tmp, "foo")
        self.assertEqual(find_git_repo_root(repo), repo.resolve())

    def test_walks_up_from_subdirectory(self) -> None:
        repo = make_repo(self.tmp, "foo")
        nested = repo / "src" / "deep"
        nested.mkdir(parents=True)
        # Subfolder isn't a repo itself — walker should climb to `foo`.
        self.assertEqual(find_git_repo_root(nested), repo.resolve())

    def test_non_repo_returns_none(self) -> None:
        plain = self.tmp / "no_git_here"
        plain.mkdir()
        self.assertIsNone(find_git_repo_root(plain))


class TestRepoCoveredByWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_no_workspaces_means_not_covered(self) -> None:
        repo = make_repo(self.tmp, "foo")
        self.assertIsNone(repo_covered_by_workspace(repo, []))

    def test_exact_folder_match_covers(self) -> None:
        repo = make_repo(self.tmp, "foo")
        ws = Workspace(name="W", folders=[repo])
        self.assertIs(repo_covered_by_workspace(repo, [ws]), ws)

    def test_parent_folder_match_covers(self) -> None:
        # A workspace folder that lists `tmp` (the parent dir of `foo`)
        # covers `foo` via the "immediate child" discovery rule.
        repo = make_repo(self.tmp, "foo")
        ws = Workspace(name="W", folders=[self.tmp])
        self.assertIs(repo_covered_by_workspace(repo, [ws]), ws)

    def test_unrelated_folder_does_not_cover(self) -> None:
        repo = make_repo(self.tmp, "foo")
        other = self.tmp / "elsewhere"
        other.mkdir()
        ws = Workspace(name="W", folders=[other])
        self.assertIsNone(repo_covered_by_workspace(repo, [ws]))

    def test_grandparent_folder_does_not_cover(self) -> None:
        # Discovery only walks immediate children — listing
        # `~/work` as a workspace folder must NOT auto-cover a
        # repo at `~/work/team/proj` (the team subfolder isn't
        # itself listed).
        nested_parent = self.tmp / "team"
        nested_parent.mkdir()
        repo = make_repo(nested_parent, "proj")
        ws = Workspace(name="W", folders=[self.tmp])
        self.assertIsNone(repo_covered_by_workspace(repo, [ws]))


class TestBuildEphemeralWorkspace(unittest.TestCase):
    def test_marks_ephemeral_and_uses_basename(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "myrepo"
            repo.mkdir()
            ws = build_ephemeral_workspace(repo)
        self.assertTrue(ws.ephemeral)
        self.assertEqual(ws.name, "myrepo")
        self.assertEqual(ws.folders, [repo])

    def test_display_name_brackets_ephemeral(self) -> None:
        ws = Workspace(name="myrepo", folders=[Path("/tmp")], ephemeral=True)
        self.assertEqual(ws.display_name, "[myrepo]")

    def test_display_name_plain_for_persisted(self) -> None:
        ws = Workspace(name="myrepo", folders=[Path("/tmp")])
        self.assertEqual(ws.display_name, "myrepo")


class TestSaveWorkspacesSkipsEphemeral(unittest.TestCase):
    """Ephemeral workspaces must NOT be persisted — they're
    regenerated each launch from cwd, so writing them out would
    pollute idlegit.workspaces with whatever directory the user
    happened to launch from."""

    def test_ephemeral_excluded_from_persisted_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                kept_dir = Path(d) / "kept"
                kept_dir.mkdir()
                src = [
                    Workspace(name="Ephem",
                              folders=[Path(d) / "ephemeral"],
                              ephemeral=True),
                    Workspace(name="Kept",
                              folders=[kept_dir]),
                ]
                save_workspaces(src, active_index=1)
                loaded, _ = load_workspaces()
        self.assertEqual([w.name for w in loaded], ["Kept"])

    def test_ephemeral_active_doesnt_persist_active_marker(self) -> None:
        # When the ephemeral workspace is active at save time, the
        # `[idlegit] active_workspace` marker must NOT name it — the
        # next session would fail to find that entry, and falling
        # back to index 0 would land the user in a different
        # workspace than they expect. Skip the active block entirely
        # in that case so load_workspaces uses its default.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                kept_dir = Path(d) / "kept"
                kept_dir.mkdir()
                src = [
                    Workspace(name="Ephem",
                              folders=[Path(d) / "ephemeral"],
                              ephemeral=True),
                    Workspace(name="Kept",
                              folders=[kept_dir]),
                ]
                save_workspaces(src, active_index=0)  # active = ephemeral
                loaded, active_idx = load_workspaces()
        # Only the persistent workspace round-trips, and the
        # active-index default falls back to 0 in its (re-numbered)
        # post-filter list.
        self.assertEqual([w.name for w in loaded], ["Kept"])
        self.assertEqual(active_idx, 0)


if __name__ == "__main__":
    unittest.main()
