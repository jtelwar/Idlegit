"""Smart-sync's "winner is detached" modal — appears when a canonical
submodule's chosen winner has no current branch and `align_heads` is on,
since we can't push without first switching to one. The user picks a
branch (or cancels); the worker thread that triggered the modal is
blocked on the prompt's `result_event` until that happens."""
from __future__ import annotations

import curses
from typing import List, Tuple

from core.models import AlignHeadsPrompt, State

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr, wrap_label_value,
)
from ..hints import KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints

# Outer padding inside the modal box: blank row above + below content,
# 2 cells of left/right margin. Modals reference repos by name and a
# tight box reads as cluttered, so the padding is uniform across every
# modal that follows this layout pattern.
_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2

# Target modal width; modal_geometry caps this against the terminal
# width so it shrinks gracefully on narrow windows.
_MODAL_W = 70


def _hints(prompt: AlignHeadsPrompt) -> list:
    """Footer hints for the align-heads branch picker. The Enter action
    names the branch we'll push the winner's commits to so the user
    sees exactly what's about to happen."""
    if not prompt.branches:
        return [Hint(KEY_ENTER, "skip"), Hint(KEY_ESC, "skip")]
    branch = prompt.branches[prompt.selected]
    return [
        Hint(KEY_UP_DOWN, "select branch"),
        Hint(KEY_ENTER, f"push winner to {branch}"),
        Hint(KEY_ESC, "skip this canonical"),
    ]


def open_align_heads_prompt(state: State, prompt: AlignHeadsPrompt) -> None:
    """Install the prompt onto state. Called from the smart-sync worker;
    after this returns, the worker blocks on `prompt.result_event`."""
    state.align_heads_prompt = prompt


# Header section structure: a list of (text, attr_kind) pairs. Kind
# is one of "title", "subline", "subline_dim". The draw routine
# resolves the kind to a curses attr at render time so the layout
# logic stays attribute-agnostic and easier to test.
_HeaderRow = Tuple[str, str]


def _build_header(prompt: AlignHeadsPrompt, inner_w: int) -> List[_HeaderRow]:
    """Compose the header rows for the modal. Repo names go through
    `wrap_label_value` so each name gets its own line (with end-only
    truncation if it still doesn't fit) — never middle-truncated."""
    rows: List[_HeaderRow] = []
    rows.append(("Align heads", "title"))
    rows.append(("", "blank"))
    for line in wrap_label_value("Submodule",
                                 prompt.canonical_name, inner_w):
        rows.append((line, "subline"))
    rows.append(("", "blank"))
    sha8 = prompt.winner_sha[:8] if prompt.winner_sha else "(unknown)"
    rows.append((f"Winner is detached at {sha8}", "subline"))
    if prompt.winner_parent_name:
        for line in wrap_label_value("in", prompt.winner_parent_name,
                                     inner_w):
            rows.append((line, "subline_dim"))
    rows.append(("", "blank"))
    rows.append(("Push these changes to which branch?", "subline_dim"))
    return rows


def _adjust_scroll(prompt: AlignHeadsPrompt, visible_rows: int) -> None:
    """Clamp `prompt.scroll` so the selected branch is visible. Also
    clamps the bottom edge so the list never scrolls past the last
    branch (no trailing empty rows under the cursor)."""
    prompt.scroll = clamp_scroll(prompt.selected, prompt.scroll,
                                 len(prompt.branches), visible_rows)


