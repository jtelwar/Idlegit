"""Color pairs + init_colors + the per-row state-colour helpers.

This module is leaf-level: it knows about the palette and the
state-precedence rules, but nothing about Repo or ChildRef shapes
beyond what `_state_color` accepts as keyword args."""
from __future__ import annotations

import curses
from typing import Optional, Tuple

from models import ChildRef, Repo


# Foreground-on-default-bg pairs, used across the main panel.
PAIR_BRANCH = 1
PAIR_DIRTY = 2
PAIR_TOGGLE_ON = 3
PAIR_TOGGLE_OFF = 4
PAIR_HINT = 5
PAIR_OK = 6
PAIR_ERR = 7
PAIR_WARN = 8
PAIR_HEADER = 9
PAIR_AHEAD = 10
PAIR_BEHIND = 11
# Extra-muted variant of PAIR_SB_FG, used for "unavailable" controls
# (e.g. the right-pane toolbar's "stage all" button when every file
# is already staged). On 256-colour terminals it's a dim grey;
# fallback is PAIR_SB_FG + A_DIM at usage site for 8/16-colour TTYs.
PAIR_SB_FG_DISABLED = 31
# Focused-title accent — same magenta family as PAIR_HEADER but a
# notch brighter so the focused row pops against the at-rest title
# colour. 256-colour terminals get xterm orchid2 (213); fallback is
# plain magenta + bold which matches PAIR_HEADER on 8-colour TTYs.
PAIR_HEADER_ACTIVE = 30
# Sidebar pairs share a darker bg so the panel reads as a distinct surface.
PAIR_SB_FG = 12
PAIR_SB_CYAN = 13
PAIR_SB_OK = 14
PAIR_SB_ERR = 15
PAIR_SB_WARN = 16
# Active-panel variant — same fg, but a slightly lighter bg so the panel
# reads as "currently focused" without going as far as inverse video.
PAIR_SB_FG_ACTIVE = 17
# Active variants of each status colour: identical fg, sb_bg_active bg so
# icons/labels rendered inside a focused panel don't punch holes in the fill.
PAIR_SB_CYAN_ACTIVE = 22
PAIR_SB_OK_ACTIVE = 23
PAIR_SB_ERR_ACTIVE = 24
PAIR_SB_WARN_ACTIVE = 25

# Pastel pairs used inside dark-bg modals (action menu's bottom pane,
# task detail) for diff stats, file-status codes, commit hashes/dates.
# On 256-colour terminals these pull from the soft pastel band of the
# xterm-256 palette; on 16-colour terminals they fall back to the
# nearest base colour. All share the modal background (sb_bg).
PAIR_PASTEL_GREEN = 18   # +ins  / Added file status
PAIR_PASTEL_RED = 19     # -del  / Deleted file status / conflict
PAIR_PASTEL_YELLOW = 20  # Modified status / commit sha
PAIR_PASTEL_BLUE = 21    # Renamed status / commit relative-time
# Active variants for the review pane's focused state.
PAIR_PASTEL_GREEN_ACTIVE = 26
PAIR_PASTEL_RED_ACTIVE = 27
PAIR_PASTEL_YELLOW_ACTIVE = 28
PAIR_PASTEL_BLUE_ACTIVE = 29


