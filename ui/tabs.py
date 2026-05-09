"""Reusable tab-strip component.

Owners (the action menu's working-tree / commits split, the diff
viewer's diff / log / blame split, the commit view's
changes / reflog split) keep their own per-tab state — `active_id`
plus whatever data each tab carries. This module is stateless: it
paints the `[ Label (count) ] [ Label (count) ]` header and offers
a `cycle_tab` helper for ←/→ switching, so the visual treatment
stays consistent across screens without each owner re-rolling the
loop."""
from __future__ import annotations

import curses
from typing import List, Tuple

from .colors import PAIR_DLG_CYAN
from .geometry import safe_addstr


def draw_tab_strip(stdscr, y: int, x: int, max_w: int,
                   tabs: "List[Tuple[str, str, str]]",
                   active_id: str, focused: bool,
                   base_attr: int) -> None:
    """Paint a tab header at row `y`, column `x`, capped at
    `max_w` cells. `tabs` is `[(id, label, count_str), …]` —
    pass `count_str=""` to skip the parenthesized count column.

    Active tab uses the cyan accent + bold when `focused`, plain
    bold when active-but-unfocused, dim otherwise. Tabs that don't
    fit are dropped silently (the caller can pre-truncate if they
    want a "…" indicator)."""
    cur_x = x
    for tid, label, count in tabs:
        active = tid == active_id
        text = f" {label} ({count}) " if count else f" {label} "
        if active and focused:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        elif active:
            attr = base_attr | curses.A_BOLD
        else:
            attr = base_attr | curses.A_DIM
        if cur_x + len(text) > x + max_w:
            break
        safe_addstr(stdscr, y, cur_x, text, attr)
        cur_x += len(text) + 1


def cycle_tab(tabs: "List[Tuple[str, str, str]]",
              active_id: str, direction: int) -> str:
    """Return the id `direction` steps from `active_id` in the tab
    list, wrapping around the ends. Returns `active_id` unchanged
    when there's only one tab or the id isn't in the list — the
    caller can safely chain this without checking."""
    if len(tabs) <= 1:
        return active_id
    ids = [t[0] for t in tabs]
    try:
        i = ids.index(active_id)
    except ValueError:
        return active_id
    return ids[(i + direction) % len(ids)]
