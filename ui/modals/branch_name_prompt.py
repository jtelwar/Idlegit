"""Type-a-name modal for the action menu's "save HEAD to new branch…"
item. Used to recover a detached HEAD by parking its commits on a
fresh branch — `git checkout -b <name>` only writes a new ref, so it's
fully cardinal-rule safe (no commits orphaned)."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.prompts import BranchNamePrompt
from features.branch_name_prompt.actions import (
    handle_branch_name_prompt_key as _handle_branch_name_prompt_key,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import KEY_BACKSPACE, KEY_ENTER, KEY_ESC, Hint, render_hints


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 70


# Branches in git can contain almost anything except a few control
# chars + the slash patterns ".." / "@{" — keeping the picker's
# allowlist conservative (alphanum + a few punctuation marks the user
# is likely to want for ergonomic names like "wip/foo-1"). Anything
# the user types that doesn't match drops on the floor; pasted invalid
# chars get filtered out.
def _hints(prompt: BranchNamePrompt) -> list:
    name = prompt.typed or prompt.default_name
    if not name or name.startswith("-"):
        return [Hint(KEY_ESC, "back")]
    if prompt.mode == "rename":
        action = (f"rename {prompt.current_branch} → {name}"
                  if prompt.current_branch and name != prompt.current_branch
                  else f"rename to {name}")
    else:
        action = f"create branch {name} at HEAD"
    return [
        Hint("a-z, 0-9, /-_.", "type name"),
        Hint(KEY_BACKSPACE, "delete char"),
        Hint(KEY_ENTER, action),
        Hint(KEY_ESC, "back"),
    ]


def _title_lines(prompt: BranchNamePrompt, inner_w: int) -> "list[str]":
    label = ("Rename branch" if prompt.mode == "rename"
             else "Save HEAD to new branch")
    return wrap_label_value(label, prompt.target_label, inner_w)


def draw_branch_name_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.branch_name_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(prompt, target_inner_w)

    # Body: detached-at line + blank + prompt label + input field.
    body_rows = 4
    blank_after_title = 1
    hint_rows = 1
    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title + body_rows
        + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = _title_lines(prompt, inner_w)

    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    if prompt.mode == "rename":
        subtitle = (f"current: {prompt.current_branch}"
                    if prompt.current_branch else "current: (unknown)")
    else:
        sha8 = prompt.head_sha[:8] if prompt.head_sha else "(unknown)"
        subtitle = f"detached at {sha8}"
    safe_addstr(stdscr, line, inner_x,
                end_truncate(subtitle, inner_w),
                sb | curses.A_DIM)
    line += 1

    label = ("Rename to:" if prompt.mode == "rename"
             else "New branch name:")
    safe_addstr(stdscr, line, inner_x,
                end_truncate(label, inner_w),
                sb | curses.A_DIM)
    line += 1

    # Render the field. When the user hasn't typed yet, show the
    # default in dim — Enter still uses it as the actual branch name.
    if prompt.typed:
        # Append a `_` cursor so the user sees their insertion point.
        field_text = f" {prompt.typed}_ "
        attr = sb | curses.A_REVERSE
    else:
        field_text = f" {prompt.default_name} "
        attr = sb | curses.A_REVERSE | curses.A_DIM
    safe_addstr(stdscr, line, inner_x,
                end_truncate(field_text, inner_w).ljust(inner_w), attr)
    line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(prompt), attr=sb | curses.A_DIM)


def handle_branch_name_prompt_key(state: State, key: int) -> None:
    _handle_branch_name_prompt_key(state, key)
