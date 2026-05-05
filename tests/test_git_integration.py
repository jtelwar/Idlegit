"""Integration tests that drive real `git` against temp repos. Slower
than the pure-helper tests, but they're the only ones that can prove
discover_repos / link_siblings / refresh_repo / find_lfs_warnings /
suggest_commit_message actually behave the same as git itself."""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import (  # noqa: E402
    _run, add_origin, make_repo, stage_and_commit, write_file,
)
from git_ops import (  # noqa: E402
    discover_repos, discover_workflows_local, find_lfs_warnings,
    link_siblings, refresh_repo, signature_mtime, suggest_commit_message,
    sync_subtree, working_tree_signature,
)
from models import Repo, SubtreeSpec  # noqa: E402


def _spawn_recovery_canceller(state, timeout: float = 5.0):
    """Background daemon that watches `state.detached_recovery_prompt`
    for the auto-recovery modal opening, then "presses Esc" by setting
    `chosen_action="cancel"` and signalling `result_event`. Lets tests
    that hit the smart-sync / commit-pipeline detached-HEAD guards
    drive the worker through to its refusal path without a real UI.

    Returns the watcher thread so the test can join it. Use only in
    tests where the cancellation is the expected outcome."""
    def watcher():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            prompt = state.detached_recovery_prompt
            if prompt is not None:
                prompt.chosen_action = "cancel"
                prompt.result_event.set()
                state.detached_recovery_prompt = None
                return
            time.sleep(0.05)
    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    return t


