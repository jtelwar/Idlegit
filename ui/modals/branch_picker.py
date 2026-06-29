"""Branch-switch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.pickers import BranchPicker
from features.branch_picker.actions import (
    handle_branch_picker_key as handle_branch_picker_key_action,
)
from features.branch_picker.projection import (
    has_create_row,
    picker_branches,
    title_label,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr, wrap_label_value,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


# Outer padding mirroring align_heads_prompt — uniform across every
# repo-name-bearing modal.
_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 60

def _spinner_glyph(state: State) -> str:
    from ..sidebar import SPINNER_FRAMES
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _hints(state: State, picker: BranchPicker) -> list:
    """Footer hints in scope for the branch picker. Empty branch list
    leaves only Esc — there's literally no row to navigate to or
    check out."""
    branches, current, loading = picker_branches(state, picker)
    if loading:
        return [Hint(KEY_ESC, "back")]
    # Create-row focus: typing-style hints, with Enter only promising
    # a creation when the user has typed a usable name.
    if has_create_row(picker) and picker.selected == -1:
        name = picker.create_typed
        hints = [
            Hint("a-z, 0-9, /-_.", "type name"),
            Hint(KEY_BACKSPACE, "delete char"),
        ]
        if name and not name.startswith("-"):
            hints.append(Hint(
                KEY_ENTER, f"create + checkout {name}"))
        hints.append(Hint(KEY_ESC, "back"))
        return hints
    if not branches:
        if picker.mode == "set_upstream":
            return [
                Hint("(no remote-tracking refs — fetch first)", ""),
                Hint(KEY_ESC, "back"),
            ]
        return [Hint(KEY_ESC, "back")]
    hints = [Hint(KEY_UP_DOWN, "select")]
    selected_branch = branches[picker.selected]
    if picker.mode in ("merge", "safe_merge"):
        if selected_branch == current:
            hints.append(Hint(KEY_ENTER, "can't merge a branch into itself"))
        elif picker.mode == "safe_merge":
            hints.append(Hint(
                KEY_ENTER,
                f"safe-merge {selected_branch} into {current}"))
        else:
            hints.append(Hint(
                KEY_ENTER,
                f"merge {selected_branch} into {current} "
                "(safe-merge if it conflicts)"))
    elif picker.mode == "set_upstream":
        target = current or "current branch"
        hints.append(Hint(
            KEY_ENTER, f"set {target} upstream → {selected_branch}"))
    else:
        if selected_branch == current:
            # Enter on the row already-checked-out is a no-op; describe it
            # accurately rather than promising a "checkout" that won't run.
            hints.append(Hint(KEY_ENTER, "stay (already checked out)"))
        else:
            hints.append(Hint(KEY_ENTER, f"checkout {selected_branch}"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _title_lines(picker: BranchPicker, inner_w: int) -> "list[str]":
    """Title-area lines. `target_label` is the full repo display name
    (no `task_repo_label` truncation) — long names get their own
    indented line via wrap_label_value, end-truncated only when even
    that doesn't fit."""
    return wrap_label_value(title_label(picker), picker.target_label, inner_w)


def _draw_create_row(stdscr, y: int, inner_x: int, inner_w: int,
                     picker: BranchPicker, focused: bool, sb: int) -> None:
    """Render the 'Create new branch' input row. When unfocused, shows
    "+ Create new branch" as dim placeholder text. When focused, swaps
    to a reverse-video input cell — same field treatment
    branch_name_prompt uses for its name field."""
    if focused:
        # Typed text + trailing `_` cursor; empty buffer still shows the
        # cursor so the user sees the insertion point.
        field_text = f"+ {picker.create_typed}_"
        attr = sb | curses.A_REVERSE
    else:
        field_text = "+ Create new branch"
        attr = sb | curses.A_DIM
    safe_addstr(stdscr, y, inner_x,
                end_truncate(field_text, inner_w).ljust(inner_w), attr)


def draw_branch_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    branches, current, loading = picker_branches(state, picker)
    has_create = has_create_row(picker) and not loading

    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    create_rows = 1 if has_create else 0
    n_branches = 1 if loading else max(1, len(branches))
    blank_after_title = 1
    blank_after_list = 1
    hint_rows = 1

    desired_h = (
        _PAD_TOP
        + len(title_rows)
        + blank_after_title
        + create_rows
        + n_branches
        + blank_after_list
        + hint_rows
        + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = _title_lines(picker, inner_w)

    fixed_rows = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + create_rows
        + blank_after_list + hint_rows + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)

    # Title.
    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    # Create row sits above the (potentially scrolled) branches list,
    # outside its scroll math — always visible at the top.
    if has_create:
        _draw_create_row(stdscr, line, inner_x, inner_w,
                         picker, picker.selected == -1, sb)
        line += 1

    if loading:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(
                        f"  {_spinner_glyph(state)} loading branches…",
                        inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(state, picker), attr=sb | curses.A_DIM)
        return

    if not branches:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no branches found)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(state, picker), attr=sb | curses.A_DIM)
        return

    n = len(branches)
    # Scroll only against the branches list. When the create row owns
    # focus (selected = -1), pin scroll to row 0 so the branches don't
    # jump just because focus is above the list.
    scroll_anchor = max(0, picker.selected)
    picker.scroll = clamp_scroll(
        scroll_anchor, picker.scroll, n, visible_rows)

    end = min(n, picker.scroll + visible_rows)
    for slot in range(visible_rows):
        idx = picker.scroll + slot
        if idx >= n:
            break
        row_y = line + slot

        if slot == 0 and picker.scroll > 0:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 picker.scroll, "up", sb | curses.A_DIM)
            continue
        if slot == visible_rows - 1 and end < n:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 n - end + 1, "down", sb | curses.A_DIM)
            continue

        name = branches[idx]
        focused = (idx == picker.selected)
        is_current = (name == current)
        marker = "* " if is_current else "  "
        prefix = "→ " if focused else marker
        text = end_truncate(prefix + name, inner_w).ljust(inner_w)
        attr = sb | curses.A_REVERSE if focused else sb
        if is_current and not focused:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(state, picker), attr=sb | curses.A_DIM)


def handle_branch_picker_key(state: State, key: int) -> None:
    handle_branch_picker_key_action(state, key)
