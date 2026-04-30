"""Layout + display primitives shared across every part of the UI.

These are pure, leaf-level helpers — they don't know about Repo, Task,
or any modal in particular. Anything that does is one layer up
(`sidebar`, `main_screen`, `modals/*`)."""
from __future__ import annotations

import curses
from typing import Tuple

from config import DEFAULT_TRUNCATION_MODE


SIDEBAR_W = 50          # task panel keeps a constant width across resizes
SIDEBAR_W_NARROW = 30   # smaller terminals get a shrunk panel


def truncate(text: str, max_len: int,
             mode: str = DEFAULT_TRUNCATION_MODE) -> str:
    """Cap text at max_len (incl. ellipsis). `mode` is one of "start",
    "middle", or "end". Unknown modes fall back to "middle". max_len <= 0
    disables truncation entirely."""
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    keep = max_len - 1
    if mode == "start":
        return "…" + text[-keep:]
    if mode == "end":
        return text[:keep] + "…"
    head = (keep + 1) // 2
    tail = keep - head
    return text[:head] + "…" + text[-tail:]


def field_visible(message: str, cursor: int, inner_w: int,
                  focused: bool) -> Tuple[str, int]:
    """Return (visible_text, cursor_offset_within_visible) for a message
    field of `inner_w` cells. When focused, the window is centered on the
    cursor (clamped at the ends) so the cursor is always visible. When not
    focused, the window simply shows the tail of the message."""
    if len(message) <= inner_w:
        return message, cursor
    if not focused:
        return message[-inner_w:], inner_w
    half = inner_w // 2
    start = max(0, min(cursor - half, len(message) - inner_w))
    return message[start:start + inner_w], cursor - start


def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that swallows errors when writing to the bottom-right corner
    or off-screen after a resize."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    text = text[: max(0, w - x)]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def sidebar_geometry(w: int) -> Tuple[int, int]:
    """(sidebar_x, sidebar_w). The task panel keeps a constant width as
    the terminal resizes — only the message field changes. Falls back to
    a narrower panel on small terminals; only hides entirely when the
    terminal is too narrow for the panel + any usable main area.

    Thresholds keep the main panel at >=30 cells so commit-message
    fields don't shrink to single words on smaller windows."""
    if w < 60:
        return w, 0  # unusably narrow — drop the panel
    if w < 90:
        return w - SIDEBAR_W_NARROW, SIDEBAR_W_NARROW
    return w - SIDEBAR_W, SIDEBAR_W


def modal_geometry(stdscr, sidebar_x: int, content_w: int,
                   content_h: int) -> Tuple[int, int, int, int]:
    """Return (x, y, w, h) for a centered modal box that fits within the
    main panel (left of the sidebar) and leaves the sidebar visible."""
    h, w = stdscr.getmaxyx()
    main_w = sidebar_x if sidebar_x > 0 else w
    box_w = min(content_w, max(40, main_w - 2))
    box_h = min(content_h, max(8, h - 2))
    x = max(1, (main_w - box_w) // 2)
    y = max(1, (h - box_h) // 2)
    return x, y, box_w, box_h


def draw_modal_fill(stdscr, x: int, y: int, w: int, h: int, sb: int) -> None:
    """Paint the background rectangle for a modal at (x, y, w, h)."""
    fill = " " * w
    for row in range(y, y + h):
        safe_addstr(stdscr, row, x, fill, sb)