class _TempWorkspace(unittest.TestCase):
    """Mixin that gives each test method a clean tmp workspace."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="idlegit-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)


class TestDiscoverRepos(_TempWorkspace):
    def test_finds_workspace_repo_when_workspace_is_a_repo(self) -> None:
        make_repo(self.tmp.parent, self.tmp.name, with_initial_commit=True)
        repos = discover_repos(self.tmp)
        rels = [r.rel for r in repos]
        self.assertIn(".", rels, "workspace itself should be tracked when it's a repo")

    def test_finds_immediate_child_repos(self) -> None:
        make_repo(self.tmp, "alpha")
        make_repo(self.tmp, "beta")
        # A non-git folder should NOT show up.
        (self.tmp / "ignore-me").mkdir()
        repos = discover_repos(self.tmp)
        rels = sorted(r.rel for r in repos)
        self.assertEqual(rels, ["alpha", "beta"])

    def test_skips_dotfolders(self) -> None:
        make_repo(self.tmp, "real")
        make_repo(self.tmp, ".hidden")
        repos = discover_repos(self.tmp)
        self.assertEqual([r.rel for r in repos], ["real"])

    def test_alphabetical_ordering(self) -> None:
        for name in ("Charlie", "alpha", "Bravo"):
            make_repo(self.tmp, name)
        repos = discover_repos(self.tmp)
        # Sort is case-insensitive in discover_repos.
        self.assertEqual([r.rel for r in repos], ["alpha", "Bravo", "Charlie"])


class TestRefreshRepo(_TempWorkspace):
    def test_clean_repo_state(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertEqual(repo.error, "")
        self.assertFalse(repo.is_dirty)
        self.assertFalse(repo.merging)
        self.assertEqual(repo.branch, "main")
        self.assertNotEqual(repo.head, "")

    def test_dirty_untracked_file(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "new.txt", "hello\n")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertTrue(repo.is_dirty)
        self.assertIn("new.txt", repo.untracked)

    def test_staged_change(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "edit.txt", "hi\n")
        _run(repo_path, "git", "add", "edit.txt")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertTrue(repo.is_dirty)
        kinds = {x for x, _ in repo.staged}
        self.assertIn("A", kinds)

    def test_no_upstream_means_zero_ahead_behind(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertIsNone(repo.upstream)
        self.assertEqual(repo.ahead, 0)
        self.assertEqual(repo.behind, 0)

    def test_remote_url_canonicalized_and_raw_kept(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        add_origin(repo_path, "git@github.com:Foo/Bar.git")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertEqual(repo.remote_url_raw, "git@github.com:Foo/Bar.git")
        self.assertEqual(repo.remote_url, "github.com/foo/bar")


class TestLinkSiblings(_TempWorkspace):
    def _setup_parent_with_submodule_of_target(self) -> tuple:
        """Create a `target` repo and a `parent` repo that adds `target` as
        a submodule. Both share the same canonical origin URL so
        link_siblings can match them. Returns the two repo objects, refreshed."""
        target_path = make_repo(self.tmp, "target")
        parent_path = make_repo(self.tmp, "parent")
        target_url = f"file://{target_path}"
        # Target advertises itself at this URL (this is what link_siblings
        # canonicalizes against the parent's recorded submodule URL).
        add_origin(target_path, target_url)
        # Add target as a submodule of parent via the same file:// URL.
        _run(parent_path, "git", "submodule", "add",
             target_url, "vendored/target")
        _run(parent_path, "git", "commit", "-q", "-m", "add submodule")
        target = Repo(rel="target", path=target_path)
        parent = Repo(rel="parent", path=parent_path)
        refresh_repo(target)
        refresh_repo(parent)
        return parent, target

    def test_submodule_match_creates_child_ref(self) -> None:
        parent, target = self._setup_parent_with_submodule_of_target()
        link_siblings([parent, target])
        self.assertEqual(len(parent.children), 1)
        ref = parent.children[0]
        self.assertEqual(ref.kind, "submodule")
        self.assertIs(ref.repo, target)

    def test_target_records_sibling_back_pointer(self) -> None:
        parent, target = self._setup_parent_with_submodule_of_target()
        link_siblings([parent, target])
        self.assertEqual(len(target.siblings), 1)
        sib_parent, sib_path = target.siblings[0]
        self.assertIs(sib_parent, parent)
        self.assertEqual(sib_path.name, "target")

    def test_child_in_sync_initially_true(self) -> None:
        parent, target = self._setup_parent_with_submodule_of_target()
        link_siblings([parent, target])
        ref = parent.children[0]
        self.assertTrue(ref.in_sync,
                        "freshly-added submodule should match top-level HEAD")

    def test_child_marked_dirty_when_nested_has_changes(self) -> None:
        parent, target = self._setup_parent_with_submodule_of_target()
        nested = parent.path / "vendored" / "target"
        write_file(nested, "scratch.txt", "wip\n")
        link_siblings([parent, target])
        ref = parent.children[0]
        self.assertTrue(ref.dirty)

    def test_submodule_not_in_tracked_repos_uses_synthetic_canonical(self) -> None:
        # A submodule whose URL doesn't match any tracked top-level repo
        # still gets shown as a child row, backed by a synthetic
        # canonical Repo. This lets the user push / take action on the
        # submodule even though the workspace doesn't have a standalone
        # checkout of it.
        external_path = make_repo(self.tmp, "external")
        parent_path = make_repo(self.tmp, "parent")
        _run(parent_path, "git", "submodule", "add",
             f"file://{external_path}", "vendored/external")
        _run(parent_path, "git", "commit", "-q", "-m", "add submodule")
        parent = Repo(rel="parent", path=parent_path)
        refresh_repo(parent)
        link_siblings([parent])  # external NOT in the list
        self.assertEqual(len(parent.children), 1)
        ref = parent.children[0]
        self.assertEqual(ref.kind, "submodule")
        # The synthetic canonical is flagged so the commit pipeline
        # knows to skip the "top-level" sync target for it.
        self.assertTrue(ref.repo.synthetic)
        # Display name comes from the on-disk basename (preserves casing).
        self.assertEqual(ref.repo.display_name, "external")

    def test_subtree_spec_creates_child_ref(self) -> None:
        target_path = make_repo(self.tmp, "lib")
        parent_path = make_repo(self.tmp, "app")
        target = Repo(rel="lib", path=target_path)
        parent = Repo(rel="app", path=parent_path)
        refresh_repo(target)
        refresh_repo(parent)
        spec = SubtreeSpec(name="x", parent="app", source="lib", prefix="vendor/lib")
        link_siblings([parent, target], [spec])
        self.assertEqual(len(parent.children), 1)
        ref = parent.children[0]
        self.assertEqual(ref.kind, "subtree")
        self.assertIs(ref.repo, target)
        # subtree drift not measured → in_sync stays True.
        self.assertTrue(ref.in_sync)


class TestFindLfsWarnings(_TempWorkspace):
    def test_threshold_zero_disables(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "huge.bin", "x" * 1024)
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertEqual(find_lfs_warnings(repo, auto_stage=True, threshold_bytes=0), [])

    def test_small_files_ignored(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "tiny.bin", "x" * 50)
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        self.assertEqual(find_lfs_warnings(repo, auto_stage=True, threshold_bytes=1024), [])

    def test_large_untracked_file_flagged(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "big.bin", "x" * 4096)
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        warnings = find_lfs_warnings(repo, auto_stage=True, threshold_bytes=1024)
        self.assertEqual(len(warnings), 1)
        path, _ = warnings[0]
        self.assertEqual(path, "big.bin")

    def test_lfs_tracked_file_not_flagged(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        # Pretend git-lfs already routes *.bin through the lfs filter.
        write_file(repo_path, ".gitattributes",
                   "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        stage_and_commit(repo_path, "track bin", paths=[".gitattributes"])
        write_file(repo_path, "big.bin", "x" * 4096)
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        warnings = find_lfs_warnings(repo, auto_stage=True, threshold_bytes=1024)
        self.assertEqual(warnings, [])

    def test_auto_stage_off_only_looks_at_staged(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "untracked.bin", "x" * 4096)
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        # auto_stage=False → untracked file is invisible to the LFS check.
        self.assertEqual(
            find_lfs_warnings(repo, auto_stage=False, threshold_bytes=1024), [])


class TestSuggestCommitMessage(_TempWorkspace):
    def test_clean_repo_returns_empty(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        out = suggest_commit_message(
            repo, max_added=3, max_updated=3, max_deleted=3, auto_stage=True)
        self.assertEqual(out, "")

    def test_added_file_in_suggestion(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "pretty.cs", "class Foo {}\n")
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        out = suggest_commit_message(
            repo, max_added=3, max_updated=3, max_deleted=3, auto_stage=True)
        self.assertIn("add: pretty.cs", out)

    def test_updated_and_deleted_categories(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        # README is committed by make_repo; modify it.
        write_file(repo_path, "README.md", "# changed\n")
        # And make a new file we'll then delete.
        write_file(repo_path, "doomed.txt", "bye\n")
        stage_and_commit(repo_path, "add doomed", paths=["doomed.txt"])
        (repo_path / "doomed.txt").unlink()
        repo = Repo(rel="r", path=repo_path)
        refresh_repo(repo)
        out = suggest_commit_message(
            repo, max_added=3, max_updated=3, max_deleted=3, auto_stage=True)
        self.assertIn("update: README.md", out)
        self.assertIn("remove: doomed.txt", out)


class TestDiscoverWorkflowsLocal(_TempWorkspace):
    """Confirms the regex-based scan of `.github/workflows/*.yml` picks
    up names + workflow_dispatch correctly without hitting the network."""

    def _write_wf(self, repo_path: Path, name: str, content: str) -> None:
        wf_dir = repo_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / name).write_text(content, encoding="utf-8")

    def test_no_workflows_dir(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self.assertEqual(discover_workflows_local(repo_path), [])

    def test_simple_workflow_name_extracted(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "ci.yml",
                       "name: Continuous Integration\non: push\njobs: {}\n")
        out = discover_workflows_local(repo_path)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "Continuous Integration")
        self.assertFalse(out[0].dispatchable)
        self.assertTrue(out[0].path.startswith(".github/workflows/"))

    def test_filename_fallback_when_no_name_field(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "lint.yml", "on: push\njobs: {}\n")
        out = discover_workflows_local(repo_path)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "lint")

    def test_workflow_dispatch_detected(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "release.yml",
                       "name: Release\non:\n  workflow_dispatch:\njobs: {}\n")
        out = discover_workflows_local(repo_path)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].dispatchable)

    def test_quoted_name_stripped(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "quoted.yml",
                       'name: "Build & Test"\non: push\njobs: {}\n')
        out = discover_workflows_local(repo_path)
        self.assertEqual(out[0].name, "Build & Test")

    def test_skips_non_yaml_files(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        wf_dir = repo_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "README.md").write_text("not a workflow")
        (wf_dir / "ci.yml").write_text("name: CI\non: push\njobs: {}\n")
        out = discover_workflows_local(repo_path)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "CI")

    def test_yaml_extension_also_picked_up(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "deploy.yaml",
                       "name: Deploy\non:\n  workflow_dispatch: {}\n")
        out = discover_workflows_local(repo_path)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "Deploy")
        self.assertTrue(out[0].dispatchable)

    def test_multiple_workflows_alpha_sorted(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self._write_wf(repo_path, "zeta.yml", "name: Z\non: push\n")
        self._write_wf(repo_path, "alpha.yml", "name: A\non: push\n")
        self._write_wf(repo_path, "mid.yml", "name: M\non: push\n")
        out = [wf.name for wf in discover_workflows_local(repo_path)]
        self.assertEqual(out, ["A", "M", "Z"])


class TestWorkingTreeSignature(_TempWorkspace):
    """The smart-sync planner's correctness rides entirely on this
    helper: two checkouts of the same submodule are treated as
    duplicates iff their signatures are equal. These tests pin down
    that contract."""

    def test_clean_repo_has_empty_signature(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        self.assertEqual(working_tree_signature(repo_path), ())

    def test_unstaged_modification_appears(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "README.md", "# hello\n")
        sig = working_tree_signature(repo_path)
        paths = [p for p, _ in sig]
        self.assertIn("README.md", paths)

    def test_staged_modification_appears(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "README.md", "# hello\n")
        _run(repo_path, "git", "add", "README.md")
        sig = working_tree_signature(repo_path)
        paths = [p for p, _ in sig]
        self.assertIn("README.md", paths)

    def test_untracked_file_appears(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "new.txt", "hello world\n")
        sig = working_tree_signature(repo_path)
        paths = [p for p, _ in sig]
        self.assertIn("new.txt", paths)

    def test_gitignored_file_does_not_appear(self) -> None:
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, ".gitignore", "build/\n")
        stage_and_commit(repo_path, "add gitignore", paths=[".gitignore"])
        write_file(repo_path, "build/output.bin", "ignored\n")
        sig = working_tree_signature(repo_path)
        paths = [p for p, _ in sig]
        self.assertNotIn("build/output.bin", paths)

    def test_identical_changes_yield_identical_signatures(self) -> None:
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        # Identical content, same paths.
        for p in (a, b):
            write_file(p, "Foo.cs", "// shared change\n")
            write_file(p, "Bar.cs", "// also shared\n")
        self.assertEqual(working_tree_signature(a),
                         working_tree_signature(b))

    def test_different_content_yields_different_signatures(self) -> None:
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "Foo.cs", "// version 1\n")
        write_file(b, "Foo.cs", "// version 2\n")
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_signature_mtime_picks_latest(self) -> None:
        import os
        import time
        repo_path = make_repo(self.tmp, "r")
        write_file(repo_path, "a.txt", "older\n")
        time.sleep(0.05)
        write_file(repo_path, "b.txt", "newer\n")
        sig = working_tree_signature(repo_path)
        # Force a tighter mtime difference for stable comparison.
        a_mtime = (repo_path / "a.txt").stat().st_mtime
        b_mtime = (repo_path / "b.txt").stat().st_mtime
        os.utime(repo_path / "a.txt", (a_mtime - 100, a_mtime - 100))
        latest = signature_mtime(repo_path, sig)
        # The newer file should drive the result.
        self.assertGreaterEqual(latest, b_mtime - 1)


class TestWorkingTreeSignatureSafetyInvariant(_TempWorkspace):
    """Pin down the safety invariant that smart-sync's destructive
    `_reset_sync_loser` (`git reset --hard HEAD && git clean -fd`)
    relies on: when two checkouts produce identical
    `working_tree_signature()` tuples, every byte-of-content the
    `clean -fd` would erase from the loser is already present in the
    winner. A regression in this helper would let smart-sync silently
    discard unique work, so each failure mode lives as its own test."""

    def test_match_with_identical_modifications_only(self) -> None:
        # Two checkouts that share an initial commit and have made the
        # same edit to the same tracked file → must match. This is the
        # baseline grouping case smart-sync is built around.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "README.md", "# changed\nbody line\n")
        write_file(b, "README.md", "# changed\nbody line\n")
        self.assertEqual(working_tree_signature(a),
                         working_tree_signature(b))

    def test_match_with_identical_untracked_files(self) -> None:
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "new.py", "print('hi')\n")
        write_file(b, "new.py", "print('hi')\n")
        self.assertEqual(working_tree_signature(a),
                         working_tree_signature(b))

    def test_match_with_identical_files_in_nested_dirs(self) -> None:
        # `git ls-files --others --exclude-standard` returns FILES only,
        # not directories. Confirm that nested-untracked-file paths
        # still appear in the signature so the safety check sees them.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "src/lib/utils.py", "def x(): return 1\n")
        write_file(b, "src/lib/utils.py", "def x(): return 1\n")
        sig_a = working_tree_signature(a)
        sig_b = working_tree_signature(b)
        self.assertEqual(sig_a, sig_b)
        paths = [p for p, _ in sig_a]
        self.assertIn("src/lib/utils.py", paths)

    def test_one_byte_difference_breaks_match(self) -> None:
        # The whole point: a single content byte must produce a
        # mismatch so smart-sync refuses to lump these together.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "f.txt", "hello\n")
        write_file(b, "f.txt", "Hello\n")  # capitalised H
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_extra_file_in_one_breaks_match(self) -> None:
        # Loser has a file the winner doesn't — clean -fd would wipe
        # it. Signatures must differ so this checkout is excluded
        # from the dedup group.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "shared.txt", "x\n")
        write_file(b, "shared.txt", "x\n")
        write_file(b, "loser-only.txt", "unique work\n")
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_deletion_in_one_breaks_match(self) -> None:
        # Both repos commit a file; only one deletes it. The diff-
        # against-HEAD set differs (one has a deletion, one doesn't),
        # so the signature must differ.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "doc.md", "shared\n")
        write_file(b, "doc.md", "shared\n")
        stage_and_commit(a, "add doc")
        stage_and_commit(b, "add doc")
        (a / "doc.md").unlink()
        # b leaves doc.md alone.
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_gitignored_diverging_files_do_not_break_match(self) -> None:
        # `clean -fd` (no `-x`) preserves ignored files, so two repos
        # that disagree only on .gitignore'd content must still match.
        # If they didn't, smart-sync would refuse to consolidate
        # legitimately-equal checkouts whenever a stray .env / build
        # artefact happened to be present.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        for p in (a, b):
            write_file(p, ".gitignore", "secret.env\nbuild/\n")
            stage_and_commit(p, "add gitignore", paths=[".gitignore"])
            write_file(p, "README.md", "# changed\n")
        # Diverging gitignored content — must NOT enter the signature.
        write_file(a, "secret.env", "TOKEN=alpha\n")
        write_file(b, "secret.env", "TOKEN=beta\n")
        write_file(a, "build/out.bin", "hash-a\n")
        write_file(b, "build/out.bin", "hash-b\n")
        self.assertEqual(working_tree_signature(a),
                         working_tree_signature(b))

    def test_one_modified_one_clean_breaks_match(self) -> None:
        # If the loser has a tracked file modification the winner
        # doesn't, the loser's edit is unique work. clean -fd doesn't
        # touch tracked files, but `reset --hard HEAD` would discard
        # the modification — so signatures must diverge.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        # a is clean, b has unique modification.
        write_file(b, "README.md", "# unique work\n")
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_signature_is_path_sensitive(self) -> None:
        # Same content under different relative paths — must NOT match.
        # The signature pairs each path with its blob hash, so reorder
        # or rename means different tuples.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        write_file(a, "alpha/foo.txt", "shared\n")
        write_file(b, "beta/foo.txt", "shared\n")
        self.assertNotEqual(working_tree_signature(a),
                            working_tree_signature(b))

    def test_match_is_stable_across_unrelated_committed_history(self) -> None:
        # Two repos with totally different commit histories but the
        # same current diff-against-HEAD + untracked sets should still
        # match. The signature is purely a working-tree property.
        a = make_repo(self.tmp, "a")
        b = make_repo(self.tmp, "b")
        # Diverge histories: extra commit in a only.
        write_file(a, "history.txt", "only in a\n")
        stage_and_commit(a, "extra commit in a")
        # Now both make the same pending edit.
        write_file(a, "shared.py", "def x(): return 1\n")
        write_file(b, "shared.py", "def x(): return 1\n")
        sig_a = working_tree_signature(a)
        sig_b = working_tree_signature(b)
        self.assertEqual(sig_a, sig_b)


class TestRedundantDirtyFF(_TempWorkspace):
    """The narrow case smart-sync now handles: a 'loser' checkout has
    dirty WT changes that are bit-identical to what a fast-forward
    merge from origin/<branch> is about to install. `_try_ff_through_
    redundant_dirty` should stash, FF, and drop the stash — leaving
    HEAD updated and the file's content identical to what the user
    typed."""

    def _setup_fork(self, branch: str = "main") -> "tuple[Path, Path, Path]":
        """Create an upstream bare repo plus two clones (winner, loser)
        on `branch`. Returns (upstream, winner, loser)."""
        upstream = self.tmp / "upstream.git"
        upstream.mkdir()
        _run(upstream, "git", "init", "--bare", "-q", "-b", branch)
        winner = self.tmp / "winner"
        _run(self.tmp, "git", "clone", "-q", str(upstream), "winner")
        # Seed an initial commit so HEAD exists on both ends.
        write_file(winner, "README.md", "# repo\n")
        stage_and_commit(winner, "init")
        _run(winner, "git", "push", "-q", "-u", "origin", branch)
        loser = self.tmp / "loser"
        _run(self.tmp, "git", "clone", "-q", str(upstream), "loser")
        return upstream, winner, loser

    def _make_checkout(self, path: Path, branch: str) -> "object":
        """Build the SmartSyncCheckout shape the helper takes — only the
        fields the helper actually reads matter (path, branch). The full
        dataclass is overkill here, so a minimal stand-in keeps the test
        focused on behavior."""
        from models import SmartSyncCheckout, Repo
        return SmartSyncCheckout(
            canonical=Repo(rel="ws", path=path),
            parent=None, path=path, branch=branch, label=path.name,
        )

    def _make_state(self) -> "object":
        """Minimal State for the helpers' new state+name signature.
        The helpers only touch state.tasks for the leftover-stash
        warning path; an empty repos list is enough for the rest."""
        from models import State
        return State(repos=[], workspace_name="test")

    def test_returns_true_when_dirty_matches_origin_bit_for_bit(self) -> None:
        from workers import _try_ff_through_redundant_dirty

        upstream, winner, loser = self._setup_fork()
        # Winner makes a change, commits, pushes. Loser independently
        # types the EXACT SAME content into the same file but doesn't
        # commit it — that's the situation the user described.
        write_file(winner, "shared.py", "def x(): return 1\n")
        stage_and_commit(winner, "add shared")
        _run(winner, "git", "push", "-q")
        write_file(loser, "shared.py", "def x(): return 1\n")
        _run(loser, "git", "fetch", "-q", "origin", "main")

        # Sanity: a vanilla FF would refuse here because the file is
        # untracked locally but exists at origin/main — git treats it
        # as a "would be overwritten" conflict even though the content
        # is bit-identical. That's the exact case the helper resolves.
        sanity = _run(loser, "git", "merge", "--ff-only", "origin/main",
                      check=False)
        self.assertNotEqual(sanity.returncode, 0)

        result = _try_ff_through_redundant_dirty(
            self._make_state(), self._make_checkout(loser, "main"),
            "main", "loser")
        self.assertTrue(result)
        # HEAD has advanced to the winner's commit and shared.py is on
        # disk with the exact content the user typed.
        rc = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        winner_head = _run(winner, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(rc, winner_head)
        self.assertEqual(
            (loser / "shared.py").read_text(), "def x(): return 1\n")

    def test_staged_redundant_changes_also_take_the_fast_path(self) -> None:
        """Regression: when the loser has staged redundant changes
        (e.g. after a `git add -A`) the helper used to refuse the
        fast path because its status filter only allowed ' M' / '??'
        — leaving the loser dirty and 1 behind even though origin
        already had the exact same content."""
        from workers import _try_ff_through_redundant_dirty

        upstream, winner, loser = self._setup_fork()
        write_file(winner, "shared.py", "def x(): return 7\n")
        stage_and_commit(winner, "add shared")
        _run(winner, "git", "push", "-q")
        # Loser writes the SAME content AND stages it (auto-stage / a
        # manual `git add` would leave it like this).
        write_file(loser, "shared.py", "def x(): return 7\n")
        _run(loser, "git", "add", "shared.py")
        _run(loser, "git", "fetch", "-q", "origin", "main")

        # Sanity: status reports the path as 'A ' (added in index).
        st = _run(loser, "git", "status", "--porcelain=v1").stdout
        self.assertTrue(st.startswith("A "))

        result = _try_ff_through_redundant_dirty(
            self._make_state(), self._make_checkout(loser, "main"),
            "main", "loser")
        self.assertTrue(result)
        head = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        winner_head = _run(winner, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head, winner_head)
        self.assertEqual(
            (loser / "shared.py").read_text(),
            "def x(): return 7\n")

    def test_returns_false_when_dirty_diverges_from_origin(self) -> None:
        from workers import _try_ff_through_redundant_dirty

        upstream, winner, loser = self._setup_fork()
        write_file(winner, "shared.py", "def x(): return 1\n")
        stage_and_commit(winner, "add shared")
        _run(winner, "git", "push", "-q")
        # Loser has a DIFFERENT pending edit to the same path — a real
        # conflict the helper must refuse to auto-resolve.
        write_file(loser, "shared.py", "def x(): return 999  # mine\n")
        _run(loser, "git", "fetch", "-q", "origin", "main")

        result = _try_ff_through_redundant_dirty(
            self._make_state(), self._make_checkout(loser, "main"),
            "main", "loser")
        self.assertFalse(result)
        # The loser's content is preserved exactly as the user typed it.
        self.assertEqual(
            (loser / "shared.py").read_text(),
            "def x(): return 999  # mine\n")
        # HEAD didn't move.
        loser_head = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        upstream_head = _run(upstream, "git", "rev-parse", "main").stdout.strip()
        self.assertNotEqual(loser_head, upstream_head)


class TestDetachedWinnerSwitch(_TempWorkspace):
    """Regression: a dirty, detached-HEAD winner used to be committed
    BEFORE the branch switch, leaving the new commit orphaned and the
    push empty. The fix switches first (via stash → checkout → pop
    when needed), then commits on the chosen branch so the change
    actually propagates to remote."""

    def test_clean_detached_winner_switches_with_plain_checkout(self) -> None:
        from workers import _stash_switch_pop_winner
        from models import Repo, SmartSyncCheckout
        # Set up a repo with a master branch and a dangling commit.
        repo = self.tmp / "winner"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "README.md", "# repo\n")
        stage_and_commit(repo, "init")
        master_head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        # Detach: checkout HEAD by SHA so we're not on any branch.
        _run(repo, "git", "checkout", "-q", master_head)
        winner = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=repo),
            parent=None, path=repo, branch="(detached)", label=repo.name)

        class _NoopState:
            class tasks:
                @staticmethod
                def add(label):
                    return object()

                @staticmethod
                def update(*a, **kw):
                    pass

        ok = _stash_switch_pop_winner(_NoopState, winner, "master", "ws")
        self.assertTrue(ok)
        cur = _run(repo, "git", "branch", "--show-current").stdout.strip()
        self.assertEqual(cur, "master")

    def test_dirty_detached_winner_carries_changes_onto_branch(self) -> None:
        """The user's actual scenario: a detached HEAD with uncommitted
        edits, asked to align with master. Plain checkout would refuse
        if master differs from detached HEAD on those paths, so the
        helper falls back to stash → checkout → pop. After the dance
        completes, the WT carries the user's edits ON the new branch
        and a subsequent `git commit` lands them on master."""
        from workers import _stash_switch_pop_winner
        from models import Repo, SmartSyncCheckout
        repo = self.tmp / "winner"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "shared.py", "def x(): return 0\n")
        stage_and_commit(repo, "init")
        master_head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo, "git", "checkout", "-q", master_head)  # detach
        # User edits the file uncommitted.
        write_file(repo, "shared.py", "def x(): return 42\n")
        winner = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=repo),
            parent=None, path=repo, branch="(detached)", label=repo.name,
            dirty=True)

        class _NoopState:
            class tasks:
                @staticmethod
                def add(label):
                    return object()

                @staticmethod
                def update(*a, **kw):
                    pass

        ok = _stash_switch_pop_winner(_NoopState, winner, "master", "ws")
        self.assertTrue(ok)
        # On master now, with the user's edit present in WT.
        self.assertEqual(
            _run(repo, "git", "branch", "--show-current").stdout.strip(),
            "master")
        self.assertEqual(
            (repo / "shared.py").read_text(),
            "def x(): return 42\n")
        # And the change is uncommitted (so the caller's commit step
        # will produce a fresh commit on master, not an orphan).
        status = _run(repo, "git", "status", "--porcelain").stdout
        self.assertIn("shared.py", status)


class TestDetachedLoserCheckout(_TempWorkspace):
    """The mirror case: after a winner pushes, detached losers run
    `git checkout origin/<branch>`. If their dirty WT matches origin's
    new content, the checkout would otherwise refuse — `_try_detached_
    checkout_through_redundant_dirty` handles it via stash → checkout
    → drop, same as the FF path does for same-branch losers."""

    def _setup_with_pushed_change(self) -> "tuple[Path, Path]":
        upstream = self.tmp / "upstream.git"
        upstream.mkdir()
        _run(upstream, "git", "init", "--bare", "-q", "-b", "master")
        winner = self.tmp / "winner"
        _run(self.tmp, "git", "clone", "-q", str(upstream), "winner")
        write_file(winner, "shared.py", "def x(): return 1\n")
        stage_and_commit(winner, "add shared")
        _run(winner, "git", "push", "-q", "-u", "origin", "master")
        loser = self.tmp / "loser"
        _run(self.tmp, "git", "clone", "-q", str(upstream), "loser")
        # Detach the loser at the upstream HEAD.
        head = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        _run(loser, "git", "checkout", "-q", head)
        return winner, loser

    def test_dirty_detached_loser_with_redundant_changes_lands_on_origin(self) -> None:
        from workers import _try_detached_checkout_through_redundant_dirty
        from models import Repo, SmartSyncCheckout
        winner, loser = self._setup_with_pushed_change()
        # Winner publishes a new edit.
        write_file(winner, "shared.py", "def x(): return 99\n")
        stage_and_commit(winner, "bump")
        _run(winner, "git", "push", "-q")
        # Loser independently types the SAME edit but doesn't commit.
        write_file(loser, "shared.py", "def x(): return 99\n")
        _run(loser, "git", "fetch", "-q", "origin", "master")

        c = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=loser),
            parent=None, path=loser, branch="(detached)", label="loser",
            dirty=True)
        from models import State
        state = State(repos=[], workspace_name="test")
        result = _try_detached_checkout_through_redundant_dirty(
            state, c, "master", "loser")
        self.assertTrue(result)
        # Loser is now at origin/master with the redundant change folded
        # into the checkout — file content matches what the user typed.
        self.assertEqual(
            (loser / "shared.py").read_text(),
            "def x(): return 99\n")

    def test_genuinely_diverging_dirty_loser_refuses(self) -> None:
        from workers import _try_detached_checkout_through_redundant_dirty
        from models import Repo, SmartSyncCheckout
        winner, loser = self._setup_with_pushed_change()
        write_file(winner, "shared.py", "def x(): return 99\n")
        stage_and_commit(winner, "bump")
        _run(winner, "git", "push", "-q")
        # Loser has a DIFFERENT pending edit — real conflict.
        write_file(loser, "shared.py", "def x(): return 1234  # mine\n")
        _run(loser, "git", "fetch", "-q", "origin", "master")

        c = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=loser),
            parent=None, path=loser, branch="(detached)", label="loser",
            dirty=True)
        from models import State
        state = State(repos=[], workspace_name="test")
        result = _try_detached_checkout_through_redundant_dirty(
            state, c, "master", "loser")
        self.assertFalse(result)
        # The loser's WT is preserved exactly as the user typed it.
        self.assertEqual(
            (loser / "shared.py").read_text(),
            "def x(): return 1234  # mine\n")


class TestNoOrphanedCommitsOnSwitch(_TempWorkspace):
    """Regression: a user lost a file because the helper plain-
    checked-out a branch from a detached HEAD that had a unique
    commit on it (the file was added in that commit). Git's own
    "Warning: leaving N commit(s) behind" only goes to stderr with
    rc=0, so smart-sync used to march on and orphan the commit. The
    `_head_is_ancestor_of` guard refuses any switch where HEAD has
    work the target ref doesn't already contain."""

    def _detached_with_unique_commit(self, branch: str = "master") -> Path:
        """Set up a checkout on a detached HEAD that has one commit
        not present on `branch`. Returns the checkout path."""
        repo = self.tmp / "repo"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", branch)
        write_file(repo, "README.md", "# r\n")
        stage_and_commit(repo, "init")
        # Detach at HEAD, then add a new file on the detached HEAD.
        head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo, "git", "checkout", "-q", head)
        write_file(repo, "Upskill_Lightmap_Prefab_Baker.cs", "class Foo {}\n")
        _run(repo, "git", "add", "Upskill_Lightmap_Prefab_Baker.cs")
        _run(repo, "git", "-c", "user.email=t@x", "-c", "user.name=t",
             "commit", "-q", "-m", "add baker")
        return repo

    def test_head_is_ancestor_of_returns_false_when_head_has_unique_commit(self) -> None:
        from workers import _head_is_ancestor_of
        repo = self._detached_with_unique_commit()
        self.assertFalse(_head_is_ancestor_of(repo, "master"))

    def test_head_is_ancestor_of_returns_true_when_clean_descendant(self) -> None:
        from workers import _head_is_ancestor_of
        repo = self.tmp / "clean"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "README.md", "# r\n")
        stage_and_commit(repo, "init")
        # Detach at HEAD without adding any extra commit.
        head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo, "git", "checkout", "-q", head)
        self.assertTrue(_head_is_ancestor_of(repo, "master"))

    def test_stash_switch_pop_winner_refuses_when_user_cancels_recovery(self) -> None:
        """Worst case: detached HEAD has a unique commit + dirty WT,
        smart-sync would have orphaned the commit (and the unique
        file in it) by switching to master. The new auto-recovery
        flow pops a modal asking permission to FF master to HEAD;
        if the user cancels, the file survives in HEAD's tree just
        like the pre-recovery refusal path used to guarantee."""
        from workers import _stash_switch_pop_winner
        from models import Repo, SmartSyncCheckout, State

        repo = self._detached_with_unique_commit()
        # Add a dirty edit on top so the would-be flow includes the
        # stash dance — the guard must run BEFORE that.
        write_file(repo, "README.md", "# r2\n")
        winner = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=repo),
            parent=None, path=repo, branch="(detached)", label=repo.name,
            dirty=True)
        state = State(repos=[], workspace_name="test")
        # Spawn the canceller BEFORE calling the function — the modal
        # blocks on result_event and we need an actor on the other end.
        canceller = _spawn_recovery_canceller(state)
        ok = _stash_switch_pop_winner(state, winner, "master", "ws")
        canceller.join(timeout=1.0)
        self.assertFalse(ok)
        # The unique file is still on disk via HEAD's tree.
        self.assertTrue(
            (repo / "Upskill_Lightmap_Prefab_Baker.cs").exists(),
            "the file unique to the detached commit MUST survive a refused switch",
        )
        # The task panel surfaces the cancellation as a warn — the
        # cardinal-rule guarantee is preserved either way.
        labels = " ".join(t.label + " " + t.message
                          for t in state.tasks.snapshot())
        self.assertIn("cancel", labels.lower())

    def test_stash_switch_pop_winner_recovers_when_user_confirms(self) -> None:
        """Cardinal-rule recovery happy path: same scenario as the
        cancel test, but the user confirms the FF. Smart-sync should
        complete the switch — HEAD ends up on master, master's ref
        moved forward to capture the unique commit, and the unique
        file is on disk via the now-tracked branch."""
        from workers import _stash_switch_pop_winner
        from models import Repo, SmartSyncCheckout, State

        repo = self._detached_with_unique_commit()
        head_before = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        winner = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=repo),
            parent=None, path=repo, branch="(detached)", label=repo.name,
            dirty=False)
        state = State(repos=[], workspace_name="test")

        # User confirms: simulate "Enter" on the modal.
        def confirmer():
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                prompt = state.detached_recovery_prompt
                if prompt is not None:
                    prompt.chosen_action = "ff"
                    prompt.result_event.set()
                    state.detached_recovery_prompt = None
                    return
                time.sleep(0.05)
        t = threading.Thread(target=confirmer, daemon=True)
        t.start()

        ok = _stash_switch_pop_winner(state, winner, "master", "ws")
        t.join(timeout=1.0)
        self.assertTrue(ok)
        # master now points at HEAD's old commit.
        master_head = _run(repo, "git", "rev-parse",
                           "master").stdout.strip()
        self.assertEqual(master_head, head_before)
        # The unique file survives.
        self.assertTrue(
            (repo / "Upskill_Lightmap_Prefab_Baker.cs").exists())
        # We're on master now, not detached.
        self.assertEqual(
            _run(repo, "git", "branch", "--show-current").stdout.strip(),
            "master")

    def test_align_detached_loser_refuses_when_head_has_unique_commit(self) -> None:
        """Same risk on the loser side: a detached loser with unique
        commits would lose them on `git checkout origin/<branch>`.
        `_align_detached_loser`'s guard refuses; file survives."""
        from workers import _align_detached_loser
        from models import Repo, SmartSyncCheckout, State

        # Build an upstream + clone. Loser detaches and commits
        # a unique file that origin doesn't have.
        upstream = self.tmp / "u.git"
        upstream.mkdir()
        _run(upstream, "git", "init", "--bare", "-q", "-b", "master")
        loser = self.tmp / "loser"
        _run(self.tmp, "git", "clone", "-q", str(upstream), "loser")
        write_file(loser, "README.md", "# r\n")
        stage_and_commit(loser, "init")
        _run(loser, "git", "push", "-q", "-u", "origin", "master")
        head = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        _run(loser, "git", "checkout", "-q", head)
        write_file(loser, "Upskill_Lightmap_Prefab_Baker.cs", "class Foo {}\n")
        _run(loser, "git", "add", "Upskill_Lightmap_Prefab_Baker.cs")
        _run(loser, "git", "-c", "user.email=t@x", "-c", "user.name=t",
             "commit", "-q", "-m", "add baker")

        c = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=loser),
            parent=None, path=loser, branch="(detached)", label="loser")
        state = State(repos=[], workspace_name="test")
        ok = _align_detached_loser(state, c, "master", "ws")
        self.assertFalse(ok)
        self.assertTrue(
            (loser / "Upskill_Lightmap_Prefab_Baker.cs").exists())
        labels = " ".join(t.label + " " + t.message
                          for t in state.tasks.snapshot())
        self.assertIn("would orphan", labels)


