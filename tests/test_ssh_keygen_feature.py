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
from core.state.ssh_keygen import SshKeygenModal  # noqa: E402
from features.ssh_keygen.actions import handle_ssh_keygen_modal_key  # noqa: E402
from features.ssh_keygen.projection import generate_blocked_reason  # noqa: E402
from features.ssh_keygen.session import open_ssh_keygen_modal  # noqa: E402


class TestSshKeygenFeature(unittest.TestCase):
    def test_open_installs_preparing_modal_and_dispatches_prepare(self) -> None:
        state = _state()

        with mock.patch(
            "features.ssh_keygen.session.kick_off_ssh_keygen_prepare",
        ) as prepare:
            open_ssh_keygen_modal(state)

        self.assertIsNotNone(state.ssh_keygen_modal)
        self.assertTrue(state.ssh_keygen_modal.preparing)
        prepare.assert_called_once_with(state, state.ssh_keygen_modal)

    def test_empty_passphrase_confirm_y_dispatches_generate(self) -> None:
        state = _state()
        modal = SshKeygenModal(
            key_path_text="/tmp/idlegit-test-key",
            confirm_empty_passphrase=True,
        )
        state.ssh_keygen_modal = modal

        with mock.patch("features.ssh_keygen.actions.kick_off_generate") as kick:
            handle_ssh_keygen_modal_key(state, ord("y"))

        kick.assert_called_once_with(state, modal)

    def test_generate_button_requests_empty_passphrase_confirmation(self) -> None:
        state = _state()
        modal = SshKeygenModal(
            key_path_text="/tmp/idlegit-test-key",
            selected=3,
        )
        state.ssh_keygen_modal = modal

        handle_ssh_keygen_modal_key(state, curses.KEY_ENTER)

        self.assertTrue(modal.confirm_empty_passphrase)
        self.assertEqual(modal.selected, 2)

    def test_blocked_reason_requires_key_path(self) -> None:
        modal = SshKeygenModal()
        self.assertEqual(generate_blocked_reason(modal), "key path is required")
