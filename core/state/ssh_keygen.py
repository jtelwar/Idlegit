"""State-owned records for the SSH key generation modal."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SshKeygenModal:
    """Modal state for creating an ed25519 SSH keypair for GitHub."""

    email: str = ""
    key_path_text: str = ""
    passphrase: str = ""
    key_path_placeholder: str = "checking..."
    selected: int = 0
    edit_field: str = ""
    edit_pre_value: str = ""
    confirm_empty_passphrase: bool = False
    preparing: bool = False
    working: bool = False
    done: bool = False
    error: str = ""
    public_key: str = ""
    key_path: Optional[Path] = None
