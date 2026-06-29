from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.selectors import child_row_state, repo_row_state  # noqa: E402
from core.state.store import (  # noqa: E402
    ChildStatusSnapshot,
    ChildTopologySnapshot,
)

try:
    from ui.main_screen import (  # noqa: E402
        _column_widths,
        _child_refresh_spinner_visible,
        _repo_refresh_spinner_visible,
        draw_child_row,
        draw_repo_row,
    )
    import ui.main_screen as main_screen  # noqa: E402
    UI_AVAILABLE = True
except Exception:  # pragma: no cover
    UI_AVAILABLE = False


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestRefreshSpinnerVisibility(unittest.TestCase):
    def test_clean_refreshing_repo_shows_spinner(self) -> None:
        repo = _make_repo("r")
        state = make_state(repo)
        state.store.set_repo_busy(repo, True)
        self.assertTrue(_repo_refresh_spinner_visible(state, repo))

    def test_dirty_refreshing_repo_shows_spinner(self) -> None:
        repo = _make_repo("r")
        repo.unstaged = [("M", "README.md")]
        state = make_state(repo)
        state.store.set_repo_busy(repo, True)
        self.assertTrue(_repo_refresh_spinner_visible(state, repo))

    def test_merging_refreshing_repo_keeps_status_visible(self) -> None:
        repo = _make_repo("r")
        repo.merging = True
        state = make_state(repo)
        state.store.set_repo_busy(repo, True)
        self.assertFalse(_repo_refresh_spinner_visible(state, repo))

    def test_dirty_refreshing_child_shows_spinner(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(repo=canonical, nested_path=parent.path / "child")
        child.dirty = True
        parent.children = [child]
        state = make_state(parent, canonical)
        state.store.set_child_busy(child, True)
        self.assertTrue(_child_refresh_spinner_visible(state, child))

    def test_repo_row_draws_store_branch_snapshot(self) -> None:
        repo = _make_repo("r")
        repo.branch = "main"
        state = make_state(repo)
        repo.branch = "raw-changed"
        drawn: list[str] = []

        with (
            mock.patch.object(main_screen, "safe_addstr",
                              side_effect=lambda *_args: drawn.append(_args[3])),
            mock.patch.object(main_screen.curses, "color_pair",
                              side_effect=lambda pair: pair),
        ):
            draw_repo_row(
                None, state, 0, repo, False,
                name_w=12, branch_w=14, field_x=30, field_w=20,
                name_max=12, branch_max=10,
                name_mode="tail", branch_mode="tail",
            )

        self.assertTrue(any("[main]" in text for text in drawn))
        self.assertFalse(any("raw-changed" in text for text in drawn))

    def test_child_row_draws_store_kind_and_branch_snapshot(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
            branch="main",
        )
        parent.children = [child]
        state = make_state(parent, canonical)
        child.kind = "subtree"
        child.branch = "raw-changed"
        drawn: list[str] = []

        with (
            mock.patch.object(main_screen, "safe_addstr",
                              side_effect=lambda *_args: drawn.append(_args[3])),
            mock.patch.object(main_screen.curses, "color_pair",
                              side_effect=lambda pair: pair),
        ):
            draw_child_row(
                None, state, 0, child, False,
                name_w=12, branch_w=14, field_x=30, field_w=20,
                name_max=12, branch_max=10,
                name_mode="tail", branch_mode="tail",
            )

        self.assertIn("↳", drawn)
        self.assertTrue(any("[main]" in text for text in drawn))
        self.assertFalse(any("raw-changed" in text for text in drawn))

    def test_child_row_draw_handles_stale_child_after_topology_replacement(
            self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
            branch="main",
        )
        parent.children = [child]
        state = make_state(parent, canonical)
        row = state.selectable_rows()[1]
        stale_parent = row[1]
        stale_child = row[2]
        replacement = ChildRef(
            repo=canonical,
            nested_path=child.nested_path,
            kind="submodule",
            branch="replacement",
        )

        state.store.replace_workspace_topology(
            name="ws",
            folders=[],
            repos=[parent, canonical],
            children=[
                ChildTopologySnapshot(
                    parent_repo=parent,
                    child=replacement,
                    status=ChildStatusSnapshot(
                        kind="submodule",
                        branch="replacement",
                    ),
                ),
            ],
        )
        drawn: list[str] = []

        self.assertIsNone(state.store.child_id_for(stale_child))
        with self.assertRaises(RuntimeError):
            child_row_state(state, stale_child)

        with (
            mock.patch.object(main_screen, "safe_addstr",
                              side_effect=lambda *_args: drawn.append(_args[3])),
            mock.patch.object(main_screen.curses, "color_pair",
                              side_effect=lambda pair: pair),
        ):
            draw_child_row(
                None, state, 0, stale_child, False,
                name_w=12, branch_w=18, field_x=34, field_w=20,
                name_max=12, branch_max=20,
                name_mode="tail", branch_mode="tail",
                parent=stale_parent,
            )

        self.assertTrue(any("[replacement]" in text for text in drawn))


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestMainScreenColumnWidths(unittest.TestCase):
    def test_column_widths_use_store_workspace_rows(self) -> None:
        repo = _make_repo("long-repository-name")
        repo.branch = "feature/something"
        state = make_state(repo)
        state.repos = []

        name_w, branch_w = _column_widths(
            state,
            name_max=40,
            child_name_max=40,
            branch_max=40,
            name_mode="tail",
            branch_mode="tail",
        )

        self.assertGreaterEqual(name_w, len(repo.display_name) + 2)
        self.assertGreaterEqual(branch_w, len("[feature/something]") + 2)

    def test_column_widths_use_store_child_records(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("very-long-child-repository")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "child",
            kind="submodule",
            branch="detached",
        )
        parent.children = [child]
        state = make_state(parent, canonical)
        parent.children = []

        name_w, branch_w = _column_widths(
            state,
            name_max=40,
            child_name_max=40,
            branch_max=40,
            name_mode="tail",
            branch_mode="tail",
        )

        self.assertGreaterEqual(name_w, 4 + len(canonical.display_name) + 2)
        self.assertGreaterEqual(branch_w, len("[detached]") + 2)


class TestRowDisplaySelectors(unittest.TestCase):
    def test_dirty_repo_is_editable_until_busy(self) -> None:
        repo = _make_repo("r")
        repo.unstaged = [("M", "README.md")]
        app_state = make_state(repo)

        state = repo_row_state(app_state, repo)
        self.assertTrue(state.dirty)
        self.assertTrue(state.editable)
        self.assertTrue(state.show_message_field)

        app_state.store.set_repo_busy(repo, True)
        busy = repo_row_state(app_state, repo)
        self.assertTrue(busy.busy)
        self.assertFalse(busy.editable)
        self.assertFalse(busy.show_message_field)

    def test_repo_selector_reads_store_snapshot_not_raw_repo(self) -> None:
        repo = _make_repo("r")
        repo.unstaged = [("M", "README.md")]
        app_state = make_state(repo)

        repo.unstaged = []
        repo.message = "raw draft"

        stale_raw = repo_row_state(app_state, repo)
        self.assertTrue(stale_raw.dirty)
        self.assertEqual(stale_raw.message, "")

        app_state.store.publish_row_status(repo)
        published = repo_row_state(app_state, repo)
        self.assertFalse(published.dirty)
        self.assertEqual(published.message, "")

        app_state.store.set_row_message(repo, "store draft")
        with_store_message = repo_row_state(app_state, repo)
        self.assertEqual(with_store_message.message, "store draft")

    def test_merging_busy_repo_keeps_status_visible(self) -> None:
        repo = _make_repo("r")
        repo.merging = True
        app_state = make_state(repo)
        app_state.store.set_repo_busy(repo, True)

        state = repo_row_state(app_state, repo)
        self.assertTrue(state.busy)
        self.assertFalse(state.show_spinner)

    def test_subtree_child_is_not_message_editable(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="subtree",
            dirty=True,
        )
        parent.children = [child]
        app_state = make_state(parent, canonical)

        state = child_row_state(app_state, child)
        self.assertTrue(state.dirty)
        self.assertFalse(state.editable)
        self.assertFalse(state.show_message_field)

    def test_store_id_lease_makes_repo_not_editable(self) -> None:
        repo = _make_repo("r")
        repo.unstaged = [("M", "README.md")]
        app_state = make_state(repo)
        repo_id = app_state.store.repo_id_for(repo)

        app_state.leases.acquire(repo_id=repo_id, owner_label="commit")

        state = repo_row_state(app_state, repo)
        self.assertTrue(state.busy)
        self.assertFalse(state.editable)
        self.assertFalse(state.show_message_field)

    def test_store_busy_repo_is_not_editable_without_refresh_flag(self) -> None:
        repo = _make_repo("r")
        repo.unstaged = [("M", "README.md")]
        app_state = make_state(repo)

        app_state.store.set_repo_busy(repo, True)

        state = repo_row_state(app_state, repo)
        self.assertTrue(state.busy)
        self.assertFalse(state.editable)

    def test_store_busy_child_is_not_editable_without_refresh_flag(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("child")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "child",
            kind="submodule",
            dirty=True,
        )
        parent.children = [child]
        app_state = make_state(parent, canonical)

        app_state.store.set_child_busy(child, True)

        state = child_row_state(app_state, child)
        self.assertTrue(state.busy)
        self.assertFalse(state.editable)


if __name__ == "__main__":
    unittest.main()
