"""Terminal input plumbing: mouse-wheel normalization + Alt+letter
detection. idlegit exposes a small set of Alt+letter chords as
alternatives to Shift+Arrow bindings — useful in terminals that don't
deliver Shift+arrow reliably, and required for VHS-driven demo tapes
(VHS only parses Shift+<character>, never Shift+<key-name>)."""
from __future__ import annotations

import curses

# Sentinel values above curses' KEY_MAX (~0o777). Returned by read_key
# when ESC is immediately followed by the relevant letter.
ALT_S = 0x10000 | ord('s')
ALT_M = 0x10000 | ord('m')

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


def _normalize_mouse(key: int) -> int:
    """Map KEY_MOUSE wheel presses to KEY_UP/KEY_DOWN. Unrecognized
    mouse events return -1 (ignore)."""
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


def read_key(stdscr) -> int:
    """One-shot keypress reader. Wraps ``stdscr.getch()`` with:

    - mouse-wheel → KEY_UP / KEY_DOWN translation, so list handlers
      don't special-case ``KEY_MOUSE``;
    - Alt+letter detection: an ESC immediately followed by ``s`` or
      ``m`` becomes ``ALT_S`` / ``ALT_M``. A bare ESC (no follower
      within one nonblocking poll) returns 27 as before. Any other
      Alt+<letter> falls back to bare ESC (the follower is dropped).

    Briefly toggles ``nodelay(True)`` to peek for a follower. The
    caller is assumed to re-assert ``timeout()`` on the next iteration
    (main + review loops both do); plain blocking is fine for callers
    that don't (e.g. the y/n confirm prompt)."""
    raw = stdscr.getch()
    if raw != 27:
        return _normalize_mouse(raw)
    stdscr.nodelay(True)
    try:
        nxt = stdscr.getch()
    finally:
        stdscr.nodelay(False)
    if nxt == -1:
        return 27
    if nxt == ord('s'):
        return ALT_S
    if nxt == ord('m'):
        return ALT_M
    return 27
