"""Safe-merge tests: conflict parsing, byte-exact resolution, the
Cardinal-Rule no-destructive-ops guarantee, backup-stash handling, and the
full begin→resolve→commit worker flow against real temp git repos."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import _run, make_repo, stage_and_commit, write_file  # noqa: E402
from core import git_ops, workers  # noqa: E402
from core.git_ops import (  # noqa: E402
    _parse_conflict_markers, begin_safe_merge, complete_safe_merge_commit,
    create_named_stash, describe_merge_side, drop_named_stash,
    is_fast_forward_merge, list_stashes, merge_head_sha,
    parse_safe_merge_conflicts, rebuild_resolved_text,
    remaining_conflict_paths, write_conflict_resolution,
)
from core.models import ConflictFile, Repo, State  # noqa: E402


def _cf_from_text(text: str, choices) -> ConflictFile:
    """Parse `text` into a ConflictFile and apply per-hunk `choices`."""
    parsed = _parse_conflict_markers(text)
    assert parsed is not None
    parts, hunks = parsed
    cf = ConflictFile(path="x", kind="text", parts=parts, hunks=hunks)
    for hunk, choice in zip(cf.hunks, choices):
        hunk.choice = choice
    return cf


class TestMarkerParser(unittest.TestCase):
    """Pure-function tests for _parse_conflict_markers / rebuild — no git."""

    def test_crlf_is_byte_exact(self) -> None:
        text = ("a\r\n<<<<<<< HEAD\r\nours\r\n=======\r\n"
                "theirs\r\n>>>>>>> feature\r\nb\r\n")
        cf = _cf_from_text(text, ["ours"])
        self.assertEqual(rebuild_resolved_text(cf), "a\r\nours\r\nb\r\n")

    def test_no_trailing_newline(self) -> None:
        text = ("top\n<<<<<<< HEAD\nO\n=======\nT\n>>>>>>> f\nbottom")
        cf = _cf_from_text(text, ["theirs"])
        self.assertEqual(rebuild_resolved_text(cf), "top\nT\nbottom")

    def test_two_way_no_base(self) -> None:
        text = ("k\n<<<<<<< HEAD\nO\n=======\nT\n>>>>>>> f\n")
        parsed = _parse_conflict_markers(text)
        self.assertIsNotNone(parsed)
        _parts, hunks = parsed
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].base, [])  # 2-way style has no base

    def test_markdown_underline_not_a_separator(self) -> None:
        # A 10-char `=` underline inside the ours side must NOT be read as
        # the conflict separator — only an exactly-7 (or 7+space) line is.
        text = ("intro\n<<<<<<< HEAD\nTitle\n==========\nbody-ours\n"
                "=======\ntheirs\n>>>>>>> f\ntail\n")
        cf = _cf_from_text(text, ["ours"])
        self.assertEqual(len(cf.hunks), 1)
        self.assertEqual(
            cf.hunks[0].ours, ["Title\n", "==========\n", "body-ours\n"])
        self.assertEqual(
            rebuild_resolved_text(cf),
            "intro\nTitle\n==========\nbody-ours\ntail\n")

    def test_no_markers_returns_none(self) -> None:
        self.assertIsNone(_parse_conflict_markers("just\nplain\ntext\n"))

    def test_content_marker_line_rejects_parse(self) -> None:
        # A literal `=======` line in the ours-side content makes the
        # marker counts disagree with the hunk count → reject (so the
        # caller falls back to a safe whole-file pick) rather than
        # silently mis-splitting the hunk.
        text = ("<<<<<<< HEAD\nfoo\n=======\nbar\n=======\n"
                "theirs\n>>>>>>> f\n")
        self.assertIsNone(_parse_conflict_markers(text))

# Commands the Cardinal Rule forbids idlegit from ever issuing.
_BANNED = [
    ("reset", "--hard"),
    ("push", "--force"),
    ("push", "--force-with-lease"),
    ("checkout", "--"),
    ("merge", "--abort"),
    ("clean",),
    ("rebase",),
    ("branch", "-D"),
    ("filter-branch",),
    ("stash", "drop"),     # only allowed behind the explicit opt-in box
    ("stash", "clear"),
]


def _is_banned(argv):
    for combo in _BANNED:
        if all(tok in argv for tok in combo):
            return combo
    return None


def _conflict_repo(tmp: Path, *, name="r") -> Path:
    """A repo whose `main` and `feature` both edit f.txt's lines 2 and 5
    differently, so merging feature into main conflicts in two hunks."""
    repo = make_repo(tmp, name, branch="main")
    write_file(repo, "f.txt", "alpha\nbeta\ngamma\ndelta\nepsilon\n")
    stage_and_commit(repo, "base")
    _run(repo, "git", "checkout", "-q", "-b", "feature")
    write_file(repo, "f.txt", "alpha\nB-theirs\ngamma\ndelta\nE-theirs\n")
    stage_and_commit(repo, "theirs")
    _run(repo, "git", "checkout", "-q", "main")
    write_file(repo, "f.txt", "alpha\nB-ours\ngamma\ndelta\nE-ours\n")
    stage_and_commit(repo, "ours")
    return repo


class TestConflictParsing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="idlegit-sm-")
        self.tmp = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_two_hunks_with_diff3_base(self) -> None:
        repo = _conflict_repo(self.tmp)
        rc, _out, _err = begin_safe_merge(repo, "feature")
        self.assertNotEqual(rc, 0)  # conflicts → non-zero is expected
        self.assertIsNotNone(merge_head_sha(repo))
        files = parse_safe_merge_conflicts(repo)
        self.assertEqual([f.path for f in files], ["f.txt"])
        cf = files[0]
        self.assertEqual(cf.kind, "text")
        self.assertEqual(len(cf.hunks), 2)
        # diff3 conflict style records the merge base inside the markers.
        self.assertEqual(cf.hunks[0].base, ["beta\n"])
        self.assertEqual(cf.hunks[0].ours, ["B-ours\n"])
        self.assertEqual(cf.hunks[0].theirs, ["B-theirs\n"])

    def test_side_labels(self) -> None:
        repo = _conflict_repo(self.tmp)
        begin_safe_merge(repo, "feature")
        ours = describe_merge_side(repo, "HEAD", "ours")
        theirs = describe_merge_side(repo, "MERGE_HEAD", "theirs",
                                     branch_label="feature")
        self.assertEqual(ours.branch, "main")
        self.assertEqual(theirs.branch, "feature")
        self.assertTrue(ours.short_sha)
        self.assertEqual(theirs.subject, "theirs")

    def test_resolution_is_byte_exact(self) -> None:
        repo = _conflict_repo(self.tmp)
        begin_safe_merge(repo, "feature")
        cf = parse_safe_merge_conflicts(repo)[0]
        cf.hunks[0].choice = "ours"
        cf.hunks[1].choice = "theirs"
        # Pure rebuild matches what gets written.
        self.assertEqual(
            rebuild_resolved_text(cf),
            "alpha\nB-ours\ngamma\ndelta\nE-theirs\n")
        ok, detail = write_conflict_resolution(repo, cf)
        self.assertTrue(ok, detail)
        self.assertEqual(
            (repo / "f.txt").read_text(),
            "alpha\nB-ours\ngamma\ndelta\nE-theirs\n")
        self.assertEqual(remaining_conflict_paths(repo), [])
        rc, _o, err = complete_safe_merge_commit(repo)
        self.assertEqual(rc, 0, err)
        # A real merge commit: HEAD has two parents.
        parents = _run(
            repo, "git", "rev-list", "--parents", "-n", "1", "HEAD"
        ).stdout.split()
        self.assertEqual(len(parents), 3)

    def test_both_concatenates(self) -> None:
        repo = _conflict_repo(self.tmp)
        begin_safe_merge(repo, "feature")
        cf = parse_safe_merge_conflicts(repo)[0]
        cf.hunks[0].choice = "both"
        cf.hunks[1].choice = "ours"
        self.assertEqual(
            rebuild_resolved_text(cf),
            "alpha\nB-ours\nB-theirs\ngamma\ndelta\nE-ours\n")

    def test_undecided_blocks_commit(self) -> None:
        repo = _conflict_repo(self.tmp)
        begin_safe_merge(repo, "feature")
        cf = parse_safe_merge_conflicts(repo)[0]
        cf.hunks[0].choice = "ours"  # hunk 1 left undecided
        self.assertIsNone(rebuild_resolved_text(cf))
        ok, _ = write_conflict_resolution(repo, cf)
        self.assertFalse(ok)
        # Index still unmerged → commit refuses.
        rc, _o, _e = complete_safe_merge_commit(repo)
        self.assertNotEqual(rc, 0)

    def test_modify_delete_is_manual(self) -> None:
        repo = make_repo(self.tmp, "md", branch="main")
        write_file(repo, "g.txt", "one\ntwo\n")
        stage_and_commit(repo, "base")
        _run(repo, "git", "checkout", "-q", "-b", "feature")
        _run(repo, "git", "rm", "-q", "g.txt")
        stage_and_commit(repo, "delete on feature")
        _run(repo, "git", "checkout", "-q", "main")
        write_file(repo, "g.txt", "one\nTWO-ours\n")
        stage_and_commit(repo, "modify on main")
        begin_safe_merge(repo, "feature")
        files = parse_safe_merge_conflicts(repo)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].kind, "manual")
        # idlegit refuses to auto-resolve (never deletes on the user's behalf)
        ok, _ = write_conflict_resolution(repo, files[0])
        self.assertFalse(ok)

    def test_binary_whole_file_pick(self) -> None:
        repo = make_repo(self.tmp, "bin", branch="main")
        (repo / "b.bin").write_bytes(b"\x00\x01base\x00")
        stage_and_commit(repo, "base")
        _run(repo, "git", "checkout", "-q", "-b", "feature")
        (repo / "b.bin").write_bytes(b"\x00\x01THEIRS\x00\xff")
        stage_and_commit(repo, "theirs")
        _run(repo, "git", "checkout", "-q", "main")
        (repo / "b.bin").write_bytes(b"\x00\x01OURS\x00\xfe")
        stage_and_commit(repo, "ours")
        begin_safe_merge(repo, "feature")
        files = parse_safe_merge_conflicts(repo)
        self.assertEqual(len(files), 1)
        cf = files[0]
        self.assertEqual(cf.kind, "binary")
        cf.whole_choice = "theirs"
        ok, detail = write_conflict_resolution(repo, cf)
        self.assertTrue(ok, detail)
        self.assertEqual((repo / "b.bin").read_bytes(), b"\x00\x01THEIRS\x00\xff")

    def test_fast_forward_detection(self) -> None:
        repo = make_repo(self.tmp, "ff", branch="main")
        write_file(repo, "f.txt", "a\n")
        stage_and_commit(repo, "base")
        _run(repo, "git", "checkout", "-q", "-b", "feature")
        write_file(repo, "f.txt", "a\nb\n")
        stage_and_commit(repo, "ahead")
        _run(repo, "git", "checkout", "-q", "main")
        # main is behind feature with no divergence → fast-forwardable.
        self.assertTrue(is_fast_forward_merge(repo, "feature"))
        # After main diverges, it's no longer a fast-forward.
        write_file(repo, "f.txt", "a\nc\n")
        stage_and_commit(repo, "diverge")
        self.assertFalse(is_fast_forward_merge(repo, "feature"))


class TestBackupStash(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="idlegit-sm-")
        self.tmp = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_create_and_drop_named_stash(self) -> None:
        repo = make_repo(self.tmp, "s", branch="main")
        write_file(repo, "f.txt", "a\n")
        stage_and_commit(repo, "base")
        # Clean tree → nothing to stash.
        status, _ = create_named_stash(repo, "pre-merge-at-x")
        self.assertEqual(status, "empty")
        # Dirty tree → a named stash appears and is droppable by name.
        write_file(repo, "f.txt", "a\ndirty\n")
        status, _ = create_named_stash(repo, "pre-merge-at-y")
        self.assertEqual(status, "created")
        self.assertTrue(any("pre-merge-at-y" in msg
                            for _ref, msg in list_stashes(repo)))
        ok, _ = drop_named_stash(repo, "pre-merge-at-y")
        self.assertTrue(ok)
        self.assertFalse(any("pre-merge-at-y" in msg
                             for _ref, msg in list_stashes(repo)))


class TestNoDestructiveOps(unittest.TestCase):
    """Record every git argv the safe-merge git layer issues during a full
    flow and assert none match a Cardinal-Rule-banned command."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="idlegit-sm-")
        self.tmp = Path(self._tmp)
        self.calls: list = []
        self._real_git = git_ops.git
        self._real_cancellable = git_ops.git_cancellable

        def rec_git(path, args, *a, **k):
            self.calls.append(list(args))
            return self._real_git(path, args, *a, **k)

        def rec_cancellable(path, args, *a, **k):
            self.calls.append(list(args))
            return self._real_cancellable(path, args, *a, **k)

        git_ops.git = rec_git
        git_ops.git_cancellable = rec_cancellable

    def tearDown(self) -> None:
        git_ops.git = self._real_git
        git_ops.git_cancellable = self._real_cancellable
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _assert_clean(self) -> None:
        for argv in self.calls:
            banned = _is_banned(argv)
            self.assertIsNone(
                banned, f"banned git command issued: {argv} (matched {banned})")

    def test_full_flow_issues_no_destructive_commands(self) -> None:
        repo = _conflict_repo(self.tmp)
        # Dirty the tree so the backup stash actually runs.
        write_file(repo, "extra.txt", "untracked\n")
        create_named_stash(repo, "pre-merge-at-test")
        begin_safe_merge(repo, "feature")
        cf = parse_safe_merge_conflicts(repo)[0]
        cf.hunks[0].choice = "ours"
        cf.hunks[1].choice = "theirs"
        write_conflict_resolution(repo, cf)
        complete_safe_merge_commit(repo)
        self.assertEqual(remaining_conflict_paths(repo), [])
        self._assert_clean()
        # Sanity: we really did exercise merge + commit.
        joined = [" ".join(a) for a in self.calls]
        self.assertTrue(any("merge --no-ff --no-commit" in j for j in joined))
        self.assertTrue(any(j.startswith("commit") for j in joined))


