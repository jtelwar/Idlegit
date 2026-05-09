"""Color pairs + init_colors + the per-row state-colour helpers.

This module is leaf-level: it knows about the palette and the
state-precedence rules, but nothing about Repo or ChildRef shapes
beyond what `_state_color` accepts as keyword args."""
from __future__ import annotations

import curses
from typing import Optional, Tuple

from core.models import ChildRef, Repo


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
# Mid-grey foreground for inline hint-text labels (e.g. the per-row
# explainer above the workspace-menu footer). Sits between
# `PAIR_SB_FG | A_DIM` (dim grey, used for the key labels in the
# footer) and `PAIR_SB_FG` (full white) so the explainer reads as
# secondary text without falling back into the same shade as the
# footer keys. 256-colour grey 250; falls back to PAIR_SB_FG on
# 8/16-colour terminals where no usable mid-tone exists.
PAIR_SB_FG_HINT_TEXT = 32
# Sidebar pairs share a darker bg so the panel reads as a distinct surface.
PAIR_SB_FG = 12
PAIR_SB_CYAN = 13
PAIR_SB_OK = 14
PAIR_SB_ERR = 15
PAIR_SB_WARN = 16
# Magenta on sb_bg — used inside the sidebar by surfaces that need
# the magenta accent (ahead/behind state dots) without falling back
# to PAIR_BEHIND whose default-bg would punch a hole through the
# panel.
PAIR_SB_MAGENTA = 33

