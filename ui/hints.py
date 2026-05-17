"""Footer hint primitives. One Hint = one keybinding shown in the
status row, with the exact action it performs in the current context.
Each screen / modal exports a small `_hints(state)` function that
returns the list of hints currently in scope; `render_hints` joins them
into the status line and truncates if the terminal is narrow.

Three reasons this lives in its own module:
  - One canonical separator + cell-budget logic, instead of every modal
    rolling its own "↑/↓ ··· · Esc back" string.
  - Keeps the "what does this key do *here*?" decision next to the
    code that owns the keybinding, not buried inside a draw call.
  - Tests can call the registry function and assert the exact hint
    list produced — no curses, no draw."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .colors import (
    PAIR_BRANCH, PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_SB_CYAN, PAIR_SB_FG,
)
from .geometry import safe_addstr


def _default_key_attr(action_attr: int) -> int:
    """Pick a 'subtle cyan' attr for the keybinding glyph that pairs
    with the action attr the caller passed. Reads the colour pair
    embedded in `action_attr` to decide which background we're
    rendering on: dialogs get DLG_CYAN, the sidebar gets SB_CYAN,
    the main panel's default-bg gets the regular branch CYAN. All
    come back DIM so they read as muted next to the bright child-
    branch colour (which uses the same pairs without DIM)."""
    pair_num = curses.pair_number(action_attr) if action_attr else 0
    if pair_num == PAIR_DLG_FG:
        base_pair = PAIR_DLG_CYAN
    elif pair_num == PAIR_SB_FG:
        base_pair = PAIR_SB_CYAN
    else:
        base_pair = PAIR_BRANCH
    return curses.color_pair(base_pair) | curses.A_DIM


# Common key-glyph strings, kept consistent so the same physical key is
# spelled the same way wherever it shows up. Modals are free to define
# their own (e.g. "Type") for prose-style hints; these are just the
# usual suspects.
KEY_UP_DOWN = "↑/↓"
KEY_LEFT_RIGHT = "←/→"
KEY_ENTER = "Enter"
KEY_TAB = "Tab"
KEY_SHIFT_TAB = "Shift+Tab"
KEY_ESC = "Esc"
KEY_SPACE = "Space"
KEY_BACKSPACE = "Backspace"
KEY_HOME = "Home"
KEY_END = "End"
KEY_CTRL_K = "Ctrl+K"
KEY_CTRL_P = "Ctrl+P"
KEY_CTRL_R = "Ctrl+R"
KEY_CTRL_S = "Ctrl+S"
KEY_LEFT = "←"
KEY_RIGHT = "→"
KEY_UP = "↑"
KEY_DOWN = "↓"

# How much of a hint is mandatory before truncation. We try to keep
# the action text intact and only drop trailing hints once the line
# would overflow the available width.
SEPARATOR = " · "


@dataclass(frozen=True)
class Hint:
    """One footer-status entry. `keys` is the human-readable glyph
    string ("↑/↓", "Enter", "Ctrl+R"); `action` is the verb-phrase
    describing what that key does *right now* in this context.

    Hints are constructed by the screen / modal that owns the
    keybinding, so the description is always accurate to the current
    state — no more "Esc back" claims when Esc actually quits."""
    keys: str
    action: str


def render_hint(h: Hint) -> str:
    """Single-hint rendering — `"Enter commit"`, `"↑/↓ select"`. Kept
    as a free function so tests can inspect the produced string without
    a Hint instance round-trip."""
    return f"{h.keys} {h.action}"


def _join(hints: Iterable[Hint]) -> str:
    return SEPARATOR.join(render_hint(h) for h in hints)


def fit_hints(hints: List[Hint], max_w: int) -> str:
    """Return a status-line string that fits inside `max_w` cells.
    Drops hints from the right (lowest priority by convention — callers
    order their list with the most-important hints first) until the
    remaining ones plus a leading "…" fit. If even one hint can't fit,
    the lone hint is truncated with an ellipsis so the row stays sane.

    `max_w <= 0` returns an empty string so callers don't need to
    branch on edge-case widths."""
    if max_w <= 0 or not hints:
        return ""
    text = _join(hints)
    if len(text) <= max_w:
        return text
    # Try shedding hints from the right one by one, stopping at the
    # smallest non-empty subset that fits with a trailing ellipsis.
    for keep in range(len(hints) - 1, 0, -1):
        candidate = _join(hints[:keep]) + SEPARATOR + "…"
        if len(candidate) <= max_w:
            return candidate
    # Even one hint doesn't fit — render the first one, ellipsised.
    sole = render_hint(hints[0])
    if len(sole) <= max_w:
        return sole
    if max_w <= 1:
        return "…"[:max_w]
    return sole[: max_w - 1] + "…"


def render_hints(stdscr, y: int, x: int, max_w: int,
                 hints: List[Hint], attr: int = curses.A_DIM,
                 key_attr: "Optional[int]" = None) -> None:
    """Draw the hints at (y, x), each rendered in two segments — the
    keybinding glyph in a subtle cyan (dim, so it reads as muted vs.
    the bright child-branch cyan), then the action description in
    `attr` (dim grey by default). The separator " · " between hints
    uses the action attr too. `key_attr` overrides the auto-derived
    cyan for callers that want a different tone."""
    if max_w <= 0 or not hints:
        return
    if key_attr is None:
        key_attr = _default_key_attr(attr)

    # Reuse fit_hints' fitting logic (and its trailing-ellipsis
    # behaviour) to decide how many hints we keep, then render each
    # surviving hint as a two-segment pair instead of a flat string.
    full = _join(hints)
    if len(full) <= max_w:
        kept_n = len(hints)
        ellipsis = ""
    else:
        kept_n = 0
        for keep in range(len(hints) - 1, 0, -1):
            candidate = _join(hints[:keep]) + SEPARATOR + "…"
            if len(candidate) <= max_w:
                kept_n = keep
                break
        if kept_n == 0:
            # Even one hint doesn't fit — fall back to the legacy
            # single-attr render. The fitter's last-resort path
            # truncates the lone hint with an ellipsis; keys-vs-action
            # styling isn't worth the complexity here.
            text = fit_hints(hints, max_w)
            safe_addstr(stdscr, y, x, text, attr)
            return
        ellipsis = SEPARATOR + "…"

    cur_x = x
    for i in range(kept_n):
        h = hints[i]
        if i > 0:
            safe_addstr(stdscr, y, cur_x, SEPARATOR, attr)
            cur_x += len(SEPARATOR)
        safe_addstr(stdscr, y, cur_x, h.keys, key_attr)
        cur_x += len(h.keys)
        action_part = " " + h.action
        safe_addstr(stdscr, y, cur_x, action_part, attr)
        cur_x += len(action_part)

    if ellipsis:
        safe_addstr(stdscr, y, cur_x, ellipsis, attr)