class TestWorkerFlow(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="idlegit-sm-")
        self.tmp = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _wait(self, pred, timeout=10.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_begin_resolve_finalize_confirm(self) -> None:
        repo_path = _conflict_repo(self.tmp)
        repo = Repo(rel="r", path=repo_path)
        git_ops.refresh_repo(repo)
        state = State(repos=[repo], workspace_name="t")

        opened = workers.kick_off_safe_merge(
            state, target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None, merge_ref="feature")
        self.assertTrue(opened)
        screen = state.safe_merge
        self.assertTrue(self._wait(
            lambda: screen.phase in ("resolve", "error", "confirm")))
        self.assertEqual(screen.phase, "resolve")
        self.assertEqual(len(screen.decisions), 2)
        # Refresh slot held for the flow.
        self.assertTrue(screen.repo_locked)

        for cf in screen.files:
            for hunk in cf.hunks:
                hunk.choice = "ours"
        workers.kick_off_safe_merge_finalize(state, screen)
        self.assertTrue(self._wait(lambda: screen.phase == "confirm"))
        self.assertTrue(screen.commit_sha)

        screen.confirm_push = False  # no remote in this fixture
        screen.confirm_remove_stash = False
        workers.kick_off_safe_merge_confirm(state, screen)
        self.assertTrue(self._wait(lambda: screen.phase == "done"))
        # Lock released, header task terminal, merge concluded.
        self.assertFalse(screen.repo_locked)
        self.assertIn(screen.header_task.status, ("ok", "warn"))
        self.assertIsNone(merge_head_sha(repo_path))

    def test_adopt_existing_merge(self) -> None:
        repo_path = _conflict_repo(self.tmp)
        # Start a merge OUTSIDE idlegit, leaving conflicts in the tree.
        _run(repo_path, "git", "merge", "feature", check=False)
        self.assertIsNotNone(merge_head_sha(repo_path))
        repo = Repo(rel="r", path=repo_path)
        git_ops.refresh_repo(repo)
        state = State(repos=[repo], workspace_name="t")

        opened = workers.kick_off_safe_merge(
            state, target_label="r", target_path=repo_path,
            target_repo=repo, target_parent=None, merge_ref="")
        self.assertTrue(opened)
        screen = state.safe_merge
        self.assertTrue(self._wait(lambda: screen.phase == "resolve"))
        self.assertEqual(screen.backup_stash_name, "")  # no stash mid-merge
        # The conflict is adopted and parsed; an externally-driven `git
        # merge` (default 2-way style) may group the two changed regions
        # into one hunk where begin_safe_merge's diff3 split them — either
        # way the file is resolvable here.
        self.assertEqual([f.path for f in screen.files], ["f.txt"])
        self.assertGreaterEqual(len(screen.decisions), 1)


if __name__ == "__main__":
    unittest.main()
