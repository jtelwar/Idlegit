"""Lease helpers for worker ownership of repo and child UI state."""
from __future__ import annotations
from itertools import count
from typing import List, Optional

from core.state.app import State
from core.state.repos import ChildRef, Repo
from core.state.row_state import (
    set_child_refreshing,
    set_repo_refreshing,
)
from core.runtime.tasks import Task


_LEASE_IDS = count(1)


class WorkerClaim:
    """Own a worker's repo/child UI claim and release it in one place."""

    def __init__(self, state: State, *,
                 repo: Optional[Repo] = None,
                 child: Optional[ChildRef] = None,
                 task: Optional[Task] = None,
                 cancel_job_id: Optional[int] = None,
                 acquire_repo: bool = False,
                 acquire_child: bool = False,
                 repo_timeout: Optional[float] = None,
                 child_timeout: Optional[float] = None,
                 mark_repo: bool = True,
                 mark_child: bool = True,
                 claim_mutation: bool = True,
                 owner_id: str = "",
                 owner_label: str = "worker") -> None:
        self.state = state
        self.repo = repo
        self.child = child
        self.task = task
        self.cancel_job_id = cancel_job_id
        self.acquire_repo = acquire_repo
        self.acquire_child = acquire_child
        self.repo_timeout = repo_timeout
        self.child_timeout = child_timeout
        self.mark_repo = mark_repo
        self.mark_child = mark_child
        self.claim_mutation = claim_mutation
        self.owner_id = owner_id or f"lease-{next(_LEASE_IDS)}"
        self.owner_label = owner_label
        self.repo_acquired = False
        self.child_acquired = False
        self.repo_id = None
        self.child_id = None
        self._mutation_claim_id = 0

    @property
    def target_repos(self) -> List[Repo]:
        """Repos explicitly targeted by this lease."""
        return [self.repo] if self.repo is not None else []

    @property
    def target_children(self) -> List[ChildRef]:
        """Child rows explicitly targeted by this lease."""
        return [self.child] if self.child is not None else []

    def __enter__(self) -> "WorkerClaim":
        if self.repo is not None:
            if self.acquire_repo:
                if self.state.store.repo_busy(self.repo):
                    raise RuntimeError("repo refresh in progress")
                self.repo_acquired, self.repo_id = (
                    self.state.store.acquire_repo_refresh(
                        self.repo,
                        timeout=self.repo_timeout,
                    ))
                if not self.repo_acquired:
                    raise RuntimeError("repo refresh in progress")
                self.state.store.set_repo_busy(self.repo, True)
            elif self.mark_repo:
                set_repo_refreshing(self.state, self.repo, True)
        if self.child is not None:
            if self.acquire_child:
                if self.state.store.child_busy(self.child):
                    if self.repo_acquired and self.repo is not None:
                        self.state.store.release_repo_refresh_by_id(
                            self.repo_id)
                        self.state.store.set_busy_snapshot_by_id(
                            self.repo_id, [], False)
                        self.repo_acquired = False
                        self.repo_id = None
                    elif self.repo is not None and self.mark_repo:
                        set_repo_refreshing(self.state, self.repo, False)
                    raise RuntimeError("child refresh in progress")
                self.child_acquired, self.child_id = (
                    self.state.store.acquire_child_refresh(
                        self.child,
                        timeout=self.child_timeout,
                    ))
                if not self.child_acquired:
                    if self.repo_acquired and self.repo is not None:
                        self.state.store.release_repo_refresh_by_id(
                            self.repo_id)
                        self.state.store.set_busy_snapshot_by_id(
                            self.repo_id, [], False)
                        self.repo_acquired = False
                        self.repo_id = None
                    elif self.repo is not None and self.mark_repo:
                        set_repo_refreshing(self.state, self.repo, False)
                    raise RuntimeError("child refresh in progress")
                self.state.store.set_child_busy(self.child, True)
            elif self.mark_child:
                set_child_refreshing(self.state, self.child, True)
        if self.task is not None:
            if self.cancel_job_id is not None:
                self.state.job_registry.link_task_by_id(
                    self.cancel_job_id,
                    self.task,
                )
        if self.claim_mutation and (self.repo is not None or self.child is not None):
            try:
                repo_id = (
                    self.state.store.repo_id_for(self.repo)
                    if self.repo is not None else None
                )
                child_id = (
                    self.state.store.child_id_for(self.child)
                    if self.child is not None else None
                )
                self._mutation_claim_id = self.state.leases.acquire(
                    self.repo,
                    self.child,
                    repo_id=repo_id,
                    child_id=child_id,
                    owner_id=self.owner_id,
                    owner_label=self.owner_label,
                )
            except Exception:
                self.__exit__(None, None, None)
                raise
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.state.leases.release(self._mutation_claim_id)
        self._mutation_claim_id = 0
        if self.child is not None:
            if self.child_acquired:
                self.state.store.release_child_refresh_by_id(self.child_id)
                if self.child_id is not None:
                    self.state.store.set_busy_snapshot_by_id(
                        None, [self.child_id], False)
                self.child_acquired = False
                self.child_id = None
            elif self.mark_child:
                set_child_refreshing(self.state, self.child, False)
        if self.repo is not None:
            if self.repo_acquired:
                self.state.store.release_repo_refresh_by_id(self.repo_id)
                self.state.store.set_busy_snapshot_by_id(
                    self.repo_id, [], False)
                self.repo_acquired = False
                self.repo_id = None
            elif self.mark_repo:
                set_repo_refreshing(self.state, self.repo, False)


