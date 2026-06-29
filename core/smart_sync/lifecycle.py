"""Smart-sync task presentation and row-state lifecycle."""

from __future__ import annotations

from typing import Dict, List, Tuple

from ..runtime.jobs import Job, JobStatus, JobTaskBridge
from ..runtime.claims import CanonicalTreeClaim, WorkerClaim
from core.state.app import State
from ..state.repos import ChildRef, Repo
from ..runtime.tasks import Task


class SmartSyncLifecycle:
    """Own smart-sync sentinel rows, row refresh state, and final status."""

    def __init__(
            self,
            state: State,
            header: Task,
            job: Job,
            canonicals: List[Repo],
            subtree_items: List[Tuple[Repo, ChildRef]],
    ) -> None:
        self.state = state
        self.header = header
        self.job = job
        self.canonicals = canonicals
        self.subtree_items = subtree_items
        self.sentinel_row_claims: List[WorkerClaim] = []
        self.sentinel_by_canonical: Dict[int, Task] = {}
        self.sentinel_by_subtree: Dict[int, Task] = {}
        self.result_by_canonical: Dict[int, Tuple[str, str]] = {}
        self.result_by_subtree: Dict[int, Tuple[str, str]] = {}
        self.task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
        self.task_bridge.attach(header)
        self.canonical_tree_claims: List[CanonicalTreeClaim] = []

    def acquire(self) -> None:
        """Create sentinel rows and enter row refresh claims."""
        try:
            self._acquire_canonicals()
            self._acquire_subtrees()
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        """Release all sentinel-owned row state idempotently."""
        for claim in self.canonical_tree_claims:
            claim.release()
        for claim in self.sentinel_row_claims:
            claim.__exit__(None, None, None)

    def fail_acquire(self, job: Job, message: str) -> None:
        """Terminalize the header after synchronous sentinel acquisition fails."""
        self.task_bridge.update(self.header, "fail", message)
        self.state.job_registry.finish(job, JobStatus.FAIL, message)

    def fail_thread_start(self, message: str) -> None:
        """Clear sentinel state after the smart-sync worker thread fails to start."""
        self.release()
        for sent in list(self.sentinel_by_canonical.values()):
            if sent.status not in ("ok", "fail", "warn"):
                self.task_bridge.update(sent, "fail", message)
        for sent in list(self.sentinel_by_subtree.values()):
            if sent.status not in ("ok", "fail", "warn"):
                self.task_bridge.update(sent, "fail", message)
        self.task_bridge.update(self.header, "fail", message)

    def record_canonical_result(self, canonical: Repo, fail_count: int) -> None:
        """Keep a canonical sentinel running until final cleanup releases row state."""
        sent = self.sentinel_by_canonical.get(id(canonical))
        if sent is None:
            return
        status = "ok" if fail_count == 0 else "warn"
        message = "" if fail_count == 0 else f"{fail_count} failed"
        self.result_by_canonical[id(canonical)] = (status, message)
        self.task_bridge.update(
            sent, "running", "aligned" if fail_count == 0 else message)

    def record_subtree_result(self, ref: ChildRef, ok: bool) -> None:
        """Keep a subtree sentinel running until final cleanup releases row state."""
        sent = self.sentinel_by_subtree.get(id(ref))
        if sent is None:
            return
        status = "ok" if ok else "warn"
        message = "" if ok else "subtree sync failed"
        self.result_by_subtree[id(ref)] = (status, message)
        self.task_bridge.update(sent, "running", "aligned" if ok else message)

    def finish(self, job: Job, ok_total: int, fail_total: int) -> None:
        """Release row state, terminalize sentinels/header, and finish the job."""
        self.release()
        self._terminalize_sentinels()
        status, message = self._terminalize_header(ok_total, fail_total)
        self.state.job_registry.finish(job, status, message)

    def cancel(self, job: Job, message: str = "cancelled") -> None:
        """Release row state and terminalize smart-sync as cancelled."""
        self.release()
        for sent in list(self.sentinel_by_canonical.values()):
            self.task_bridge.update(sent, "warn", message)
        for sent in list(self.sentinel_by_subtree.values()):
            self.task_bridge.update(sent, "warn", message)
        self.task_bridge.update(self.header, "warn", message)
        self.state.job_registry.finish(job, JobStatus.CANCELLED, message)

    def _acquire_canonicals(self) -> None:
        for canonical in self.canonicals:
            tree_claim = CanonicalTreeClaim(self.state, canonical)
            tree_claim.acquire()
            self.canonical_tree_claims.append(tree_claim)
            sent = self.task_bridge.add(
                f"  ↳ smart-sync {self.state.task_repo_label(canonical)}",
                parent=self.header,
            )
            claim = WorkerClaim(
                self.state,
                repo=canonical,
                task=sent,
                mark_repo=False,
                claim_mutation=False,
            )
            claim.__enter__()
            self.sentinel_row_claims.append(claim)
            self.sentinel_by_canonical[id(canonical)] = sent

    def _acquire_subtrees(self) -> None:
        for parent, ref in self.subtree_items:
            sent = self.task_bridge.add(
                f"  ⊕ smart-sync {self.state.task_repo_label(ref.repo)}",
                parent=self.header,
            )
            claim = WorkerClaim(
                self.state,
                repo=parent,
                child=ref,
                task=sent,
                mark_repo=False,
                mark_child=True,
                claim_mutation=False,
            )
            claim.__enter__()
            self.sentinel_row_claims.append(claim)
            self.sentinel_by_subtree[id(ref)] = sent

    def _terminalize_sentinels(self) -> None:
        for canonical in self.canonicals:
            sent = self.sentinel_by_canonical.get(id(canonical))
            if sent is None:
                continue
            status, message = self.result_by_canonical.get(
                id(canonical), ("warn", "smart-sync aborted"))
            self.task_bridge.update(sent, status, message)
        for _parent, ref in self.subtree_items:
            sent = self.sentinel_by_subtree.get(id(ref))
            if sent is None:
                continue
            status, message = self.result_by_subtree.get(
                id(ref), ("warn", "smart-sync aborted"))
            self.task_bridge.update(sent, status, message)

    def _terminalize_header(self, ok_total: int, fail_total: int) -> Tuple[JobStatus, str]:
        total = ok_total + fail_total
        if total == 0:
            status, message = JobStatus.OK, "all aligned"
        elif fail_total == 0:
            status, message = JobStatus.OK, f"{ok_total} synced"
        elif ok_total == 0:
            status, message = JobStatus.FAIL, f"{fail_total} failed"
        else:
            status, message = JobStatus.WARN, f"{ok_total} ok / {fail_total} failed"
        self.task_bridge.update(self.header, status.value, message)
        return status, message
