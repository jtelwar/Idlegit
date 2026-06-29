from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.state.edit_buffers import CommitMsgEditor  # noqa: E402
from features.commit_msg_editor.actions import handle_commit_msg_editor_key  # noqa: E402
from features.commit_msg_editor.projection import cursor_to_row_col  # noqa: E402
from features.commit_msg_editor.session import open_commit_msg_editor  # noqa: E402


class TestCommitMsgEditorFeature(unittest.TestCase):
    def test_open_uses_focused_dirty_repo(self) -> None:
        repo = _make_repo("a")
        repo.staged = [("M", "file.py")]
        repo.branch = "main"
        state = _state(repo)
        state.focused_panel = "repos"
        state.selected = 0

        self.assertTrue(open_commit_msg_editor(state))

        self.assertIs(state.commit_msg_editor.holder, repo)
        self.assertEqual(state.commit_msg_editor.branch, "main")

    def test_printable_key_updates_store_message(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        state.commit_msg_editor = CommitMsgEditor(
            holder=repo,
            parent=None,
            label="a",
            branch="main",
            cursor=0,
        )

        handle_commit_msg_editor_key(state, ord("H"))

        self.assertEqual(state.store.row_message(repo), "H")
        self.assertEqual(state.commit_msg_editor.cursor, 1)

    def test_vertical_navigation_uses_display_projection(self) -> None:
        self.assertEqual(cursor_to_row_col("abc\ndef", 6), (1, 2))

    def test_enter_closes_editor(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        state.commit_msg_editor = CommitMsgEditor(
            holder=repo,
            parent=None,
            label="a",
            branch="main",
            cursor=0,
        )

        handle_commit_msg_editor_key(state, curses.KEY_ENTER)

        self.assertIsNone(state.commit_msg_editor)
