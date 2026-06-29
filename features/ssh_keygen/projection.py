"""SSH keygen modal projection helpers."""
from __future__ import annotations

from pathlib import Path

from core.state.ssh_keygen import SshKeygenModal
from core.ssh import github_new_key_url, key_path_conflict_message


PAD_TOP = 1
PAD_BOTTOM = 1
PAD_X = 2
MODAL_W = 78
LABEL_W = 14

FIELD_EMAIL = 0
FIELD_PATH = 1
FIELD_PASSPHRASE = 2
FIELD_BUTTON = 3
N_FIELDS = 4

CONFIRM_PROMPT = "Continue without setting a passkey? [y/N]"


def passphrase_display(modal: SshKeygenModal, *, editing: bool) -> str:
    if editing:
        return modal.passphrase
    if modal.passphrase:
        return "*" * len(modal.passphrase)
    return ""


def generate_blocked_reason(modal: SshKeygenModal) -> str | None:
    conflict = key_path_conflict_message(modal.key_path_text)
    if conflict:
        return conflict
    text = modal.key_path_text.strip()
    if not text:
        return "key path is required"
    try:
        Path(text).expanduser()
    except (TypeError, ValueError):
        return "invalid key path"
    return None


def github_key_url() -> str:
    return github_new_key_url()


def key_path_warning(modal: SshKeygenModal) -> str:
    return key_path_conflict_message(modal.key_path_text)
