"""SSH keygen key handling and generation job dispatch."""
from __future__ import annotations

import curses
from pathlib import Path

from core.runtime.jobs import JobSpec, submit_job
from core.runtime.threads import create_job_thread
from core.state.app import State
from core.state.ssh_keygen import SshKeygenModal
from core.ssh import (
    create_ed25519_keypair,
    ensure_ssh_agent,
    key_path_conflict_message,
    read_public_key,
)

from .projection import (
    FIELD_BUTTON,
    FIELD_EMAIL,
    FIELD_PASSPHRASE,
    FIELD_PATH,
    N_FIELDS,
    generate_blocked_reason,
)


def kick_off_generate(state: State, modal: SshKeygenModal) -> None:
    blocked = generate_blocked_reason(modal)
    if blocked:
        modal.error = blocked
        return
    email = modal.email.strip() or "idlegit"
    passphrase = modal.passphrase
    key_path = Path(modal.key_path_text.strip()).expanduser()

    modal.working = True
    modal.error = ""
    modal.confirm_empty_passphrase = False

    def worker(_job) -> None:
        if state.auto_start_ssh_agent:
            ensure_ssh_agent(True)
        ok, message = create_ed25519_keypair(
            key_path, email, passphrase=passphrase)
        pubkey, read_error = read_public_key(key_path)
        modal.working = False
        if ok:
            modal.done = True
            modal.key_path = key_path
            modal.public_key = pubkey or ""
            modal.error = message
        else:
            modal.error = message or "key generation failed"
        if read_error and modal.done:
            modal.error = read_error

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="ssh-keygen",
            label=f"ssh-keygen {key_path.name}",
            local_mutation=True,
            repo_keys=(str(key_path),),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        modal.working = False
        modal.error = job.message or "thread start failed"


def request_generate(state: State, modal: SshKeygenModal) -> None:
    if modal.edit_field:
        return
    if not modal.passphrase:
        modal.confirm_empty_passphrase = True
        modal.selected = FIELD_PASSPHRASE
        return
    kick_off_generate(state, modal)


def cancel_empty_passphrase_confirm(modal: SshKeygenModal) -> None:
    modal.confirm_empty_passphrase = False
    modal.selected = FIELD_PASSPHRASE
    modal.edit_pre_value = modal.passphrase
    modal.edit_field = "passphrase"


def handle_empty_passphrase_confirm(state: State, key: int) -> None:
    modal = state.ssh_keygen_modal
    if modal is None:
        return
    if key in (ord("y"), ord("Y")):
        kick_off_generate(state, modal)
        return
    if key in (ord("n"), ord("N"), 10, 13, curses.KEY_ENTER):
        cancel_empty_passphrase_confirm(modal)
        return
    if key == 27:
        modal.confirm_empty_passphrase = False


def handle_ssh_keygen_modal_key(state: State, key: int) -> None:
    modal = state.ssh_keygen_modal
    if modal is None:
        return

    if modal.done:
        if key in (27, 9, 10, 13, curses.KEY_ENTER):
            state.ssh_keygen_modal = None
        return

    if modal.working:
        return
    if modal.preparing:
        if key in (27, 9):
            state.ssh_keygen_modal = None
        return

    if modal.confirm_empty_passphrase:
        handle_empty_passphrase_confirm(state, key)
        return

    if key in (27, 9):
        state.ssh_keygen_modal = None
        return

    if modal.edit_field:
        handle_edit_key(modal, key)
        return

    if key == curses.KEY_UP:
        modal.selected = max(0, modal.selected - 1)
        return
    if key == curses.KEY_DOWN:
        modal.selected = min(N_FIELDS - 1, modal.selected + 1)
        return

    if key in (10, 13, curses.KEY_ENTER):
        handle_enter_key(state, modal)


def handle_enter_key(state: State, modal: SshKeygenModal) -> None:
    if modal.selected == FIELD_EMAIL:
        modal.edit_pre_value = modal.email
        modal.edit_field = "email"
        return
    if modal.selected == FIELD_PATH:
        modal.edit_pre_value = modal.key_path_text
        modal.edit_field = "path"
        return
    if modal.selected == FIELD_PASSPHRASE:
        modal.edit_pre_value = modal.passphrase
        modal.edit_field = "passphrase"
        return
    if modal.selected == FIELD_BUTTON and not generate_blocked_reason(modal):
        request_generate(state, modal)


def handle_edit_key(modal: SshKeygenModal, key: int) -> None:
    if key in (10, 13, curses.KEY_ENTER):
        if modal.edit_field == "passphrase" and not modal.passphrase:
            modal.edit_field = ""
            modal.confirm_empty_passphrase = True
            modal.selected = FIELD_PASSPHRASE
            return
        modal.edit_field = ""
        return
    if key == 27:
        cancel_edit(modal)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        delete_char(modal)
        return
    if 32 <= key < 127:
        append_char(modal, chr(key))


def cancel_edit(modal: SshKeygenModal) -> None:
    if modal.edit_field == "email":
        modal.email = modal.edit_pre_value
    elif modal.edit_field == "path":
        modal.key_path_text = modal.edit_pre_value
    elif modal.edit_field == "passphrase":
        modal.passphrase = modal.edit_pre_value
    modal.edit_field = ""


def delete_char(modal: SshKeygenModal) -> None:
    if modal.edit_field == "email":
        modal.email = modal.email[:-1]
    elif modal.edit_field == "path":
        modal.key_path_text = modal.key_path_text[:-1]
        if key_path_conflict_message(modal.key_path_text):
            modal.error = ""
    elif modal.edit_field == "passphrase":
        modal.passphrase = modal.passphrase[:-1]


def append_char(modal: SshKeygenModal, char: str) -> None:
    if modal.edit_field == "email":
        modal.email += char
    elif modal.edit_field == "path":
        modal.key_path_text += char
        if key_path_conflict_message(modal.key_path_text):
            modal.error = ""
    elif modal.edit_field == "passphrase":
        modal.passphrase += char
