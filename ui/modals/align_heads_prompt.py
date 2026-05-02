"""Smart-sync's "winner is detached" modal — appears when a canonical
submodule's chosen winner has no current branch and `align_heads` is on,
since we can't push without first switching to one. The user picks a
branch (or cancels); the worker thread that triggered the modal is
blocked on the prompt's `result_event` until that happens."""
from __future__ import annotations

import curses

from models import AlignHeadsPrompt, State

from ..colors import PAIR_BRANCH, PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr
from ..hints import KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints


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


def draw_align_heads_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.align_heads_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_SB_FG)
    n_branches = max(1, len(prompt.branches))
    # blank-top + Header (title) + 1 spacer + winner-label + sha
    # + 1 spacer + prompt text + 1 spacer + branches list + 1 spacer
    # + hint + blank-bottom = 9 + n_branches + 1 (blank bottom).
    content_h = 1 + 1 + 1 + 1 + 1 + 1 + n_branches + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 70, content_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4
    line = y + 1

    title = f"Align heads — {prompt.canonical_label}"
    safe_addstr(stdscr, line, inner_x, title[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))
    line += 2

    winner_line = f"Winner: {prompt.winner_label}"
    safe_addstr(stdscr, line, inner_x, winner_line[:inner_w], sb)
    line += 1
    sha_line = f"  detached at {prompt.winner_sha[:8]}"
    safe_addstr(stdscr, line, inner_x, sha_line[:inner_w], sb | curses.A_DIM)
    line += 2

    safe_addstr(stdscr, line, inner_x,
                "Push these changes to which branch?",
                sb | curses.A_DIM)
    line += 1

    if not prompt.branches:
        safe_addstr(stdscr, line, inner_x + 2,
                    "(no local branches available)", sb | curses.A_DIM)
        line += 1
    else:
        for i, branch in enumerate(prompt.branches):
            focused = (i == prompt.selected)
            prefix = "→ " if focused else "  "
            attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                    if focused else sb)
            text = f"{prefix}{branch}"
            safe_addstr(stdscr, line, inner_x, text[:inner_w], attr)
            line += 1

    render_hints(stdscr, y + h - 2, inner_x, w - 4, _hints(prompt),
                 attr=sb | curses.A_DIM)


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
