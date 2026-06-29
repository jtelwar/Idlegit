"""Temporary compatibility exports for runtime thread helpers.

Thread helper ownership lives in :mod:`core.runtime.threads`. This shell exists
only while Phase 2 moves old imports onto the runtime package.
"""
from __future__ import annotations

from .runtime.threads import ThreadFactory, ThreadGroup

__all__ = [
    "ThreadFactory",
    "ThreadGroup",
]
