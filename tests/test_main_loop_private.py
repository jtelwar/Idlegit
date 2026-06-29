from __future__ import annotations

import sys
import termios
import types
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.jobs import JobSpec, JobStatus, JobTaskOutcome  # noqa: E402
from core import workers  # noqa: E402
import idlegit  # noqa: E402
import ui.main_loop as main_loop  # noqa: E402


class TestModalDriver(unittest.TestCase):
    def test_detached_preflight_waits_on_job_and_routes_prompt_keys(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.timeout_values = []

            def timeout(self, value: int) -> None:
                self.timeout_values.append(value)

            def refresh(self) -> None:
                return None

        state = _state(_make_repo("a"))
        job = state.job_registry.start(JobSpec(kind="review-preflight", label="preflight"))
        state.detached_recovery_prompt = object()
        screen = FakeScreen()

        def close_prompt(target_state: State, key: int) -> None:
            self.assertEqual(key, 10)
            target_state.detached_recovery_prompt = None
            target_state.job_registry.finish(job, JobStatus.OK, "ready")

        with (
            mock.patch(
                "ui.main_loop.kick_off_detached_review_preflight",
                return_value=job,
            ),
            mock.patch("ui.main_loop.draw_main"),
            mock.patch("ui.main_loop.read_key", return_value=10),
            mock.patch(
                "ui.main_loop.handle_detached_recovery_prompt_key",
                side_effect=close_prompt,
            ),
        ):
            self.assertTrue(
                main_loop._detached_review_preflight(screen, state)
            )

        self.assertEqual(screen.timeout_values, [100])

    def test_detached_preflight_returns_false_when_job_fails(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        job = state.job_registry.start(JobSpec(kind="review-preflight", label="preflight"))
        state.job_registry.finish(job, JobStatus.FAIL, "cancelled")

        with (
            mock.patch.object(
                main_loop,
                "kick_off_detached_review_preflight",
                return_value=job,
            ),
        ):
            self.assertFalse(main_loop._detached_review_preflight(None, state))


class TestDetachedReviewPreflightJob(unittest.TestCase):
    class InlineThread:
        def __init__(self, target, name) -> None:
            self.target = target
            self.name = name
            self.daemon = False

        def start(self) -> None:
            self.target()

    def test_target_discovery_uses_state_snapshot_without_git_probe(self) -> None:
        repo = _make_repo("a", branch="(detached)")
        clean = _make_repo("b", branch="main")
        state = _state(repo, clean)
        state.store.set_row_message(repo, "commit me")
        state.store.set_row_message(clean, "ignore me")

        self.assertEqual(
            workers.review_detached_targets(state),
            [(repo.path, repo.display_name)],
        )

    def test_target_discovery_ignores_raw_message_without_store_snapshot(self) -> None:
        repo = _make_repo("a", branch="(detached)")
        state = _state(repo)
        repo.message = "raw only"

        self.assertEqual(workers.review_detached_targets(state), [])

    def test_detached_target_refresh_uses_store_workspace_rows(self) -> None:
        repo = _make_repo("a", branch="(detached)")
        state = _state(repo)
        state.repos = []

        with mock.patch.object(
                workers, "_refresh_repo_snapshot_into_state") as refresh:
            outcome = workers._refresh_detached_review_target(
                state, repo.path, repo.display_name)

        refresh.assert_called_once_with(state, repo)
        self.assertIsNone(outcome.status)

    def test_preflight_worker_warns_when_refresh_warns(self) -> None:
        repo = _make_repo("a", branch="(detached)")
        state = _state(repo)
        state.store.set_row_message(repo, "commit me")

        with (
            mock.patch.object(workers.threading, "Thread", self.InlineThread),
            mock.patch.object(workers, "_attempt_detached_recovery",
                              return_value=(True, "")),
            mock.patch.object(
                workers,
                "_refresh_detached_review_target",
                return_value=JobTaskOutcome(JobStatus.WARN, "refresh boom"),
            ),
        ):
            job = workers.kick_off_detached_review_preflight(state)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, JobStatus.WARN)
        self.assertEqual(job.message, "refresh boom")
        header = state.tasks.snapshot()[0]
        self.assertEqual(header.status, "warn")
        self.assertEqual(header.message, "refresh boom")

    def test_preflight_worker_fails_when_recovery_is_refused(self) -> None:
        repo = _make_repo("a", branch="(detached)")
        state = _state(repo)
        state.store.set_row_message(repo, "commit me")

        with (
            mock.patch.object(workers.threading, "Thread", self.InlineThread),
            mock.patch.object(workers, "_attempt_detached_recovery",
                              return_value=(False, "user cancelled recovery")),
        ):
            job = workers.kick_off_detached_review_preflight(state)

        self.assertIsNotNone(job)
        self.assertEqual(job.status, JobStatus.FAIL)
        self.assertEqual(job.message, "user cancelled recovery")
        header = state.tasks.snapshot()[0]
        self.assertEqual(header.status, "fail")
        self.assertEqual(header.message, "user cancelled recovery")


class TestMainLoopMutationDrain(unittest.TestCase):
    def test_row_activity_uses_store_workspace_repo_rows(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        state.repos = []
        state.store.set_repo_suggesting(repo, True)

        self.assertTrue(idlegit._row_activity_active(state))

    def test_row_activity_uses_store_workspace_child_rows(self) -> None:
        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        child = ChildRef(
            repo=canonical,
            nested_path=parent.path / "vendor" / "canonical",
            kind="submodule",
        )
        parent.children = [child]
        state = _state(parent, canonical)
        parent.children = []
        state.store.set_child_suggesting(child, True)

        self.assertTrue(idlegit._row_activity_active(state))

    def test_pending_ui_event_forces_one_snappy_timeout(self) -> None:
        state = _state(_make_repo("a"))
        state.ui_events.notify()

        self.assertEqual(
            idlegit._main_loop_timeout_ms(
                state,
                anim_running=False,
                mutation_jobs_just_drained=False,
                periodic_refresh_fired=False,
            ),
            100,
        )
        self.assertEqual(
            idlegit._main_loop_timeout_ms(
                state,
                anim_running=False,
                mutation_jobs_just_drained=False,
                periodic_refresh_fired=False,
            ),
            1000,
        )

    def test_job_registry_mutation_completion_drains_and_keeps_polling(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.timeout_values = []

            def timeout(self, value: int) -> None:
                self.timeout_values.append(value)

        state = _state(_make_repo("a"))
        job = state.job_registry.start(
            JobSpec(
                kind="smart-sync",
                label="smart-sync",
                local_mutation=True,
            )
        )
        screen = FakeScreen()
        draw_count = 0

        def draw_side_effect(_screen, _state) -> None:
            nonlocal draw_count
            draw_count += 1
            if draw_count == 2:
                state.job_registry.finish(job, JobStatus.OK)

        fake_fs_watcher = types.ModuleType("core.fs_watcher")
        fake_fs_watcher.drain_pending_refreshes = mock.Mock()

        with (
            mock.patch.dict(
                sys.modules,
                {"core.fs_watcher": fake_fs_watcher},
            ),
            mock.patch("idlegit.draw_main", side_effect=draw_side_effect),
            mock.patch("ui.mouse.read_key", side_effect=[-1, 27]),
        ):
            idlegit._run_main_loop(screen, state, "Idlegit")

        fake_fs_watcher.drain_pending_refreshes.assert_called_once()
        self.assertEqual(screen.timeout_values, [100, 100])


class TestTerminalFlowControl(unittest.TestCase):
    def test_disable_flow_control_clears_real_tty_candidates(self) -> None:
        attrs_by_fd = {
            0: [termios.IXON | termios.IXOFF],
            9: [termios.IXON | termios.IXOFF],
        }
        written = {}

        def tcgetattr(fd: int):
            return list(attrs_by_fd[fd])

        def tcsetattr(fd: int, _when: int, attrs) -> None:
            written[fd] = attrs[0]

        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0

        with (
            mock.patch.object(idlegit.sys, "stdin", fake_stdin),
            mock.patch.object(idlegit.os, "isatty", return_value=True),
            mock.patch.object(idlegit.os, "open", return_value=9) as open_tty,
            mock.patch.object(idlegit.os, "close") as close_fd,
            mock.patch.object(termios, "tcgetattr", side_effect=tcgetattr),
            mock.patch.object(termios, "tcsetattr", side_effect=tcsetattr),
        ):
            idlegit._disable_flow_control()

        open_tty.assert_called_once()
        close_fd.assert_called_once_with(9)
        self.assertEqual(written[0], 0)
        self.assertEqual(written[9], 0)


if __name__ == "__main__":
    unittest.main()
