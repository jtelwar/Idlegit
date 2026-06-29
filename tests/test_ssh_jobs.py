from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_state as _state  # noqa: E402
from core.jobs import JobStatus  # noqa: E402
from core.state.ssh_keygen import SshKeygenModal  # noqa: E402
from core.ssh import SshToolsStatus  # noqa: E402
from core.workers import (  # noqa: E402
    kick_off_ssh_add_keys,
    kick_off_ssh_keygen_prepare,
)
from features.ssh_keygen.actions import kick_off_generate  # noqa: E402
from features.ssh_keygen.session import open_ssh_keygen_modal  # noqa: E402


def _wait_jobs(state) -> None:
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        jobs = state.job_registry.snapshot()
        if jobs and all(job.terminal for job in jobs):
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestSshKeygenJob(unittest.TestCase):
    def test_open_installs_loading_modal_without_sync_prefill(self) -> None:
        state = _state()

        with (
            mock.patch("features.ssh_keygen.session.kick_off_ssh_keygen_prepare") as prepare,
            mock.patch("core.ssh.git_user_email") as email,
            mock.patch("core.ssh.default_ed25519_path") as default_path,
        ):
            open_ssh_keygen_modal(state)

        self.assertIsNotNone(state.ssh_keygen_modal)
        modal = state.ssh_keygen_modal
        assert modal is not None
        self.assertTrue(modal.preparing)
        self.assertEqual(modal.key_path_placeholder, "checking...")
        prepare.assert_called_once_with(state, modal)
        email.assert_not_called()
        default_path.assert_not_called()

    def test_prepare_prefills_modal_from_read_only_job(self) -> None:
        state = _state()
        modal = SshKeygenModal(preparing=True)
        tools = SshToolsStatus(
            has_ssh_agent=True,
            has_ssh_add=True,
            has_ssh_keygen=True,
            agent_running=True,
            keys_loaded=0,
        )

        with (
            mock.patch("core.ssh.ssh_tools_status", return_value=tools),
            mock.patch("core.ssh.git_user_email",
                       return_value="me@example.test"),
            mock.patch("core.ssh.default_ed25519_path",
                       return_value=Path("/tmp/id_ed25519")),
        ):
            kick_off_ssh_keygen_prepare(state, modal)
            _wait_jobs(state)

        self.assertFalse(modal.preparing)
        self.assertEqual(modal.email, "me@example.test")
        self.assertEqual(modal.key_path_text, "/tmp/id_ed25519")
        self.assertEqual(modal.key_path_placeholder, "/tmp/id_ed25519")
        jobs = state.job_registry.snapshot()
        self.assertEqual(jobs[0].spec.kind, "ssh-keygen-prepare")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)

    def test_prepare_missing_ssh_keygen_marks_modal_and_job_failed(self) -> None:
        state = _state()
        modal = SshKeygenModal(preparing=True)
        tools = SshToolsStatus(
            has_ssh_agent=True,
            has_ssh_add=True,
            has_ssh_keygen=False,
            agent_running=True,
            keys_loaded=0,
        )

        with mock.patch("core.ssh.ssh_tools_status", return_value=tools):
            kick_off_ssh_keygen_prepare(state, modal)
            _wait_jobs(state)

        self.assertFalse(modal.preparing)
        self.assertEqual(modal.key_path_placeholder, "ssh-keygen not found")
        self.assertIn("ssh-keygen not on PATH", modal.error)
        self.assertEqual(state.job_registry.snapshot()[0].status, JobStatus.FAIL)

    def test_generate_runs_as_local_mutation_job(self) -> None:
        state = _state()
        modal = SshKeygenModal(
            email="me@example.test",
            key_path_text="/tmp/idlegit-test-key",
            passphrase="secret",
        )

        with (
            mock.patch("features.ssh_keygen.actions.ensure_ssh_agent"),
            mock.patch("features.ssh_keygen.actions.create_ed25519_keypair",
                       return_value=(True, "created")),
            mock.patch("features.ssh_keygen.actions.read_public_key",
                       return_value=("ssh-ed25519 AAA", "")),
        ):
            kick_off_generate(state, modal)
            _wait_jobs(state)

        self.assertFalse(modal.working)
        self.assertTrue(modal.done)
        self.assertEqual(modal.public_key, "ssh-ed25519 AAA")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "ssh-keygen")
        self.assertTrue(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)

    def test_thread_start_failure_clears_working(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        modal = SshKeygenModal(
            email="me@example.test",
            key_path_text="/tmp/idlegit-test-key",
            passphrase="secret",
        )

        with mock.patch("core.runtime.threads.threading.Thread", FailingThread):
            kick_off_generate(state, modal)

        self.assertFalse(modal.working)
        self.assertEqual(modal.error, "thread start failed")
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)


class TestSshAddJob(unittest.TestCase):
    def test_ssh_add_runs_as_read_only_job(self) -> None:
        state = _state()
        state.auto_start_ssh_agent = True
        tools = SshToolsStatus(
            has_ssh_agent=True,
            has_ssh_add=True,
            has_ssh_keygen=True,
            agent_running=True,
            keys_loaded=0,
        )

        with (
            mock.patch("core.ssh.ssh_tools_status",
                       return_value=tools),
            mock.patch("core.ssh.ensure_ssh_agent") as ensure,
            mock.patch("core.ssh.add_default_keys_to_agent",
                       return_value=(2, [])),
        ):
            kick_off_ssh_add_keys(state)
            _wait_jobs(state)

        ensure.assert_called_once_with(True)
        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "ssh-add-keys")
        self.assertFalse(jobs[0].spec.local_mutation)
        self.assertEqual(jobs[0].status, JobStatus.OK)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "2 key(s) loaded")

    def test_ssh_add_thread_start_failure_adds_failed_task(self) -> None:
        class FailingThread:
            daemon = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        state = _state()
        tools = SshToolsStatus(
            has_ssh_agent=True,
            has_ssh_add=True,
            has_ssh_keygen=True,
            agent_running=True,
            keys_loaded=0,
        )

        with (
            mock.patch("core.ssh.ssh_tools_status",
                       return_value=tools),
            mock.patch("core.runtime.threads.threading.Thread", FailingThread),
        ):
            kick_off_ssh_add_keys(state)

        jobs = state.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.FAIL)
        task = state.tasks.snapshot()[0]
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")


if __name__ == "__main__":
    unittest.main()
