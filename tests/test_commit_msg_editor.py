"""Tests for the Shift+Right commit-message editor modal.

Pure-logic tests only: cursor math (flat ↔ row/col), open gating
(only fires on dirty editable holders), key dispatch (close on
Enter/Esc/Tab, typing mutates the underlying holder.message in place,
arrows / Home / End / Backspace / Delete behave correctly across
newlines)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Curses is a hard dep for the modal module; skip the whole file when
# the platform doesn't ship it (same gate as test_workspaces).
try:
    import curses  # noqa: F401
    from features.commit_msg_editor.actions import (  # noqa: E402
        handle_commit_msg_editor_key,
    )
    from features.commit_msg_editor.projection import (  # noqa: E402
        _cursor_to_row_col, _row_col_to_cursor,
    )
    from features.commit_msg_editor.session import (  # noqa: E402
        open_commit_msg_editor,
    )
    UI_AVAILABLE = True
except Exception:
    UI_AVAILABLE = False

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.state.edit_buffers import CommitMsgEditor  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402


@unittest.skipUnless(UI_AVAILABLE, "curses unavailable")
class TestCursorMath(unittest.TestCase):
    def test_empty_message(self) -> None:
        self.assertEqual(_cursor_to_row_col("", 0), (0, 0))
        self.assertEqual(_row_col_to_cursor("", 0, 0), 0)

    def test_single_line_cursor_round_trips(self) -> None:
        msg = "hello world"
        for i in range(len(msg) + 1):
            row, col = _cursor_to_row_col(msg, i)
            self.assertEqual(row, 0)
            self.assertEqual(col, i)
            self.assertEqual(_row_col_to_cursor(msg, row, col), i)

    def test_multi_line_cursor(self) -> None:
        # "abc\ndef\nghi" — cursor at index 4 is row 1, col 0 (just
        # after the first newline, at the start of "def").
        msg = "abc\ndef\nghi"
        self.assertEqual(_cursor_to_row_col(msg, 0), (0, 0))
        self.assertEqual(_cursor_to_row_col(msg, 3), (0, 3))  # end of row 0
        self.assertEqual(_cursor_to_row_col(msg, 4), (1, 0))  # start of row 1
        self.assertEqual(_cursor_to_row_col(msg, 7), (1, 3))  # end of row 1
        self.assertEqual(_cursor_to_row_col(msg, 8), (2, 0))  # start of row 2
        self.assertEqual(_cursor_to_row_col(msg, 11), (2, 3))  # end of row 2

    def test_row_col_clamps_short_line(self) -> None:
        # Moving from "abcdef" (row 0, col 5) up/down to a shorter
        # line should clamp the column instead of overshooting.
        msg = "abcdef\nxy"
        # Asking for row 1, col 5 should clamp to row 1, col 2 (end of "xy").
        cur = _row_col_to_cursor(msg, 1, 5)
        self.assertEqual(_cursor_to_row_col(msg, cur), (1, 2))

    def test_row_col_clamps_out_of_range_row(self) -> None:
        msg = "abc\ndef"
        # Row way out of range collapses to the last line.
        cur = _row_col_to_cursor(msg, 99, 1)
        self.assertEqual(_cursor_to_row_col(msg, cur), (1, 1))


@unittest.skipUnless(UI_AVAILABLE, "curses unavailable")
class TestOpenGating(unittest.TestCase):
    """`open_commit_msg_editor` is silent-fail by design — Shift+Right
    on an ineligible row leaves the editor unset."""

    def _state_on_repo(self, repo):
        s = _state(repo)
        s.selected = 0
        s.focused_panel = "repos"
        return s

    def test_opens_on_dirty_repo(self) -> None:
        repo = _make_repo("a")
        repo.staged = [("M", "foo.py")]
        repo.branch = "main"
        s = self._state_on_repo(repo)
        self.assertTrue(open_commit_msg_editor(s))
        self.assertIsNotNone(s.commit_msg_editor)
        self.assertIs(s.commit_msg_editor.holder, repo)
        self.assertEqual(s.commit_msg_editor.label, repo.display_name)
        self.assertEqual(s.commit_msg_editor.branch, "main")

    def test_opens_on_repo_with_pending_message(self) -> None:
        # Clean working tree but a draft message — still worth opening
        # so the user can edit the draft in the large editor.
        repo = _make_repo("a")
        s = self._state_on_repo(repo)
        s.store.set_row_message(repo, "draft")
        self.assertTrue(open_commit_msg_editor(s))

    def test_refuses_on_clean_repo(self) -> None:
        repo = _make_repo("a")
        s = self._state_on_repo(repo)
        self.assertFalse(open_commit_msg_editor(s))
        self.assertIsNone(s.commit_msg_editor)

    def test_refuses_while_busy(self) -> None:
        repo = _make_repo("a")
        repo.staged = [("M", "foo.py")]
        s = self._state_on_repo(repo)
        s.store.set_repo_busy(repo, True)
        self.assertFalse(open_commit_msg_editor(s))

    def test_refuses_on_title_or_workspace_rows(self) -> None:
        repo = _make_repo("a")
        repo.staged = [("M", "x")]
        s = _state(repo)
        s.focused_panel = "repos"
        s.selected = -2  # title row
        self.assertFalse(open_commit_msg_editor(s))
        s.selected = -1  # workspace row
        self.assertFalse(open_commit_msg_editor(s))

    def test_cursor_lands_at_end_of_existing_message(self) -> None:
        repo = _make_repo("a")
        repo.staged = [("M", "x")]
        s = self._state_on_repo(repo)
        s.store.set_row_message(repo, "draft text")
        open_commit_msg_editor(s)
        self.assertEqual(s.commit_msg_editor.cursor, len("draft text"))


@unittest.skipUnless(UI_AVAILABLE, "curses unavailable")
class TestKeyDispatch(unittest.TestCase):
    """The editor binds to the store-owned row message. These tests build a Repo + editor by
    hand (without exercising the curses screen path), drive keys
    through `handle_commit_msg_editor_key`, and assert on the resulting
    store state."""

    def _editor_for(self, msg: str = "", cursor: int = 0) -> "tuple":
        repo = _make_repo("a")
        s = _state(repo)
        s.store.set_row_message(repo, msg)
        s.commit_msg_editor = CommitMsgEditor(
            holder=repo, parent=None, label="a", branch="main",
            cursor=cursor, scroll=0,
        )
        return s, repo

    def test_enter_closes(self) -> None:
        s, _ = self._editor_for("hello", 5)
        for key in (10, 13, curses.KEY_ENTER):
            s.commit_msg_editor = CommitMsgEditor(
                holder=s.repos[0], parent=None, label="a", branch="main",
                cursor=0)
            handle_commit_msg_editor_key(s, key)
            self.assertIsNone(s.commit_msg_editor,
                              f"key={key} should close")

    def test_esc_closes(self) -> None:
        s, _ = self._editor_for("hello", 5)
        handle_commit_msg_editor_key(s, 27)
        self.assertIsNone(s.commit_msg_editor)

    def test_tab_does_not_close(self) -> None:
        # The modal opens with Shift+Right, not Tab — per the project
        # convention "Tab opens / Tab closes," Tab on this modal must
        # be a no-op rather than a close.
        s, _ = self._editor_for("hello", 5)
        handle_commit_msg_editor_key(s, 9)
        self.assertIsNotNone(s.commit_msg_editor,
                             "Tab must NOT close this modal")

    def test_printable_insert_updates_holder(self) -> None:
        s, repo = self._editor_for("ello", 0)
        handle_commit_msg_editor_key(s, ord("H"))
        self.assertEqual(s.store.row_message(repo), "Hello")
        self.assertEqual(s.commit_msg_editor.cursor, 1)

    def test_backspace_at_end(self) -> None:
        s, repo = self._editor_for("hello", 5)
        handle_commit_msg_editor_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.store.row_message(repo), "hell")
        self.assertEqual(s.commit_msg_editor.cursor, 4)

    def test_backspace_at_zero_is_noop(self) -> None:
        s, repo = self._editor_for("hello", 0)
        handle_commit_msg_editor_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.store.row_message(repo), "hello")
        self.assertEqual(s.commit_msg_editor.cursor, 0)

    def test_delete_at_cursor(self) -> None:
        s, repo = self._editor_for("hello", 2)
        handle_commit_msg_editor_key(s, curses.KEY_DC)
        self.assertEqual(s.store.row_message(repo), "helo")
        self.assertEqual(s.commit_msg_editor.cursor, 2)

    def test_left_right_moves_cursor(self) -> None:
        s, _ = self._editor_for("abc", 1)
        handle_commit_msg_editor_key(s, curses.KEY_LEFT)
        self.assertEqual(s.commit_msg_editor.cursor, 0)
        handle_commit_msg_editor_key(s, curses.KEY_LEFT)
        self.assertEqual(s.commit_msg_editor.cursor, 0)  # clamped
        handle_commit_msg_editor_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.commit_msg_editor.cursor, 1)

    def test_home_end_jump_within_line(self) -> None:
        # Multi-line message — Home / End should stay on the current
        # row, not jump to start/end of the whole message.
        s, _ = self._editor_for("abc\ndef", 5)  # cursor on row 1, col 1
        handle_commit_msg_editor_key(s, curses.KEY_HOME)
        self.assertEqual(s.commit_msg_editor.cursor, 4)  # start of "def"
        handle_commit_msg_editor_key(s, curses.KEY_END)
        self.assertEqual(s.commit_msg_editor.cursor, 7)  # end of "def"

    def test_up_down_navigate_lines(self) -> None:
        s, _ = self._editor_for("abc\ndef", 6)  # row 1, col 2
        handle_commit_msg_editor_key(s, curses.KEY_UP)
        # Up to row 0, col 2 → "ab|c" → cursor index 2.
        self.assertEqual(_cursor_to_row_col(
            s.store.row_message(s.repos[0]), s.commit_msg_editor.cursor), (0, 2))
        handle_commit_msg_editor_key(s, curses.KEY_DOWN)
        self.assertEqual(_cursor_to_row_col(
            s.store.row_message(s.repos[0]), s.commit_msg_editor.cursor), (1, 2))

    def test_up_at_top_lands_at_start(self) -> None:
        s, _ = self._editor_for("abc", 2)
        handle_commit_msg_editor_key(s, curses.KEY_UP)
        self.assertEqual(s.commit_msg_editor.cursor, 0)

    def test_down_at_bottom_lands_at_end(self) -> None:
        s, _ = self._editor_for("abc", 1)
        handle_commit_msg_editor_key(s, curses.KEY_DOWN)
        self.assertEqual(s.commit_msg_editor.cursor, 3)


@unittest.skipUnless(UI_AVAILABLE, "curses unavailable")
class TestSubmoduleHolder(unittest.TestCase):
    """The editor accepts submodule ChildRef holders too, with the
    parent repo name surfacing in the modal header."""

    def test_opens_on_dirty_submodule(self) -> None:
        parent = _make_repo("parent")
        sub_canonical = _make_repo("sub")
        child = ChildRef(
            repo=sub_canonical,
            nested_path=Path("/tmp/parent/sub"),
            kind="submodule",
            dirty=True,
            branch="main",
        )
        parent.children = [child]
        s = _state(parent)
        s.focused_panel = "repos"
        # Body row index 1 = the child (parent is row 0).
        s.selected = 1
        self.assertTrue(open_commit_msg_editor(s))
        self.assertIs(s.commit_msg_editor.holder, child)
        self.assertIs(s.commit_msg_editor.parent, parent)
        # Header label includes both parent and submodule names.
        self.assertIn("parent", s.commit_msg_editor.label)
        self.assertIn("sub", s.commit_msg_editor.label)


if __name__ == "__main__":
    unittest.main()
