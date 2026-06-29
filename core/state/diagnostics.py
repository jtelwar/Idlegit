"""Diagnostics over state-owned jobs and leases."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime.jobs import Job
    from core.state.app import State
    from ..runtime.leases import MutationLease
    from .repos import ChildRef, Repo


@dataclass(frozen=True)
class StaleOwnerDiagnostic:
    """One active owner that has exceeded its stale threshold."""

    owner_kind: str
    owner_id: str
    owner_label: str
    age_seconds: float
    stale_after_seconds: float
    target_label: str


def collect_stale_owner_diagnostics(
        state: "State",
        *,
        now: Optional[float] = None,
        default_job_stale_after_seconds: float = 0.0,
        default_lease_stale_after_seconds: float = 0.0,
) -> list[StaleOwnerDiagnostic]:
    """Return stale active job and lease owners without mutating state."""
    diagnostics: list[StaleOwnerDiagnostic] = []
    diagnostics.extend(
        _diagnostic_for_job(
            job,
            now=now,
            default_stale_after_seconds=default_job_stale_after_seconds,
        )
        for job in state.job_registry.stale_jobs(
            now=now,
            default_stale_after_seconds=default_job_stale_after_seconds,
        )
    )
    diagnostics.extend(
        _diagnostic_for_lease(
            lease,
            now=now,
            default_stale_after_seconds=default_lease_stale_after_seconds,
        )
        for lease in state.leases.stale_leases(
            now=now,
            default_stale_after_seconds=default_lease_stale_after_seconds,
        )
    )
    return diagnostics


def _diagnostic_for_job(
        job: "Job",
        *,
        now: Optional[float],
        default_stale_after_seconds: float,
) -> StaleOwnerDiagnostic:
    threshold = (
        job.spec.stale_after_seconds
        if job.spec.stale_after_seconds > 0
        else default_stale_after_seconds
    )
    return StaleOwnerDiagnostic(
        owner_kind="job",
        owner_id=f"job-{job.job_id}",
        owner_label=job.spec.label,
        age_seconds=job.age_seconds(now),
        stale_after_seconds=threshold,
        target_label=_job_target_label(job),
    )


def _diagnostic_for_lease(
        lease: "MutationLease",
        *,
        now: Optional[float],
        default_stale_after_seconds: float,
) -> StaleOwnerDiagnostic:
    threshold = (
        lease.stale_after_seconds
        if lease.stale_after_seconds > 0
        else default_stale_after_seconds
    )
    return StaleOwnerDiagnostic(
        owner_kind="lease",
        owner_id=lease.owner_id,
        owner_label=lease.owner_label,
        age_seconds=lease.age_seconds(now),
        stale_after_seconds=threshold,
        target_label=_lease_target_label(lease),
    )


def _job_target_label(job: "Job") -> str:
    parts = []
    if job.spec.repo_keys:
        parts.append(_shorten_paths(job.spec.repo_keys))
    if job.spec.child_keys:
        parts.append(_shorten_paths(job.spec.child_keys))
    return ", ".join(parts)


def _lease_target_label(lease: "MutationLease") -> str:
    parts = []
    if lease.repo is not None:
        parts.append(_repo_label(lease.repo))
    if lease.child is not None:
        parts.append(_child_label(lease.child))
    return ", ".join(parts)


def _repo_label(repo: "Repo") -> str:
    return repo.rel or repo.path.name


def _child_label(child: "ChildRef") -> str:
    return child.repo.rel or child.nested_path.name


def _shorten_paths(paths: tuple[str, ...]) -> str:
    labels = [Path(path).name or path for path in paths]
    if len(labels) <= 2:
        return ", ".join(labels)
    return f"{labels[0]}, {labels[1]} +{len(labels) - 2}"
