"""Pure periodic-refresh decision tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.config import Config  # noqa: E402
from core.jobs import JobSpec, JobStatus  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.workspaces import Workspace  # noqa: E402
from idlegit import (  # noqa: E402
    _disable_flow_control, _periodic_refresh_idle, _periodic_refresh_interval,
    run,
)


class TestPeriodicRefreshDecision(unittest.TestCase):
    def test_interval_zero_when_disabled(self) -> None:
        state = State(repos=[], workspace_name="", periodic_refresh_seconds=0)
        self.assertEqual(_periodic_refresh_interval(state), 0)

    def test_interval_uses_enabled_seconds(self) -> None:
        state = State(repos=[], workspace_name="", periodic_refresh_seconds=1)
        self.assertEqual(_periodic_refresh_interval(state), 1)

    def test_idle_requires_no_mutation_jobs_or_busy_rows(self) -> None:
        repo = _make_repo("repo")
        state = State(repos=[repo], workspace_name="A")
        self.assertTrue(_periodic_refresh_idle(state))

        claim_id = state.leases.acquire(repo=repo)
        self.assertFalse(_periodic_refresh_idle(state))
        state.leases.release(claim_id)
        self.assertTrue(_periodic_refresh_idle(state))

        state.store.set_repo_busy(repo, True)
        self.assertFalse(_periodic_refresh_idle(state))

    def test_idle_respects_registry_local_mutation_jobs(self) -> None:
        state = State(repos=[], workspace_name="A")
        job = state.job_registry.start(
            JobSpec(kind="commit", label="commit", local_mutation=True))

        self.assertFalse(_periodic_refresh_idle(state))

        state.job_registry.finish(job, JobStatus.OK)
        self.assertTrue(_periodic_refresh_idle(state))

    def test_idle_false_during_review_and_safe_merge(self) -> None:
        state = State(repos=[], workspace_name="A")
        state.in_review = True
        self.assertFalse(_periodic_refresh_idle(state))
        state.in_review = False
        state.in_safe_merge = True
        self.assertFalse(_periodic_refresh_idle(state))


class TestFlowControl(unittest.TestCase):
    def test_disable_flow_control_clears_xon_xoff_on_stdin_fd(self) -> None:
        attrs = [0xFFFF, 0, 0, 0, 0, 0, []]
        fake_termios = mock.Mock()
        fake_termios.IXON = 0x0400
        fake_termios.IXOFF = 0x1000
        fake_termios.TCSANOW = 0
        fake_termios.error = OSError
        fake_termios.tcgetattr.return_value = attrs
        stdin = mock.Mock()
        stdin.fileno.return_value = 7

        with mock.patch.dict(sys.modules, {"termios": fake_termios}), \
             mock.patch("idlegit.sys.stdin", stdin), \
             mock.patch("idlegit.os.isatty", return_value=True), \
             mock.patch("idlegit.os.open", side_effect=OSError):
            _disable_flow_control()

        self.assertIn(mock.call(7), fake_termios.tcgetattr.call_args_list)
        self.assertGreaterEqual(fake_termios.tcsetattr.call_count, 1)
        self.assertEqual(attrs[0] & fake_termios.IXON, 0)
        self.assertEqual(attrs[0] & fake_termios.IXOFF, 0)

    def test_disable_flow_control_falls_back_to_fd_zero(self) -> None:
        attrs = [0xFFFF, 0, 0, 0, 0, 0, []]
        fake_termios = mock.Mock()
        fake_termios.IXON = 0x0400
        fake_termios.IXOFF = 0x1000
        fake_termios.TCSANOW = 0
        fake_termios.error = OSError
        fake_termios.tcgetattr.return_value = attrs
        stdin = mock.Mock()
        stdin.fileno.side_effect = OSError

        with mock.patch.dict(sys.modules, {"termios": fake_termios}), \
             mock.patch("idlegit.sys.stdin", stdin), \
             mock.patch("idlegit.os.isatty", return_value=True):
            _disable_flow_control()

        fake_termios.tcgetattr.assert_called_once_with(0)


class _FakeScreen:
    def keypad(self, _enabled: bool) -> None:
        pass

    def timeout(self, _ms: int) -> None:
        pass


class TestStartupActiveWorkspaceRefresh(unittest.TestCase):
    def test_run_shows_all_workspaces_but_refreshes_only_active(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        repo_b = _make_repo("b")

        def discover(folders):
            self.assertEqual(folders, ws_b.folders)
            return [repo_b]

        with mock.patch("idlegit.curses.set_escdelay"), \
             mock.patch("idlegit.curses.curs_set"), \
             mock.patch("idlegit.init_colors"), \
             mock.patch("idlegit._disable_flow_control"), \
             mock.patch("ui.mouse.enable_mouse"), \
             mock.patch("idlegit._set_terminal_title"), \
             mock.patch("idlegit._discover_workspace_repos",
                        side_effect=discover) as discover_repos, \
             mock.patch("idlegit.refresh_all_workspaces",
                        return_value=True) as refresh, \
             mock.patch("core.fs_watcher.reconcile_repo_watchers"), \
             mock.patch("core.fs_watcher.stop_repo_watchers"), \
             mock.patch("idlegit._run_main_loop"):
            run(_FakeScreen(), Config(), [ws_a, ws_b], initial_active_idx=1)

        discover_repos.assert_called_once_with(ws_b.folders)
        refresh_arg = refresh.call_args.args[1]
        self.assertEqual(len(refresh_arg), 2)
        self.assertEqual(refresh_arg[0][0], "A")
        self.assertEqual(refresh_arg[0][1], [])
        self.assertEqual(refresh_arg[1][0], "B")
        self.assertEqual(refresh_arg[1][1], [repo_b])
        self.assertEqual(refresh.call_args.kwargs["active_index"], 1)
        self.assertEqual(ws_a.cached_repos, [])
        self.assertEqual(ws_b.cached_repos, [repo_b])


if __name__ == "__main__":
    unittest.main()
