"""Thread-safe UI wake/change signalling."""

from __future__ import annotations

import threading


class UiEvents:
    """Coalesced state-change signal for the curses loop."""

    def __init__(self) -> None:
        self._changed = threading.Event()

    def notify(self) -> None:
        self._changed.set()

    def drain(self) -> bool:
        """Return whether a change was pending, then clear the signal."""
        changed = self._changed.is_set()
        if changed:
            self._changed.clear()
        return changed

    def is_set(self) -> bool:
        return self._changed.is_set()
