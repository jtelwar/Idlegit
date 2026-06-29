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

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.views import DiffViewer  # noqa: E402
from features.diff_viewer.actions import (  # noqa: E402
    handle_diff_viewer_key,
    next_tab,
)
from features.diff_viewer.projection import (  # noqa: E402
    any_tab_loading,
    diff_viewer_hint_specs,
    set_tab_scroll,
    tab_lines,
    tab_load_id,
    tab_loading,
    tab_scroll,
)
from features.diff_viewer.session import (  # noqa: E402
    close_diff_viewer,
    open_diff_viewer,
)


class TestDiffViewerFeature(unittest.TestCase):
    def _state(self):
        return _state(_make_repo("repo"))

    def _viewer(self) -> DiffViewer:
        return DiffViewer(
            file_path="src/app.py",
            target_path=Path("/tmp/repo"),
            label="repo",
            diff_load_id="diff-load",
            log_load_id="log-load",
            blame_load_id="blame-load",
        )

    def test_open_session_installs_viewer_and_starts_loaders(self) -> None:
        state = self._state()

        with mock.patch(
                "features.diff_viewer.session.kick_off_diff_viewer_loads"
        ) as load:
            open_diff_viewer(
                state,
                target_path=Path("/tmp/repo"),
                label="repo",
                file_path="src/app.py",
                untracked=False,
                commit_sha="abc123",
            )

        self.assertIsNotNone(state.diff_viewer)
        self.assertEqual(state.diff_viewer.file_path, "src/app.py")
        self.assertEqual(state.diff_viewer.commit_sha, "abc123")
        load.assert_called_once_with(state, state.diff_viewer)

    def test_close_session_removes_view_load_records(self) -> None:
        state = self._state()
        viewer = self._viewer()
        state.diff_viewer = viewer
        state.view_loads.create(viewer.diff_load_id)
        state.view_loads.create(viewer.log_load_id)
        state.view_loads.create(viewer.blame_load_id)

        close_diff_viewer(state)

        self.assertIsNone(state.diff_viewer)
        self.assertEqual(state.view_loads.snapshot(viewer.diff_load_id),
                         ([], True, ""))

    def test_projection_reads_tab_loads_and_scrolls(self) -> None:
        state = self._state()
        viewer = self._viewer()
        state.view_loads.create(viewer.diff_load_id)
        state.view_loads.finish(viewer.diff_load_id, ["line"])

        self.assertEqual(tab_load_id(viewer, "diff"), "diff-load")
        self.assertEqual(tab_lines(state, viewer, "diff"), ["line"])
        self.assertFalse(tab_loading(state, viewer, "diff"))

        set_tab_scroll(viewer, "log", 4)
        self.assertEqual(tab_scroll(viewer, "log"), 4)
        self.assertIn(("Tab", "close"), diff_viewer_hint_specs())

    def test_any_tab_loading_reports_pending_loads(self) -> None:
        state = self._state()
        viewer = self._viewer()
        state.view_loads.create(viewer.diff_load_id)

        self.assertTrue(any_tab_loading(state, viewer))

    def test_next_tab_wraps(self) -> None:
        self.assertEqual(next_tab("diff", 1), "log")
        self.assertEqual(next_tab("diff", -1), "blame")
        self.assertEqual(next_tab("unknown", 1), "log")

    def test_key_handler_switches_tabs_and_scrolls(self) -> None:
        state = self._state()
        state.diff_viewer = self._viewer()

        handle_diff_viewer_key(state, curses.KEY_RIGHT)
        self.assertEqual(state.diff_viewer.active_tab, "log")

        handle_diff_viewer_key(state, curses.KEY_DOWN)
        self.assertEqual(state.diff_viewer.log_scroll, 1)

    def test_key_handler_end_uses_active_tab_line_count(self) -> None:
        state = self._state()
        viewer = self._viewer()
        viewer.active_tab = "log"
        state.diff_viewer = viewer
        state.view_loads.create(viewer.log_load_id)
        state.view_loads.finish(viewer.log_load_id, ["a", "b", "c"])

        handle_diff_viewer_key(state, curses.KEY_END)

        self.assertEqual(viewer.log_scroll, 3)

    def test_close_key_closes_viewer(self) -> None:
        state = self._state()
        viewer = self._viewer()
        state.diff_viewer = viewer
        state.view_loads.create(viewer.diff_load_id)

        handle_diff_viewer_key(state, 27)

        self.assertIsNone(state.diff_viewer)


if __name__ == "__main__":
    unittest.main()