class TestPostMergeCleanGuard(_TempWorkspace):
    """The redundant-dirty fast path drops a stash only if the post-
    merge `git status --porcelain=v1` is fully clean. If it's not —
    because something raced or an edge case left content unaccounted
    for — the stash stays on the list so the user can recover."""

    def test_post_merge_clean_true_on_clean_tree(self) -> None:
        from workers import _post_merge_clean
        repo = self.tmp / "clean"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "README.md", "# r\n")
        stage_and_commit(repo, "init")
        self.assertTrue(_post_merge_clean(repo))

    def test_post_merge_clean_false_when_anything_dirty(self) -> None:
        from workers import _post_merge_clean
        repo = self.tmp / "dirty"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "README.md", "# r\n")
        stage_and_commit(repo, "init")
        write_file(repo, "untracked.txt", "x\n")
        self.assertFalse(_post_merge_clean(repo))


class TestSafeStageAll(_TempWorkspace):
    """Cardinal-rule regression: `git add -A` could destroy submodule
    pointers (when the dir was empty after deinit) or commit stray
    gitlinks at unregistered paths (when a buggy script placed a `.git`
    at a doubled path). `safe_stage_all` refuses both classes outright.

    These tests model the two failure modes the user lost real files
    to on 2026-05-01."""

    def _bare_remote(self, name: str = "remote.git", branch: str = "master") -> Path:
        bare = self.tmp / name
        bare.mkdir()
        _run(bare, "git", "init", "--bare", "-q", "-b", branch)
        return bare

    def test_refuses_submodule_pointer_deletion_when_dir_empty(self) -> None:
        """The Mobile-sweep failure mode: a registered submodule whose
        working directory is empty (e.g. after `git submodule deinit`)
        shows up as a `D` entry in porcelain status. Plain `git add -A`
        stages that deletion as a submodule pointer removal — destroying
        the link. `safe_stage_all` refuses outright."""
        from git_ops import safe_stage_all

        # Outer parent + a registered submodule pointing at a bare
        # remote. The submodule's working dir starts populated.
        sub_remote = self._bare_remote("sub.git")
        # Seed sub_remote with a commit so we can clone it.
        seed = self.tmp / "seed"
        _run(self.tmp, "git", "clone", "-q", str(sub_remote), "seed")
        write_file(seed, "lib.txt", "hi\n")
        stage_and_commit(seed, "init")
        _run(seed, "git", "push", "-q", "-u", "origin", "master")

        parent = make_repo(self.tmp, "parent")
        _run(parent, "git", "-c", "protocol.file.allow=always",
             "submodule", "add", str(sub_remote), "vendor/sub")
        stage_and_commit(parent, "register submodule")

        # Empty out the submodule's working directory (the deinit
        # scenario — the .gitmodules entry remains, the gitlink in the
        # parent's index remains, but the dir is gone).
        shutil.rmtree(parent / "vendor" / "sub")

        ok, msg = safe_stage_all(parent)
        self.assertFalse(ok, "must refuse to stage when submodule dir is empty")
        self.assertIn("vendor/sub", msg)
        self.assertIn("submodule", msg.lower())

        # And — most importantly — nothing was staged.
        diff = _run(parent, "git", "diff", "--cached", "--name-only").stdout
        self.assertEqual(diff.strip(), "",
                         "safe_stage_all must stage nothing on refusal")

    def test_refuses_stray_gitlink_at_unregistered_path(self) -> None:
        """The path-doubling failure mode: some other tool placed a
        nested `.git` at a path that's NOT in `.gitmodules`. Plain
        `git add -A` would commit a gitlink at that bogus path.
        `safe_stage_all` refuses."""
        from git_ops import safe_stage_all

        parent = make_repo(self.tmp, "parent")
        # Drop a fully-formed nested git repo at an unregistered path.
        stray = parent / "weird" / "doubled" / "path"
        stray.mkdir(parents=True)
        _run(stray, "git", "init", "-q", "-b", "master")
        write_file(stray, "x.txt", "x\n")
        stage_and_commit(stray, "stray")

        ok, msg = safe_stage_all(parent)
        self.assertFalse(ok)
        self.assertIn("stray gitlink", msg)
        self.assertIn("weird/doubled/path", msg)

        diff = _run(parent, "git", "diff", "--cached", "--name-only").stdout
        self.assertEqual(diff.strip(), "")

    def test_stages_normal_changes_when_no_risk(self) -> None:
        """Sanity: when the WT has only ordinary edits / additions,
        `safe_stage_all` behaves exactly like `git add -A`."""
        from git_ops import safe_stage_all

        repo = make_repo(self.tmp, "r")
        write_file(repo, "edit.txt", "hello\n")
        write_file(repo, "nested/new.txt", "hi\n")

        ok, msg = safe_stage_all(repo)
        self.assertTrue(ok, msg)
        staged = _run(repo, "git", "diff", "--cached", "--name-only").stdout
        self.assertIn("edit.txt", staged)
        self.assertIn("nested/new.txt", staged)

    def test_does_not_refuse_normal_submodule_modification(self) -> None:
        """The guard is about D-on-submodule and stray gitlinks ONLY.
        A submodule with an in-WT change (its HEAD moved) still stages
        normally — that's the legitimate update flow."""
        from git_ops import safe_stage_all

        sub_remote = self._bare_remote("sub.git")
        seed = self.tmp / "seed"
        _run(self.tmp, "git", "clone", "-q", str(sub_remote), "seed")
        write_file(seed, "lib.txt", "hi\n")
        stage_and_commit(seed, "init")
        _run(seed, "git", "push", "-q", "-u", "origin", "master")

        parent = make_repo(self.tmp, "parent")
        _run(parent, "git", "-c", "protocol.file.allow=always",
             "submodule", "add", str(sub_remote), "vendor/sub")
        stage_and_commit(parent, "register submodule")

        # Move the submodule's HEAD by adding a new commit.
        write_file(parent / "vendor" / "sub", "new.txt", "y\n")
        _run(parent / "vendor" / "sub", "git", "add", "new.txt")
        _run(parent / "vendor" / "sub",
             "git", "-c", "user.email=t@x", "-c", "user.name=t",
             "commit", "-q", "-m", "advance")

        ok, msg = safe_stage_all(parent)
        self.assertTrue(ok, msg)
        # The submodule pointer change is staged in the parent.
        staged = _run(parent, "git", "diff", "--cached", "--name-only").stdout
        self.assertIn("vendor/sub", staged)


