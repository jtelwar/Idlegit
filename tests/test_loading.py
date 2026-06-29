from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from ui import loading  # noqa: E402


class FakeScreen:
    def timeout(self, _value: int) -> None:
        return None

    def getch(self) -> int:
        return -1

    def erase(self) -> None:
        return None

    def refresh(self) -> None:
        return None

    def getmaxyx(self):
        return (24, 80)


class TestStartupLoading(unittest.TestCase):
    def test_refreshes_only_active_workspace(self) -> None:
        active = _make_repo("active")
        inactive = _make_repo("inactive")
        active_started = threading.Event()
        release_active = threading.Event()
        refreshed = []

        def refresh(repo):
            refreshed.append(repo.rel)
            if repo is active:
                active_started.set()
                self.assertTrue(release_active.wait(timeout=2.0))

        with mock.patch("ui.loading.curses.curs_set"), \
             mock.patch("ui.loading.curses.napms"), \
             mock.patch("ui.loading.curses.color_pair", return_value=0), \
             mock.patch("ui.loading.safe_addstr"), \
             mock.patch("core.workers.refresh_repo", side_effect=refresh), \
             mock.patch("core.workers.link_siblings") as link:
            thread = threading.Thread(
                target=lambda: loading.refresh_all_workspaces(
                    FakeScreen(),
                    [
                        ("active", [active], []),
                        ("inactive", [inactive], []),
                    ],
                    name_max=20,
                    active_index=0,
                )
            )
            thread.start()
            self.assertTrue(active_started.wait(timeout=2.0))
            release_active.set()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(refreshed, ["active"])
        link.assert_called_once_with([active], [])

    def test_thread_start_failure_does_not_hang_loading(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        active = _make_repo("active")

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread), \
             mock.patch("ui.loading.curses.curs_set") as curs_set, \
             mock.patch("core.workers.refresh_repo") as refresh, \
             mock.patch("core.workers.link_siblings") as link:
            ok = loading.refresh_all_workspaces(
                FakeScreen(),
                [("active", [active], [])],
                name_max=20,
                active_index=0,
            )

        self.assertTrue(ok)
        curs_set.assert_not_called()
        refresh.assert_not_called()
        link.assert_not_called()

    def test_final_relink_failure_does_not_abort_loading(self) -> None:
        active = _make_repo("active")

        with mock.patch("ui.loading.curses.curs_set"), \
             mock.patch("ui.loading.curses.napms"), \
             mock.patch("ui.loading.curses.color_pair", return_value=0), \
             mock.patch("ui.loading.safe_addstr"), \
             mock.patch("core.workers.refresh_repo"), \
             mock.patch("core.workers.link_siblings",
                        side_effect=RuntimeError("link boom")):
            ok = loading.refresh_all_workspaces(
                FakeScreen(),
                [("active", [active], [])],
                name_max=20,
                active_index=0,
            )

        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
