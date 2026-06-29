"""State-owned smart-sync runtime records."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .repos import Repo


@dataclass
class SmartSyncCheckout:
    """One checkout of a canonical submodule captured during smart-sync."""

    canonical: Repo
    parent: Optional[Repo]
    path: Path
    branch: str
    label: str
    head: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    upstream: Optional[str] = None
    signature: Tuple[Tuple[str, str], ...] = ()
    sig_mtime: float = 0.0