def init_colors() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(PAIR_BRANCH, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_DIRTY, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_TOGGLE_ON, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_TOGGLE_OFF, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_HINT, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_ERR, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_HEADER, curses.COLOR_MAGENTA, bg)
    if curses.COLORS >= 256:
        # orchid2 — bright pink-magenta, one shade above bold magenta.
        curses.init_pair(PAIR_HEADER_ACTIVE, 213, bg)
    else:
        curses.init_pair(PAIR_HEADER_ACTIVE, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_AHEAD, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_BEHIND, curses.COLOR_MAGENTA, bg)
    sb_bg = curses.COLOR_BLACK
    # xterm-256 grayscale ramp lives at indices 232 (black) → 255
    # (white). 236 lands at RGB ~48/48/48 — noticeably lighter than
    # plain black but still very dark, giving the active panel a
    # subtle "one shade up" feel without the artefacts that index 8
    # produced on terminals with reflowed palettes. On <256-colour
    # terminals there's no reliable subtle-grey slot, so fall back
    # to plain black (active state still signalled by the cyan
    # header accent).
    sb_bg_active = 236 if curses.COLORS >= 256 else sb_bg
    curses.init_pair(PAIR_SB_FG, curses.COLOR_WHITE, sb_bg)
    if curses.COLORS >= 256:
        # xterm grey (240) — dimmer than COLOR_WHITE + A_DIM, gives a
        # clearly "unavailable" look on the toolbar without mistaking
        # for a missing/blank button.
        curses.init_pair(PAIR_SB_FG_DISABLED, 240, sb_bg)
    else:
        curses.init_pair(PAIR_SB_FG_DISABLED, curses.COLOR_WHITE, sb_bg)
    curses.init_pair(PAIR_SB_CYAN, curses.COLOR_CYAN, sb_bg)
    curses.init_pair(PAIR_SB_OK, curses.COLOR_GREEN, sb_bg)
    curses.init_pair(PAIR_SB_ERR, curses.COLOR_RED, sb_bg)
    curses.init_pair(PAIR_SB_WARN, curses.COLOR_YELLOW, sb_bg)
    curses.init_pair(PAIR_SB_FG_ACTIVE, curses.COLOR_WHITE, sb_bg_active)
    curses.init_pair(PAIR_SB_CYAN_ACTIVE, curses.COLOR_CYAN, sb_bg_active)
    curses.init_pair(PAIR_SB_OK_ACTIVE, curses.COLOR_GREEN, sb_bg_active)
    curses.init_pair(PAIR_SB_ERR_ACTIVE, curses.COLOR_RED, sb_bg_active)
    curses.init_pair(PAIR_SB_WARN_ACTIVE, curses.COLOR_YELLOW, sb_bg_active)
    # Pastel pairs — pulled from the xterm-256 soft-tone band where
    # available (151/174/179/109 are gentle on the eye against a dark
    # bg without the screaming primaries of the 16-colour set).
    if curses.COLORS >= 256:
        curses.init_pair(PAIR_PASTEL_GREEN, 151, sb_bg)
        curses.init_pair(PAIR_PASTEL_RED, 174, sb_bg)
        curses.init_pair(PAIR_PASTEL_YELLOW, 179, sb_bg)
        curses.init_pair(PAIR_PASTEL_BLUE, 109, sb_bg)
        curses.init_pair(PAIR_PASTEL_GREEN_ACTIVE, 151, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_RED_ACTIVE, 174, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_YELLOW_ACTIVE, 179, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_BLUE_ACTIVE, 109, sb_bg_active)
    else:
        curses.init_pair(PAIR_PASTEL_GREEN, curses.COLOR_GREEN, sb_bg)
        curses.init_pair(PAIR_PASTEL_RED, curses.COLOR_RED, sb_bg)
        curses.init_pair(PAIR_PASTEL_YELLOW, curses.COLOR_YELLOW, sb_bg)
        curses.init_pair(PAIR_PASTEL_BLUE, curses.COLOR_CYAN, sb_bg)
        curses.init_pair(PAIR_PASTEL_GREEN_ACTIVE, curses.COLOR_GREEN, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_RED_ACTIVE, curses.COLOR_RED, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_YELLOW_ACTIVE, curses.COLOR_YELLOW, sb_bg_active)
        curses.init_pair(PAIR_PASTEL_BLUE_ACTIVE, curses.COLOR_CYAN, sb_bg_active)


def _state_color(*, error: str, merging: bool, ahead: int, behind: int,
                 dirty: bool, upstream: Optional[str]) -> Tuple[str, int]:
    """Shared core for the state dot precedence used by both top-level
    repos and submodule child rows. Caller passes the discrete state
    flags so we don't have to special-case the two row shapes."""
    if error:
        return "error", curses.color_pair(PAIR_ERR)
    if merging:
        return "merging", curses.color_pair(PAIR_ERR)
    if ahead > 0 and behind > 0:
        return "diverged", curses.color_pair(PAIR_ERR)
    if dirty:
        return "dirty", curses.color_pair(PAIR_DIRTY)
    if behind > 0:
        return "behind", curses.color_pair(PAIR_BEHIND)
    if ahead > 0:
        return "ahead", curses.color_pair(PAIR_AHEAD)
    if not upstream:
        return "no upstream", curses.A_DIM
    return "clean", curses.color_pair(PAIR_OK)


def state_color(repo: Repo) -> Tuple[str, int]:
    """Return (state-label, attr) for the dot showing this repo's state."""
    return _state_color(
        error=repo.error, merging=repo.merging,
        ahead=repo.ahead, behind=repo.behind,
        dirty=repo.is_dirty, upstream=repo.upstream,
    )


def child_state_color(child: ChildRef) -> Tuple[str, int]:
    """Same dot palette as `state_color`, but reading from a ChildRef
    (which exposes the same fields independently of the canonical Repo)."""
    return _state_color(
        error=child.error, merging=child.merging,
        ahead=child.ahead, behind=child.behind,
        dirty=child.dirty, upstream=child.upstream,
    )
