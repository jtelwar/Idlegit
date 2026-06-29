"""Temporary compatibility exports for runtime mutation leases.

Lease ownership lives in :mod:`core.runtime.leases`. This shell exists only
while Phase 2 moves state/runtime imports onto the runtime package.
"""
from __future__ import annotations

from core.runtime.leases import LeaseConflictError, LeaseManager, MutationLease

__all__ = [
    "LeaseConflictError",
    "LeaseManager",
    "MutationLease",
]
