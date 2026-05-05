"""Type-a-name modal for the action menu's "save HEAD to new branch…"
item. Used to recover a detached HEAD by parking its commits on a
fresh branch — `git checkout -b <name>` only writes a new ref, so it's
fully cardinal-rule safe (no commits orphaned)."""
from __future__ import annotations

import curses

from models import BranchNamePrompt, State
from git_ops import git
from workers import kick_off_action

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
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
_VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def _hints(prompt: BranchNamePrompt) -> list:
    name = prompt.typed or prompt.default_name
    if not name or name.startswith("-"):
        return [Hint(KEY_ESC, "back")]
    return [
        Hint("a-z, 0-9, /-_.", "type name"),
        Hint(KEY_BACKSPACE, "delete char"),
        Hint(KEY_ENTER, f"create branch {name} at HEAD"),
        Hint(KEY_ESC, "back"),
    ]


def open_branch_name_prompt(state: State) -> None:
    """Install the prompt onto state. Default branch name is
    `idlegit/wip-<sha8>` so even a panicked Enter is recoverable —
    every detached-HEAD save lands on a predictable namespace the
    user can grep / clean up later (`git branch -D idlegit/wip-*`)."""
    menu = state.action_menu
    if menu is None:
        return
    rc, head_out, _ = git(menu.target_path, ["rev-parse", "HEAD"])
    sha = head_out.strip() if rc == 0 else ""
    sha8 = sha[:8] if sha else "head"
    state.branch_name_prompt = BranchNamePrompt(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        default_name=f"idlegit/wip-{sha8}",
        head_sha=sha,
    )


def _title_lines(prompt: BranchNamePrompt, inner_w: int) -> "list[str]":
    return wrap_label_value("Save HEAD to new branch",
                            prompt.target_label, inner_w)


def draw_branch_name_prompt(stdscr, state: State, sidebar_x: int) -> None:
    prompt = state.branch_name_prompt
    if prompt is None:
        return

    sb = curses.color_pair(PAIR_SB_FG)
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
        attr = (curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    sha8 = prompt.head_sha[:8] if prompt.head_sha else "(unknown)"
    safe_addstr(stdscr, line, inner_x,
                end_truncate(f"detached at {sha8}", inner_w),
                sb | curses.A_DIM)
    line += 1

    safe_addstr(stdscr, line, inner_x,
                end_truncate("New branch name:", inner_w),
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
    prompt = state.branch_name_prompt
    if prompt is None:
        return
    if key == 27:  # Esc
        state.branch_name_prompt = None
        return
    if key in (10, 13, curses.KEY_ENTER):
        name = prompt.typed.strip() or prompt.default_name
        if not name or name.startswith("-"):
            return
        kick_off_action(
            state, "branch_from_head",
            target_label=prompt.target_label,
            target_path=prompt.target_path,
            target_repo=prompt.target_repo,
            target_parent=prompt.target_parent,
            branch_arg=name,
        )
        state.branch_name_prompt = None
        state.action_menu = None
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        prompt.typed = prompt.typed[:-1]
        return
    # Printable char in the allowlist gets appended.
    if 32 <= key < 127:
        ch = chr(key)
        if not prompt.typed and ch == "-":
            return
        if ch in _VALID_NAME_CHARS:
            prompt.typed += ch
