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


def end_truncate(text: str, max_w: int) -> str:
    """End-only truncation: keep the front, drop the tail with an
    ellipsis. Differs from `truncate(..., mode="end")` only in how it
    behaves at degenerate widths — `max_w <= 0` returns ``""`` instead
    of pass-through, which is what callers laying out modal content
    actually want when their available width has shrunk to zero."""
    if max_w <= 0:
        return ""
    if len(text) <= max_w:
        return text
    if max_w == 1:
        return "…"
    return text[: max_w - 1] + "…"


def wrap_label_value(label: str, value: str, max_w: int) -> "list[str]":
    """Lay out one "label: value" pair across one or two lines.

    Repo-name modals use this to keep names readable when the window is
    narrow. The strict no-mid-truncation rule the user wants:

      - Try to fit ``"label: value"`` on one line. If it does → that's
        the only line.
      - Otherwise put ``"label:"`` on its own line, then the value
        indented two cells on the next.
      - If the value still overflows max_w on its own line, end-truncate
        with "…" — never middle-truncate, since the head of a long repo
        name (e.g. ``Upskill.Health.Domain.Models``) is what users
        actually recognise.

    Returns a list of strings. Caller renders each at successive y
    coordinates. ``max_w <= 0`` returns ``[]`` so callers in the
    pathological-narrow case render nothing rather than crash.

    >>> wrap_label_value("Winner", "short", 20)
    ['Winner: short']
    >>> wrap_label_value("Winner", "this-name-is-long", 20)
    ['Winner:', '  this-name-is-long']
    >>> wrap_label_value("Winner", "this-name-is-way-too-long-to-fit", 20)
    ['Winner:', '  this-name-is-way…']
    """
    if max_w <= 0:
        return []
    label_part = f"{label}:" if label else ""
    one_liner = f"{label_part} {value}".strip()
    if len(one_liner) <= max_w:
        return [one_liner]
    if not label_part:
        # No label: there's no two-line form, so just end-truncate the
        # value and return.
        return [end_truncate(value, max_w)]
    indent = "  "
    value_room = max_w - len(indent)
    return [label_part, indent + end_truncate(value, value_room)]


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