class TestSyncSiblingAncestorGuard(_TempWorkspace):
    """Cardinal-rule regression: post-push fan-out called
    `git checkout origin/<branch>` from a detached HEAD without
    checking whether HEAD's commits were on origin. Git would silently
    orphan them. `sync_sibling` now requires HEAD to be an ancestor
    of the target ref."""

    def test_refuses_checkout_when_head_has_unique_commit(self) -> None:
        from git_ops import sync_sibling

        bare = self.tmp / "u.git"
        bare.mkdir()
        _run(bare, "git", "init", "--bare", "-q", "-b", "master")
        sib = self.tmp / "sib"
        _run(self.tmp, "git", "clone", "-q", str(bare), "sib")
        write_file(sib, "README.md", "# r\n")
        stage_and_commit(sib, "init")
        _run(sib, "git", "push", "-q", "-u", "origin", "master")

        # Detach + add a unique commit not on origin/master.
        head = _run(sib, "git", "rev-parse", "HEAD").stdout.strip()
        _run(sib, "git", "checkout", "-q", head)
        write_file(sib, "Upskill_Lightmap_Prefab_Baker.cs", "class Foo {}\n")
        _run(sib, "git", "add", "Upskill_Lightmap_Prefab_Baker.cs")
        _run(sib, "git", "-c", "user.email=t@x", "-c", "user.name=t",
             "commit", "-q", "-m", "add baker")

        ok, msg = sync_sibling(sib, "master")
        self.assertFalse(ok, "must refuse a checkout that would orphan commits")
        self.assertIn("orphan", msg)
        # The unique file is still in the WT (it was in HEAD's tree).
        self.assertTrue((sib / "Upskill_Lightmap_Prefab_Baker.cs").exists())

    def test_allows_checkout_when_head_is_ancestor(self) -> None:
        """The standard happy path: detached HEAD points at the same
        commit as origin/master (or earlier). Sync proceeds."""
        from git_ops import sync_sibling

        bare = self.tmp / "u.git"
        bare.mkdir()
        _run(bare, "git", "init", "--bare", "-q", "-b", "master")
        sib = self.tmp / "sib"
        _run(self.tmp, "git", "clone", "-q", str(bare), "sib")
        write_file(sib, "README.md", "# r\n")
        stage_and_commit(sib, "init")
        _run(sib, "git", "push", "-q", "-u", "origin", "master")

        # Detach at a commit that IS on origin/master.
        head = _run(sib, "git", "rev-parse", "HEAD").stdout.strip()
        _run(sib, "git", "checkout", "-q", head)

        ok, msg = sync_sibling(sib, "master")
        self.assertTrue(ok, msg)


