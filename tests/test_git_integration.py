"""Integration tests that drive real `git` against temp repos. Slower
than the pure-helper tests, but they're the only ones that can prove
discover_repos / link_siblings / refresh_repo / find_lfs_warnings /
suggest_commit_message actually behave the same as git itself."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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
    working_tree_signature,
)
from models import Repo, SubtreeSpec  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
