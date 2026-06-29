"""State-owned lease tracking for local mutation ownership."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from itertools import count
from typing import Callable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.state.ids import ChildId, RepoId
    from core.state.repos import ChildRef, Repo


_LEASE_IDS = count(1)


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class MutationLease:
    """Snapshot of local mutation ownership."""

    lease_id: int
    owner_id: str
    owner_label: str
    repo: Optional["Repo"]
    child: Optional["ChildRef"]
    repo_id: Optional["RepoId"]
    child_id: Optional["ChildId"]
    started_at: float
    stale_after_seconds: float = 0.0

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Return monotonic age for active diagnostics."""
        current = _monotonic() if now is None else now
        return max(0.0, current - self.started_at)

    def is_stale(
            self,
            *,
            now: Optional[float] = None,
            default_stale_after_seconds: float = 0.0,
    ) -> bool:
        """Return whether this active lease has exceeded its stale threshold."""
        threshold = (
            self.stale_after_seconds
            if self.stale_after_seconds > 0
            else default_stale_after_seconds
        )
        if threshold <= 0:
            return False
        return self.age_seconds(now) >= threshold


class LeaseConflictError(RuntimeError):
    """Raised when a local mutation target is already leased."""


class LeaseManager:
    """Thread-safe owner of local mutation leases."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._leases: dict[int, MutationLease] = {}
        self.on_change = on_change

    def acquire(
            self,
            repo: Optional["Repo"] = None,
            child: Optional["ChildRef"] = None,
            repo_id: Optional["RepoId"] = None,
            child_id: Optional["ChildId"] = None,
            owner_id: str = "",
            owner_label: str = "",
            stale_after_seconds: float = 0.0,
    ) -> int:
        """Register exclusive local mutation ownership for a repo and/or child."""
        if repo is None and child is None and repo_id is None and child_id is None:
            return 0
        with self._lock:
            conflict = self._conflicting_lease_locked(
                repo=repo,
                child=child,
                repo_id=repo_id,
                child_id=child_id,
            )
            if conflict is not None:
                owner = conflict.owner_label or conflict.owner_id
                raise LeaseConflictError(f"{owner} already owns this row")
            lease_id = next(_LEASE_IDS)
            self._leases[lease_id] = MutationLease(
                lease_id=lease_id,
                owner_id=owner_id or f"lease-{lease_id}",
                owner_label=owner_label or "worker",
                repo=repo,
                child=child,
                repo_id=repo_id,
                child_id=child_id,
                started_at=_monotonic(),
                stale_after_seconds=stale_after_seconds,
            )
        self._notify_change()
        return lease_id

    def try_acquire(
            self,
            repo: Optional["Repo"] = None,
            child: Optional["ChildRef"] = None,
            repo_id: Optional["RepoId"] = None,
            child_id: Optional["ChildId"] = None,
            owner_id: str = "",
            owner_label: str = "",
            stale_after_seconds: float = 0.0,
    ) -> int:
        """Return a lease id, or 0 when another mutation already owns it."""
        try:
            return self.acquire(
                repo=repo,
                child=child,
                repo_id=repo_id,
                child_id=child_id,
                owner_id=owner_id,
                owner_label=owner_label,
                stale_after_seconds=stale_after_seconds,
            )
        except LeaseConflictError:
            return 0

    def release(self, lease_id: int) -> None:
        """Release a lease id returned by acquire."""
        if lease_id == 0:
            return
        with self._lock:
            removed = self._leases.pop(lease_id, None)
        if removed is not None:
            self._notify_change()

    def snapshot(self) -> List[MutationLease]:
        """Return active mutation leases for diagnostics/tests."""
        with self._lock:
            return list(self._leases.values())

    def has_leases(self) -> bool:
        """Return whether any local mutation lease is active."""
        with self._lock:
            return bool(self._leases)

    def has_lease_for(
            self,
            repos: Optional[List["Repo"]] = None,
            children: Optional[List["ChildRef"]] = None,
            repo_ids: Optional[List["RepoId"]] = None,
            child_ids: Optional[List["ChildId"]] = None,
    ) -> bool:
        """Return whether local mutation leases overlap the targets."""
        repo_objects = {id(repo) for repo in (repos or [])}
        child_objects = {id(child) for child in (children or [])}
        repo_targets = set(repo_ids or [])
        child_targets = set(child_ids or [])
        if not repo_objects and not child_objects and not repo_targets and not child_targets:
            return self.has_leases()
        with self._lock:
            for lease in self._leases.values():
                if lease.repo is not None and id(lease.repo) in repo_objects:
                    return True
                if lease.child is not None and id(lease.child) in child_objects:
                    return True
                if lease.repo_id is not None and lease.repo_id in repo_targets:
                    return True
                if lease.child_id is not None and lease.child_id in child_targets:
                    return True
            return False

    def stale_leases(
            self,
            *,
            now: Optional[float] = None,
            default_stale_after_seconds: float = 0.0,
    ) -> List[MutationLease]:
        """Return active leases that have exceeded their stale threshold."""
        with self._lock:
            return [
                lease for lease in self._leases.values()
                if lease.is_stale(
                    now=now,
                    default_stale_after_seconds=default_stale_after_seconds,
                )
            ]

    def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:  # noqa: BLE001
            pass

    def _conflicting_lease_locked(
            self,
            *,
            repo: Optional["Repo"],
            child: Optional["ChildRef"],
            repo_id: Optional["RepoId"],
            child_id: Optional["ChildId"],
    ) -> Optional[MutationLease]:
        for lease in self._leases.values():
            if _leases_overlap(
                lease,
                repo=repo,
                child=child,
                repo_id=repo_id,
                child_id=child_id,
            ):
                return lease
        return None


def _leases_overlap(
        lease: MutationLease,
        *,
        repo: Optional["Repo"],
        child: Optional["ChildRef"],
        repo_id: Optional["RepoId"],
        child_id: Optional["ChildId"],
) -> bool:
    if repo is not None and lease.repo is not None and lease.repo is repo:
        return True
    if child is not None and lease.child is not None and lease.child is child:
        return True
    if repo_id is not None and lease.repo_id == repo_id:
        return True
    if child_id is not None and lease.child_id == child_id:
        return True
    if repo_id is not None and lease.child_id is not None:
        return _child_belongs_to_repo(lease.child_id, repo_id)
    if child_id is not None and lease.repo_id is not None:
        return _child_belongs_to_repo(child_id, lease.repo_id)
    return False


def _child_belongs_to_repo(
        child_id: "ChildId",
        repo_id: "RepoId",
) -> bool:
    prefix = f"{repo_id}:"
    return str(child_id).startswith(prefix)