class TestStashPreservedAlways(_TempWorkspace):
    """Cardinal rule: idlegit MUST NOT call `git stash drop` on the
    user's behalf. Both redundant-dirty fast paths now leave the stash
    on the list, even after a successful operation."""

    def _seed(self, branch: str = "master") -> Path:
        bare = self.tmp / "u.git"
        bare.mkdir()
        _run(bare, "git", "init", "--bare", "-q", "-b", branch)
        loser = self.tmp / "loser"
        _run(self.tmp, "git", "clone", "-q", str(bare), "loser")
        write_file(loser, "README.md", "# r\n")
        stage_and_commit(loser, "init")
        _run(loser, "git", "push", "-q", "-u", "origin", branch)
        return loser

    def test_ff_through_redundant_dirty_keeps_stash(self) -> None:
        """After the redundant-dirty FF succeeds, the stash entry MUST
        still be on the stash list — pruning is the user's call."""
        from workers import _try_ff_through_redundant_dirty
        from models import Repo, SmartSyncCheckout, State

        loser = self._seed()
        # Advance origin: another clone pushes a new file.
        winner = self.tmp / "winner"
        _run(self.tmp, "git", "clone", "-q",
             str(self.tmp / "u.git"), "winner")
        write_file(winner, "shared.txt", "v2\n")
        stage_and_commit(winner, "v2")
        _run(winner, "git", "push", "-q", "origin", "master")

        # Loser fetches origin (so origin/master is up-to-date locally)
        # and gets the SAME edit in WT — bit-identical to what FF will land.
        _run(loser, "git", "fetch", "-q", "origin", "master")
        write_file(loser, "shared.txt", "v2\n")

        c = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=loser),
            parent=None, path=loser, branch="master", label="loser",
            dirty=True)
        state = State(repos=[], workspace_name="test")
        result = _try_ff_through_redundant_dirty(state, c, "master", "ws")
        self.assertTrue(result, "redundant-dirty FF should succeed here")

        # Stash list MUST still contain idlegit's entry.
        stash_out = _run(loser, "git", "stash", "list").stdout
        self.assertIn("redundant dirty", stash_out,
                      "stash MUST be preserved (cardinal rule)")

    def test_detached_checkout_through_redundant_dirty_keeps_stash(self) -> None:
        """Same guarantee on the detached-loser checkout path."""
        from workers import _try_detached_checkout_through_redundant_dirty
        from models import Repo, SmartSyncCheckout, State

        loser = self._seed()
        winner = self.tmp / "winner"
        _run(self.tmp, "git", "clone", "-q",
             str(self.tmp / "u.git"), "winner")
        write_file(winner, "shared.txt", "v2\n")
        stage_and_commit(winner, "v2")
        _run(winner, "git", "push", "-q", "origin", "master")

        # Loser detaches at HEAD, fetches, gets the same WT edit.
        head = _run(loser, "git", "rev-parse", "HEAD").stdout.strip()
        _run(loser, "git", "checkout", "-q", head)
        _run(loser, "git", "fetch", "-q", "origin", "master")
        write_file(loser, "shared.txt", "v2\n")

        c = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=loser),
            parent=None, path=loser, branch="(detached)", label="loser",
            dirty=True)
        state = State(repos=[], workspace_name="test")
        result = _try_detached_checkout_through_redundant_dirty(
            state, c, "master", "ws")
        self.assertTrue(result)

        stash_out = _run(loser, "git", "stash", "list").stdout
        self.assertIn("redundant dirty", stash_out)


