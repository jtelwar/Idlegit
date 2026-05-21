"""Terminal mouse wheel → arrow-key translation for scrollable UI."""
from __future__ import annotations

import curses

# macOS ncurses often omits BUTTON5_PRESSED; wheel-down uses a
# different bstate bit depending on terminal / ncurses build.
_WHEEL_DOWN_MASK = (
    getattr(curses, "BUTTON5_PRESSED", 0) | 0x200000 | 0x1000000
)


def enable_mouse() -> None:
    """Route mouse wheel events to the app (xterm mouse reporting).
    Without this, terminals such as iTerm scroll their backlog instead
    of delivering wheel input to curses."""
    try:
        curses.mousemask(
            curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except (AttributeError, curses.error):
        pass


def normalize_input(key: int) -> int:
    """Map KEY_MOUSE wheel presses to KEY_UP/KEY_DOWN so list handlers
    work unchanged. Unrecognized mouse events return -1 (ignore)."""
    if key != curses.KEY_MOUSE:
        return key
    try:
        _id, _x, _y, _z, bstate = curses.getmouse()
    except curses.error:
        return -1
    if bstate & curses.BUTTON4_PRESSED:
        return curses.KEY_UP
    if bstate & _WHEEL_DOWN_MASK:
        return curses.KEY_DOWN
    return -1
