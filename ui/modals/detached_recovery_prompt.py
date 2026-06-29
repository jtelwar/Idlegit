"""Detached-HEAD recovery prompt — pops from the smart-sync /
commit pipelines when they need permission to fast-forward a branch
to HEAD's commit before continuing. Cardinal-rule guarantee: this
modal only opens when the FF is already known to be safe (target
branch is an ancestor of HEAD). The user just confirms or cancels."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.prompts import DetachedRecoveryPrompt

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import KEY_ENTER, KEY_ESC, Hint, render_hints


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70


def _hints(prompt: DetachedRecoveryPrompt) -> list:
    if prompt.can_ff:
        return [
            Hint(KEY_ENTER, f"fast-forward {prompt.target_branch} to HEAD"),
            Hint(KEY_ESC, "cancel"),
        ]
    return [Hint(KEY_ESC, "cancel")]


def open_detached_recovery_prompt(state: State,
                                  prompt: DetachedRecoveryPrompt) -> None:
    """Install the prompt onto state. The caller (a worker thread)
    then blocks on `prompt.result_event` until the user picks."""
    state.detached_recovery_prompt = prompt


def draw_detached_recovery_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.detached_recovery_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = wrap_label_value("Recover detached HEAD",
                                  prompt.target_label, target_inner_w)

    # Body rows: detached-at line + N-commits line + blank + question
    # (1 line when can_ff, 2 lines when explaining a refusal).
    detail_rows = 2 if prompt.can_ff else 3
    body_rows = 1 + 1 + 1 + detail_rows  # detached / N commits / blank / question
    blank_after_title = 1
    blank_before_hint = 1
    hint_rows = 1

    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title + body_rows
        + blank_before_hint + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = wrap_label_value("Recover detached HEAD",
                                      prompt.target_label, inner_w)

    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    sha8 = prompt.head_sha[:8] if prompt.head_sha else "(unknown)"
    safe_addstr(stdscr, line, inner_x,
                end_truncate(f"HEAD: detached at {sha8}", inner_w),
                sb | curses.A_DIM)
    line += 1

    n = prompt.n_extra
    plural = "" if n == 1 else "s"
    safe_addstr(
        stdscr, line, inner_x,
        end_truncate(
            f"{n} commit{plural} not on {prompt.target_branch}", inner_w),
        sb | curses.A_DIM)
    line += 1

    line += 1  # blank between detail and the question

    if prompt.can_ff:
        # Safe FF: target is ancestor of HEAD → no commits orphaned.
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(
                f"Fast-forward '{prompt.target_branch}' to HEAD?", inner_w),
            sb | curses.A_BOLD)
        line += 1
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(
                "  (target is an ancestor of HEAD — safe)", inner_w),
            sb | curses.A_DIM)
        line += 1
    else:
        # Divergent histories: can't FF either direction. Tell the user
        # what to do manually instead.
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(
                f"HEAD and '{prompt.target_branch}' have diverged.", inner_w),
            sb | curses.A_DIM)
        line += 1
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate("Auto-recovery isn't safe here. Use the action",
                         inner_w),
            sb | curses.A_DIM)
        line += 1
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(
                "menu's 'save HEAD to new branch…' to park HEAD's commits.",
                inner_w),
            sb | curses.A_DIM)
        line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(prompt), attr=sb | curses.A_DIM)


def handle_detached_recovery_prompt_key(state: State, key: int) -> None:
    prompt = state.detached_recovery_prompt
    if prompt is None:
        return
    if key == 27:  # Esc — cancel
        prompt.chosen_action = "cancel"
        prompt.result_event.set()
        state.detached_recovery_prompt = None
        return
    if key in (10, 13, curses.KEY_ENTER):
        if prompt.can_ff:
            prompt.chosen_action = "ff"
        else:
            # FF refused at modal-open time — Enter is functionally
            # the same as Esc (acknowledge + bail).
            prompt.chosen_action = "cancel"
        prompt.result_event.set()
        state.detached_recovery_prompt = None


__all__ = [
    "open_detached_recovery_prompt",
    "draw_detached_recovery_prompt",
    "handle_detached_recovery_prompt_key",
]