def draw_align_heads_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.align_heads_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    term_h, _ = stdscr.getmaxyx()

    # Pre-build header rows against the eventual inner width. The
    # modal_geometry call caps box_w against terminal width, so we
    # use _MODAL_W as the upper bound and account for left/right pad.
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    header_rows = _build_header(prompt, target_inner_w)

    branch_rows = max(1, len(prompt.branches))
    blank_before_branches = 1
    blank_after_branches = 1
    hint_rows = 1

    desired_h = (
        _PAD_TOP
        + len(header_rows)
        + blank_before_branches
        + branch_rows
        + blank_after_branches
        + hint_rows
        + _PAD_BOTTOM
    )

    # modal_geometry caps height against the terminal — when desired_h
    # exceeds what the terminal can display, the modal shrinks and the
    # branch list scrolls inside whatever rows are left over.
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    # Re-derive header for the actual inner_w in case the modal had
    # to shrink horizontally. Cheap; layout logic only runs once per
    # frame.
    if inner_w != target_inner_w:
        header_rows = _build_header(prompt, inner_w)

    # How many branch rows actually fit?
    fixed_rows = (
        _PAD_TOP
        + len(header_rows)
        + blank_before_branches
        + blank_after_branches
        + hint_rows
        + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)

    _adjust_scroll(prompt, visible_rows)

    line = y + _PAD_TOP

    for text, kind in header_rows:
        if kind == "blank":
            line += 1
            continue
        if kind == "title":
            attr = curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
        elif kind == "subline_dim":
            attr = sb | curses.A_DIM
        else:  # "subline"
            attr = sb
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1

    line += blank_before_branches

    # Branch list (scrollable). Above/below indicators replace the
    # first/last row when there's content offscreen, so the modal
    # always tells the user "there's more" without expanding.
    if not prompt.branches:
        safe_addstr(stdscr, line, inner_x + 2,
                    end_truncate("(no local branches available)", inner_w - 2),
                    sb | curses.A_DIM)
    else:
        n = len(prompt.branches)
        end = min(n, prompt.scroll + visible_rows)
        for slot in range(visible_rows):
            i = prompt.scroll + slot
            if i >= n:
                break
            row_y = line + slot

            # Replace the topmost visible row with "↑ N more" when we're
            # scrolled past the start; same for the bottom.
            if slot == 0 and prompt.scroll > 0:
                draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                     prompt.scroll, "up", sb | curses.A_DIM)
                continue
            if slot == visible_rows - 1 and end < n:
                draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                     n - end + 1, "down",
                                     sb | curses.A_DIM)
                continue

            branch = prompt.branches[i]
            focused = (i == prompt.selected)
            prefix = "→ " if focused else "  "
            attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
                    if focused else sb)
            text = end_truncate(f"{prefix}{branch}", inner_w)
            safe_addstr(stdscr, row_y, inner_x, text, attr)

    line += visible_rows + blank_after_branches

    render_hints(stdscr, line, inner_x, inner_w, _hints(prompt),
                 attr=sb | curses.A_DIM)
    # Reference unused import so lint doesn't strip curses-keying
    # constants if they ever become unused — and silence the term_h
    # warning since the geometry helper consumes terminal height
    # internally.
    _ = term_h


def handle_align_heads_prompt_key(state: State, key: int) -> None:
    prompt = state.align_heads_prompt
    if prompt is None:
        return

    if key == 27:  # Esc — cancel; worker treats this as warn-skip
        prompt.chosen_branch = ""
        prompt.result_event.set()
        state.align_heads_prompt = None
        return

    if not prompt.branches:
        # Nothing to pick from — Enter or any other key just cancels.
        if key in (10, 13, curses.KEY_ENTER):
            prompt.chosen_branch = ""
            prompt.result_event.set()
            state.align_heads_prompt = None
        return

    if key == curses.KEY_UP:
        prompt.selected = max(0, prompt.selected - 1)
        return
    if key == curses.KEY_DOWN:
        prompt.selected = min(len(prompt.branches) - 1, prompt.selected + 1)
        return

    if key in (10, 13, curses.KEY_ENTER):
        prompt.chosen_branch = prompt.branches[prompt.selected]
        prompt.result_event.set()
        state.align_heads_prompt = None


__all__ = [
    "open_align_heads_prompt",
    "draw_align_heads_prompt",
    "handle_align_heads_prompt_key",
]
