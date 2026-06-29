"""Side-effect-free smart-sync domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple


class CanonicalPlanStatus(str, Enum):
    """Overall planner result for one canonical checkout group."""

    NOOP = "noop"
    READY = "ready"
    WARN = "warn"


class SyncStepKind(str, Enum):
    """A single planned smart-sync action."""

    PROMPT_BRANCH = "prompt-branch"
    RESOLVE_ORIGIN_HEAD = "resolve-origin-head"
    COMMIT_DIRTY = "commit-dirty"
    PUSH_WINNER = "push-winner"
    ALIGN_FF = "align-ff"
    ALIGN_DETACHED = "align-detached"
    WARN_MANUAL = "warn-manual"


@dataclass(frozen=True)
class SmartSyncSettings:
    """User-configurable smart-sync behavior that affects planning."""

    auto_stage: bool
    auto_ff: bool
    align_heads: bool
    prompt_for_branch: bool


@dataclass(frozen=True)
class CheckoutSnapshot:
    """Read-only facts about one checkout in a canonical smart-sync group."""

    checkout_id: str
    label: str
    path: Path
    branch: str
    head: str = ""
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    parent_id: Optional[str] = None
    commit_time: int = 0
    sig_mtime: float = 0.0


@dataclass(frozen=True)
class SyncStep:
    """One action or terminal warning selected by the pure planner."""

    kind: SyncStepKind
    target_id: str
    message: str = ""


@dataclass(frozen=True)
class CanonicalPlan:
    """Pure execution plan for one canonical smart-sync group."""

    status: CanonicalPlanStatus
    winner_id: Optional[str] = None
    steps: Tuple[SyncStep, ...] = ()
    warning: str = ""
