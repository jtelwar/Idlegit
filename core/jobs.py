"""Temporary compatibility exports for runtime job primitives.

Production ownership lives in :mod:`core.runtime.jobs`. This shell exists only
while Phase 2 moves callers onto the runtime package.
"""
from __future__ import annotations

from .runtime.jobs import (
    Job,
    JobRegistry,
    JobSpec,
    JobStatus,
    JobTaskBridge,
    JobTaskOutcome,
    start_job_thread,
    submit_job,
)

__all__ = [
    "Job",
    "JobRegistry",
    "JobSpec",
    "JobStatus",
    "JobTaskBridge",
    "JobTaskOutcome",
    "start_job_thread",
    "submit_job",
]
