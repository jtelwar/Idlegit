from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_state as _state  # noqa: E402
from core.state.action_menu import FileEntry  # noqa: E402
from core.state.views import CommitViewModal  # noqa: E402
from features.commit_view.actions import handle_commit_view_modal_key  # noqa: E402
from features.commit_view.projection import commit_view_load_ids, wrap_text  # noqa: E402
from features.commit_view.session import open_commit_view_modal  # noqa: E402


class TestCommitViewFeature(unittest.TestCase):
    def test_open_installs_modal_and_dispatches_loader(self) -> None:
        state = _state()
        target_path = Path("/tmp/repo")

        with mock.patch("features.commit_view.session.kick_off_load_commit_view") as kick:
            open_commit_view_modal(state, target_path, "repo", "abc123", "subject")

        modal = state.commit_view_modal
        self.assertIsNotNone(modal)
        self.assertEqual(modal.target_path, target_path)
        self.assertEqual(modal.subject, "subject")
        kick.assert_called_once_with(state, modal)

    def test_tab_on_focused_change_returns_open_diff_effect(self) -> None:
        state = _state()
        state.commit_view_modal = CommitViewModal(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            sha="abcdef123",
            section="tabs",
            active_tab="changes",
            files=[FileEntry(path="a.py", x="M")],
        )

        effect = handle_commit_view_modal_key(state, 9)

        self.assertEqual(effect.kind, "open_diff")
        self.assertEqual(effect.file_path, "a.py")
        self.assertEqual(effect.commit_sha, "abcdef123")

    def test_add_tag_confirm_dispatches_worker_and_updates_tags(self) -> None:
        state = _state()
        modal = CommitViewModal(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            sha="abcdef123",
            section="actions",
        )
        state.commit_view_modal = modal
        handle_commit_view_modal_key(state, curses.KEY_ENTER)
        for char in "v1.0":
            handle_commit_view_modal_key(state, ord(char))
        handle_commit_view_modal_key(state, curses.KEY_ENTER)

        with mock.patch("features.commit_view.actions.kick_off_add_tag") as kick:
            handle_commit_view_modal_key(state, ord("y"))

        kick.assert_called_once()
        self.assertEqual(modal.tags, ["v1.0"])
        self.assertEqual(modal.confirm_message, "")

    def test_close_removes_view_load_records(self) -> None:
        state = _state()
        modal = CommitViewModal(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            sha="abc123",
            tags_load_id="tags",
            details_load_id="details",
            files_load_id="files",
            reflog_load_id="reflog",
        )
        state.commit_view_modal = modal
        for load_id in commit_view_load_ids(modal):
            state.view_loads.create(load_id)

        handle_commit_view_modal_key(state, 27)

        self.assertIsNone(state.commit_view_modal)
        for load_id in commit_view_load_ids(modal):
            self.assertTrue(state.view_loads.is_cancelled(load_id))

    def test_projection_wraps_text(self) -> None:
        self.assertEqual(wrap_text("hello world", 5), ["hello", "world"])
