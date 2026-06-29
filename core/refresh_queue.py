"""Refresh queue bookkeeping for workspace refresh orchestration."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class InlineRefreshQueue:
    """Coalesce inline refresh requests per workspace target.

    The queue only owns scheduling state. Callers still own repo refresh locks,
    job records, and UI task rows.
    """

    stale_seconds: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    in_flight: Set[int] = field(default_factory=set)
    pending: Set[int] = field(default_factory=set)
    started_at: Dict[int, float] = field(default_factory=dict)

    def try_start(
            self,
            target_idx: int,
            *,
            manual: bool,
            now: float,
            workspace_busy: bool,
    ) -> bool:
        """Return True when the caller should start a refresh immediately."""
        if target_idx in self.in_flight:
            started = self.started_at.get(target_idx, 0.0)
            stale = (
                manual
                and target_idx in self.started_at
                and now - started > self.stale_seconds
                and not workspace_busy
            )
            if not stale:
                self.pending.add(target_idx)
                return False
            self._discard_target(target_idx)

        self.in_flight.add(target_idx)
        self.started_at[target_idx] = now
        self.pending.discard(target_idx)
        return True

    def complete(
            self,
            target_idx: int,
            *,
            active_current: bool,
            stale_result: bool,
    ) -> bool:
        """Release a target and return whether a pending refresh should run."""
        pending_this_target = target_idx in self.pending
        self._discard_target(target_idx)
        return (pending_this_target or stale_result) and active_current

    def release(self, target_idx: int) -> None:
        """Release a target without scheduling follow-up work."""
        self._discard_target(target_idx)

    def has_in_flight(self) -> bool:
        return bool(self.in_flight)

    def has_pending(self) -> bool:
        return bool(self.pending)

    def _discard_target(self, target_idx: int) -> None:
        self.in_flight.discard(target_idx)
        self.pending.discard(target_idx)
        self.started_at.pop(target_idx, None)