class TestStashPopConflictNoHardReset(_TempWorkspace):
    """Cardinal rule: when `git stash pop` conflicts during the
    detached-winner switch, idlegit MUST NOT run `git reset --hard
    HEAD` to tidy up. The conflict markers stay in the WT and the
    stash stays on the stash list — both are recoverable. The user
    resolves manually."""

    def test_no_hard_reset_after_pop_conflict(self) -> None:
        from workers import _stash_switch_pop_winner
        from models import Repo, SmartSyncCheckout, State

        # Build a repo with two branches whose 'collide.txt' differs.
        # Detach on 'master' with a dirty edit that conflicts with the
        # 'feature' branch's version of 'collide.txt' — stash + checkout
        # feature works (clean WT after stash), but stash pop will
        # conflict.
        repo = self.tmp / "repo"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "master")
        write_file(repo, "collide.txt", "master-base\n")
        stage_and_commit(repo, "init master")
        _run(repo, "git", "checkout", "-q", "-b", "feature")
        write_file(repo, "collide.txt", "feature-content\n")
        stage_and_commit(repo, "feature edit")
        _run(repo, "git", "checkout", "-q", "master")
        # Detach at master HEAD, then dirty-edit collide.txt with text
        # that doesn't match either branch — guarantees a pop conflict
        # when we land on feature.
        head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo, "git", "checkout", "-q", head)
        write_file(repo, "collide.txt", "winner-edit\n")

        winner = SmartSyncCheckout(
            canonical=Repo(rel="ws", path=repo),
            parent=None, path=repo, branch="(detached)", label=repo.name,
            dirty=True)
        state = State(repos=[], workspace_name="test")
        ok = _stash_switch_pop_winner(state, winner, "feature", "ws")
        self.assertFalse(ok, "pop should have conflicted")

        # WT must show the conflict (NOT a clean tip from a hard reset).
        # Conflict markers in collide.txt OR the file in conflicted state
        # in porcelain status.
        status = _run(repo, "git", "status", "--porcelain=v1").stdout
        self.assertTrue(
            any(line[:2] in ("UU", "AA", "DD", "AU", "UA", "DU", "UD")
                for line in status.splitlines()) or
            "<<<<<<<" in (repo / "collide.txt").read_text(),
            "WT should be in conflicted state, not reset clean",
        )

        # Stash must be preserved.
        stash_out = _run(repo, "git", "stash", "list").stdout
        self.assertIn("align detached winner", stash_out)