# ---------- Dialog (modal) pairs -----------------------------------------
#
# Modal dialogs are visually a separate concept from the sidebar:
# the sidebar is always-on chrome, dialogs pop up on demand. Giving
# them their own pair set keeps the two namespaces clean — a future
# tweak to the dialog palette doesn't bleed into the sidebar (and
# vice versa). They currently share colours with the sidebar but
# have independent `dlg_bg` / `dlg_bg_active` slots in init_colors,
# so divergence is a one-variable change.
PAIR_DLG_FG = 34
PAIR_DLG_CYAN = 35
PAIR_DLG_OK = 36
PAIR_DLG_ERR = 37
PAIR_DLG_WARN = 38
PAIR_DLG_MAGENTA = 39
PAIR_DLG_FG_DISABLED = 40
PAIR_DLG_FG_HINT_TEXT = 41
PAIR_DLG_FG_ACTIVE = 42
PAIR_DLG_CYAN_ACTIVE = 43
PAIR_DLG_OK_ACTIVE = 44
PAIR_DLG_ERR_ACTIVE = 45
PAIR_DLG_WARN_ACTIVE = 46
PAIR_DLG_PASTEL_GREEN = 47
PAIR_DLG_PASTEL_RED = 48
PAIR_DLG_PASTEL_YELLOW = 49
PAIR_DLG_PASTEL_BLUE = 50
PAIR_DLG_PASTEL_GREEN_ACTIVE = 51
PAIR_DLG_PASTEL_RED_ACTIVE = 52
PAIR_DLG_PASTEL_YELLOW_ACTIVE = 53
PAIR_DLG_PASTEL_BLUE_ACTIVE = 54
# Thin border drawn around every modal. Foreground is a shade
# lighter than dlg_bg (xterm 237 on 256-colour terminals; falls
# back to white-dim on 8/16-colour). Background is the surrounding
# terminal default — the border cells deliberately sit OUTSIDE the
# panel fill so the dlg_bg doesn't extend past the box.
PAIR_DLG_BORDER = 55
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
    curses.init_pair(PAIR_AHEAD, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_BEHIND, curses.COLOR_MAGENTA, bg)
    # Dialog / sidebar panel background. ANSI `COLOR_BLACK` (= 0)
    # is rendered by the terminal theme — most modern themes paint
    # it as a dark grey rather than pure #000000, which is the
    # "lighter grey panel" appearance idlegit has historically had.
    # The active variant bumps to xterm 236 (~RGB 48/48/48) so a
    # focused panel reads as "raised" against the resting panel bg
    # without going as far as inverse video; on <256-colour
    # terminals it collapses back to COLOR_BLACK and focus is
    # carried purely by the cyan accent.
    sb_bg = curses.COLOR_BLACK
    sb_bg_active = 236 if curses.COLORS >= 256 else sb_bg
    curses.init_pair(PAIR_SB_FG, curses.COLOR_WHITE, sb_bg)
    if curses.COLORS >= 256:
        # xterm grey (240) — dimmer than COLOR_WHITE + A_DIM, gives a
        # clearly "unavailable" look on the toolbar without mistaking
        # for a missing/blank button.
        curses.init_pair(PAIR_SB_FG_DISABLED, 240, sb_bg)
        # xterm grey (250) — lighter than DIM, darker than full white.
        # Reads as "secondary text" without colliding with either the
        # dim hints footer (DIM grey) or the bright body labels.
        curses.init_pair(PAIR_SB_FG_HINT_TEXT, 250, sb_bg)
    else:
        curses.init_pair(PAIR_SB_FG_DISABLED, curses.COLOR_WHITE, sb_bg)
        curses.init_pair(PAIR_SB_FG_HINT_TEXT, curses.COLOR_WHITE, sb_bg)
    curses.init_pair(PAIR_SB_CYAN, curses.COLOR_CYAN, sb_bg)
    curses.init_pair(PAIR_SB_OK, curses.COLOR_GREEN, sb_bg)
    curses.init_pair(PAIR_SB_ERR, curses.COLOR_RED, sb_bg)
    curses.init_pair(PAIR_SB_WARN, curses.COLOR_YELLOW, sb_bg)
    curses.init_pair(PAIR_SB_MAGENTA, curses.COLOR_MAGENTA, sb_bg)
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

    # Dialog (modal) panel background — independent of the sidebar
    # so the two surfaces can diverge later. Currently the same
    # values: COLOR_BLACK on every terminal (theme renders it as a
    # dark grey on most modern themes — the "lighter grey panel"
    # appearance the modals have historically had), bumping to
    # xterm 236 for the active variant on 256-colour terminals.
    dlg_bg = curses.COLOR_BLACK
    dlg_bg_active = 236 if curses.COLORS >= 256 else dlg_bg
    curses.init_pair(PAIR_DLG_FG, curses.COLOR_WHITE, dlg_bg)
    curses.init_pair(PAIR_DLG_CYAN, curses.COLOR_CYAN, dlg_bg)
    curses.init_pair(PAIR_DLG_OK, curses.COLOR_GREEN, dlg_bg)
    curses.init_pair(PAIR_DLG_ERR, curses.COLOR_RED, dlg_bg)
    curses.init_pair(PAIR_DLG_WARN, curses.COLOR_YELLOW, dlg_bg)
    curses.init_pair(PAIR_DLG_MAGENTA, curses.COLOR_MAGENTA, dlg_bg)
    curses.init_pair(PAIR_DLG_FG_ACTIVE, curses.COLOR_WHITE, dlg_bg_active)
    curses.init_pair(PAIR_DLG_CYAN_ACTIVE, curses.COLOR_CYAN, dlg_bg_active)
    curses.init_pair(PAIR_DLG_OK_ACTIVE, curses.COLOR_GREEN, dlg_bg_active)
    curses.init_pair(PAIR_DLG_ERR_ACTIVE, curses.COLOR_RED, dlg_bg_active)
    curses.init_pair(PAIR_DLG_WARN_ACTIVE, curses.COLOR_YELLOW, dlg_bg_active)
    if curses.COLORS >= 256:
        curses.init_pair(PAIR_DLG_FG_DISABLED, 240, dlg_bg)
        curses.init_pair(PAIR_DLG_FG_HINT_TEXT, 250, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_GREEN, 151, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_RED, 174, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_YELLOW, 179, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_BLUE, 109, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_GREEN_ACTIVE, 151, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_RED_ACTIVE, 174, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_YELLOW_ACTIVE, 179, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_BLUE_ACTIVE, 109, dlg_bg_active)
    else:
        curses.init_pair(PAIR_DLG_FG_DISABLED, curses.COLOR_WHITE, dlg_bg)
        curses.init_pair(PAIR_DLG_FG_HINT_TEXT, curses.COLOR_WHITE, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_GREEN, curses.COLOR_GREEN, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_RED, curses.COLOR_RED, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_YELLOW, curses.COLOR_YELLOW, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_BLUE, curses.COLOR_CYAN, dlg_bg)
        curses.init_pair(PAIR_DLG_PASTEL_GREEN_ACTIVE, curses.COLOR_GREEN, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_RED_ACTIVE, curses.COLOR_RED, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_YELLOW_ACTIVE, curses.COLOR_YELLOW, dlg_bg_active)
        curses.init_pair(PAIR_DLG_PASTEL_BLUE_ACTIVE, curses.COLOR_CYAN, dlg_bg_active)
    # Modal border foreground: a shade lighter than dlg_bg so the box
    # reads as a soft outline rather than the screaming-white the
    # PAIR_DLG_FG_HINT_TEXT (xterm 250) gave. xterm 237 (~RGB 58/58/58)
    # sits a touch above dlg_bg / dlg_bg_active. Background is dlg_bg
    # itself — box-drawing chars only fill part of their cell, so the
    # cell's bg shows around the glyph; matching it to dlg_bg keeps
    # the border row reading as part of the dialog rather than a
    # black ring tracing its edge.
    if curses.COLORS >= 256:
        curses.init_pair(PAIR_DLG_BORDER, 237, dlg_bg)
    else:
        curses.init_pair(PAIR_DLG_BORDER, curses.COLOR_WHITE, dlg_bg)


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
