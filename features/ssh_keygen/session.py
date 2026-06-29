"""SSH keygen modal session lifecycle."""
from __future__ import annotations

from core.state.app import State
from core.state.ssh_keygen import SshKeygenModal
from core.workers import kick_off_ssh_keygen_prepare


def open_ssh_keygen_modal(state: State) -> None:
    state.app_menu = None
    modal = SshKeygenModal(preparing=True)
    state.ssh_keygen_modal = modal
    kick_off_ssh_keygen_prepare(state, modal)