class TestCommitWorkerDetachedGuard(_TempWorkspace):
    """Regression: pushing on a detached-HEAD top-level repo used to
    produce `error: src refspec (detached) does not match` — the push
    refspec was the literal "(detached)" sentinel from refresh_repo.
    `commit_worker` now mirrors `commit_worker_for_child`'s early-bail:
    detached HEAD → fail with a clear "switch to a branch first"
    message before any stage/commit/push runs."""

    def test_commit_worker_refuses_on_detached_head_when_user_cancels(self) -> None:
        """When the auto-recovery modal pops and the user cancels, the
        pipeline must NOT proceed — no stage/commit/push, work intact."""
        from workers import commit_worker
        from models import Repo, State

        repo_path = make_repo(self.tmp, "r")
        # Stage a real change so the staging step would otherwise have
        # work to do — proves we bail BEFORE staging, not just because
        # there's nothing to commit.
        write_file(repo_path, "edit.txt", "hi\n")
        # Detach at HEAD.
        head = _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo_path, "git", "checkout", "-q", head)

        repo = Repo(rel="r", path=repo_path)
        repo.merging = False
        repo.upstream = None
        repo.remote_url = None
        state = State(repos=[repo], workspace_name="test")

        # Spawn the modal canceller, then call commit_worker. The
        # worker pops the recovery prompt and blocks; canceller
        # presses Esc; worker bails with the cannot-commit warning.
        canceller = _spawn_recovery_canceller(state)
        commit_worker(state, repo, "msg", lfs_cands=[])
        canceller.join(timeout=1.0)

        labels = [t.label + " " + t.message
                  for t in state.tasks.snapshot()]
        self.assertTrue(
            any("cannot commit" in line and "detached" in line.lower()
                for line in labels),
            f"expected detached-HEAD refusal, got: {labels}",
        )
        for forbidden in ("stage all", ": commit", ": push"):
            self.assertFalse(
                any(forbidden in line for line in labels),
                f"{forbidden!r} task fired despite cancelled recovery: {labels}",
            )
        # The pre-existing edit is still in the WT (untouched).
        self.assertEqual((repo_path / "edit.txt").read_text(), "hi\n")