class RefreshClaim:
    """Own a read-only refresh row lock and store-backed busy state."""

    def __init__(
            self,
            state: State,
            *,
            repo: Optional[Repo] = None,
            child: Optional[ChildRef] = None,
            timeout: Optional[float] = None,
    ) -> None:
        self.state = state
        self.repo = repo
        self.child = child
        self.timeout = timeout
        self.acquired = False
        self.repo_id = None
        self.child_id = None

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if self.repo is None and self.child is None:
            return False
        if self.repo is not None:
            self.acquired = self._acquire_repo()
            if self.acquired:
                self.state.store.set_repo_busy(self.repo, True)
            return self.acquired
        self.acquired = self._acquire_child()
        if self.acquired and self.child is not None:
            self.state.store.set_child_busy(self.child, True)
        return self.acquired

    def release(self) -> None:
        if not self.acquired:
            return
        if self.child is not None:
            self.state.store.release_child_refresh_by_id(self.child_id)
            if self.child_id is not None:
                self.state.store.set_busy_snapshot_by_id(
                    None, [self.child_id], False)
            self.child_id = None
        elif self.repo is not None:
            self.state.store.release_repo_refresh_by_id(self.repo_id)
            self.state.store.set_busy_snapshot_by_id(
                self.repo_id, [], False)
            self.repo_id = None
        self.acquired = False

    def __enter__(self) -> "RefreshClaim":
        if not self.acquire():
            raise RuntimeError("refresh in progress")
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()

    def _acquire_repo(self) -> bool:
        if self.repo is None:
            return False
        if self.state.store.repo_busy(self.repo):
            return False
        acquired, rid = self.state.store.acquire_repo_refresh(
            self.repo,
            timeout=self.timeout,
        )
        self.repo_id = rid if acquired else None
        return acquired

    def _acquire_child(self) -> bool:
        if self.child is None:
            return False
        if self.state.store.child_busy(self.child):
            return False
        acquired, cid = self.state.store.acquire_child_refresh(
            self.child,
            timeout=self.timeout,
        )
        self.child_id = cid if acquired else None
        return acquired


class CanonicalTreeClaim:
    """Own read-only busy state for a canonical repo and its child rows."""

    def __init__(self, state: State, canonical: Repo) -> None:
        self.state = state
        self.canonical = canonical
        self.acquired = False
        self.repo_id = None
        self.child_ids = []

    def acquire(self) -> bool:
        if self.acquired:
            return True
        self.repo_id = self.state.store.repo_id_for(self.canonical)
        self.child_ids = []
        workspace_id = self.state.store.active_workspace_id
        if self.repo_id is not None and workspace_id is not None:
            for repo_record in self.state.store.repo_records_for_workspace(
                    workspace_id):
                for child_record in self.state.store.child_records_for_repo(
                        repo_record.repo_id):
                    if child_record.repo_id != self.repo_id:
                        continue
                    status = self.state.store.child_status_by_id(
                        child_record.child_id)
                    kind = child_record.kind if status is None else status.kind
                    if kind == "submodule":
                        self.child_ids.append(child_record.child_id)
        self.state.store.set_busy_snapshot_by_id(
            self.repo_id,
            self.child_ids,
            True,
        )
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        self.state.store.set_busy_snapshot_by_id(
            self.repo_id,
            self.child_ids,
            False,
        )
        self.repo_id = None
        self.child_ids = []
        self.acquired = False

    def __enter__(self) -> "CanonicalTreeClaim":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