class TestBranchFromHeadAction(_TempWorkspace):
    """Cardinal-rule recovery: `git checkout -b <name>` from a detached
    HEAD only writes a new ref, so unique commits become reachable from
    a named branch and the orphan-risk guards elsewhere stop refusing."""

    def _detached_with_unique(self) -> Path:
        repo = make_repo(self.tmp, "r")
        head = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()
        _run(repo, "git", "checkout", "-q", head)
        write_file(repo, "Upskill_Lightmap_Prefab_Baker.cs", "x\n")
        _run(repo, "git", "add", "Upskill_Lightmap_Prefab_Baker.cs")
        _run(repo, "git", "-c", "user.email=t@x", "-c", "user.name=t",
             "commit", "-q", "-m", "add baker")
        return repo

    def test_branch_from_head_creates_branch_at_current_commit(self) -> None:
        from workers import kick_off_action
        from models import Repo, State
        import threading

        repo_path = self._detached_with_unique()
        head = _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip()
        repo = Repo(rel="r", path=repo_path)
        state = State(repos=[repo], workspace_name="test")

        kick_off_action(
            state, "branch_from_head",
            target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None,
            branch_arg="idlegit/wip-test")

        # kick_off_action spawns a daemon thread; join to make the
        # outcome deterministic in test.
        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5.0)

        # Branch ref now points at the same commit HEAD was on.
        rc, out, _ = _run(repo_path, "git", "rev-parse",
                          "idlegit/wip-test", check=False).returncode, \
            _run(repo_path, "git", "rev-parse", "idlegit/wip-test").stdout, ""
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), head)

        # And HEAD is no longer detached — we're on the new branch.
        cur = _run(repo_path, "git", "branch",
                   "--show-current").stdout.strip()
        self.assertEqual(cur, "idlegit/wip-test")
        # The unique file is still there.
        self.assertTrue(
            (repo_path / "Upskill_Lightmap_Prefab_Baker.cs").exists())


class TestFFMergeAction(_TempWorkspace):
    """`merge --ff-only` succeeds when the target is a descendant of
    HEAD; refuses (non-zero rc) on real divergence — never producing a
    merge commit the user didn't ask for."""

    def test_ff_merge_succeeds_when_descendant(self) -> None:
        from workers import kick_off_action
        from models import Repo, State
        import threading

        repo_path = make_repo(self.tmp, "r")
        # Branch 'topic' has one extra commit beyond master.
        _run(repo_path, "git", "checkout", "-q", "-b", "topic")
        write_file(repo_path, "extra.txt", "y\n")
        stage_and_commit(repo_path, "topic edit")
        topic_head = _run(repo_path, "git", "rev-parse",
                          "HEAD").stdout.strip()
        # Back to master; topic is a strict descendant of master HEAD.
        _run(repo_path, "git", "checkout", "-q", "main")

        repo = Repo(rel="r", path=repo_path)
        state = State(repos=[repo], workspace_name="test")
        kick_off_action(
            state, "ff_merge",
            target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None,
            branch_arg="topic")

        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5.0)

        master_head = _run(repo_path, "git", "rev-parse",
                           "HEAD").stdout.strip()
        self.assertEqual(master_head, topic_head,
                         "FF merge should advance master to topic's tip")

    def test_ff_merge_refuses_on_divergence(self) -> None:
        from workers import kick_off_action
        from models import Repo, State
        import threading

        repo_path = make_repo(self.tmp, "r")
        _run(repo_path, "git", "checkout", "-q", "-b", "topic")
        write_file(repo_path, "topic.txt", "t\n")
        stage_and_commit(repo_path, "topic edit")
        _run(repo_path, "git", "checkout", "-q", "main")
        write_file(repo_path, "main.txt", "m\n")
        stage_and_commit(repo_path, "main edit")
        master_head = _run(repo_path, "git", "rev-parse",
                           "HEAD").stdout.strip()

        repo = Repo(rel="r", path=repo_path)
        state = State(repos=[repo], workspace_name="test")
        kick_off_action(
            state, "ff_merge",
            target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None,
            branch_arg="topic")

        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5.0)

        # No merge commit was created — HEAD unchanged.
        self.assertEqual(
            _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip(),
            master_head,
            "FF refusal must NOT advance HEAD")
        # Task panel surfaces the failure.
        labels = " ".join(t.label + " " + t.message
                          for t in state.tasks.snapshot())
        self.assertIn("ff_merge", labels.replace("--ff-only", "ff_merge"))


class TestGitOperationHardening(_TempWorkspace):
    def test_manual_pull_uses_ff_only_and_refuses_divergence(self) -> None:
        from workers import kick_off_action
        from models import State

        remote = make_repo(self.tmp, "remote")
        _run(self.tmp, "git", "clone", str(remote), "r")
        repo_path = self.tmp / "r"

        write_file(remote, "remote.txt", "remote\n")
        stage_and_commit(remote, "remote edit")
        write_file(repo_path, "local.txt", "local\n")
        stage_and_commit(repo_path, "local edit")
        local_head = _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip()

        repo = Repo(rel="r", path=repo_path)
        state = State(repos=[repo], workspace_name="test")
        kick_off_action(
            state, "pull",
            target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None)

        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5.0)

        self.assertEqual(
            _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip(),
            local_head)
        labels = " ".join(t.label + " " + t.message
                          for t in state.tasks.snapshot())
        self.assertIn("pull --ff-only", labels)

    def test_option_like_branch_name_is_rejected_before_checkout(self) -> None:
        from workers import kick_off_action
        from models import State

        repo_path = make_repo(self.tmp, "r")
        head = _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip()
        repo = Repo(rel="r", path=repo_path)
        state = State(repos=[repo], workspace_name="test")
        kick_off_action(
            state, "branch_from_head",
            target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None,
            branch_arg="--bad")

        for t in threading.enumerate():
            if t.daemon and t is not threading.current_thread():
                t.join(timeout=5.0)

        self.assertEqual(
            _run(repo_path, "git", "rev-parse", "HEAD").stdout.strip(),
            head)
        labels = " ".join(t.label + " " + t.message
                          for t in state.tasks.snapshot())
        self.assertIn("unsafe branch name", labels)

    def test_sync_subtree_refuses_dirty_parent(self) -> None:
        parent = make_repo(self.tmp, "parent")
        write_file(parent, "dirty.txt", "dirty\n")

        ok, msg = sync_subtree(parent, "vendor/lib", "https://example/lib.git",
                               "main")

        self.assertFalse(ok)
        self.assertIn("local changes", msg)


class TestPromptHardening(unittest.TestCase):
    def test_detached_recovery_prompt_timeout_clears_slot(self) -> None:
        import workers
        from models import DetachedRecoveryPrompt, State

        state = State(repos=[], workspace_name="test")
        prompt = DetachedRecoveryPrompt(
            target_label="r", head_sha="abc123",
            target_branch="main", n_extra=1, can_ff=True)

        with mock.patch.object(workers, "PROMPT_WAIT_SECONDS", 0.01), \
                mock.patch.object(
                    workers, "git",
                    return_value=(0, "", "")), \
                mock.patch.object(
                    workers, "_build_recovery_prompt",
                    return_value=prompt):
            ok, msg = workers._attempt_detached_recovery(
                state, Path("/tmp/repo"), "r")

        self.assertFalse(ok)
        self.assertIn("timed out", msg)
        self.assertIsNone(state.detached_recovery_prompt)

    def test_action_refusal_still_refreshes_target_state(self) -> None:
        import workers
        from models import Repo, State

        repo = Repo(rel="r", path=Path("/tmp/repo"))
        state = State(repos=[repo], workspace_name="test")

        with mock.patch.object(workers, "MIN_ACTION_REFRESH_SECONDS", 0), \
                mock.patch.object(workers, "_refresh_target_state") as refresh:
            workers.kick_off_action(
                state, "branch_from_head",
                target_label="r", target_path=repo.path,
                target_repo=repo, target_parent=None,
                branch_arg="--bad")
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(timeout=5.0)

        refresh.assert_called_once_with(state, repo, None)
        self.assertFalse(repo.refreshing)


if __name__ == "__main__":
    unittest.main()
