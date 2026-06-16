"""Branch-switch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses

from core.models import BranchPicker, State
from core.git_ops import (
    is_fast_forward_merge, is_safe_ref_arg, list_branches,
    list_remote_tracking_refs,
)
from core.workers import kick_off_action, kick_off_safe_merge

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

# Same allowlist branch_name_prompt uses — keeps the "Create new
# branch" row's accepted characters consistent with the standalone
# branch-name prompt.
_VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def _has_create_row(picker: BranchPicker) -> bool:
    """The 'Create new branch' input row only makes sense in switch
    mode — you can't merge a branch that doesn't exist yet, and the
    set-upstream picker shows remote-tracking refs (no creation)."""
    return picker.mode == "switch"


def _hints(picker: BranchPicker) -> list:
    """Footer hints in scope for the branch picker. Empty branch list
    leaves only Esc — there's literally no row to navigate to or
    check out."""
    # Create-row focus: typing-style hints, with Enter only promising
    # a creation when the user has typed a usable name.
    if _has_create_row(picker) and picker.selected == -1:
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
    if not picker.branches:
        if picker.mode == "set_upstream":
            return [
                Hint("(no remote-tracking refs — fetch first)", ""),
                Hint(KEY_ESC, "back"),
            ]
        return [Hint(KEY_ESC, "back")]
    hints = [Hint(KEY_UP_DOWN, "select")]
    selected_branch = picker.branches[picker.selected]
    if picker.mode in ("merge", "safe_merge"):
        if selected_branch == picker.current:
            hints.append(Hint(KEY_ENTER, "can't merge a branch into itself"))
        elif picker.mode == "safe_merge":
            hints.append(Hint(
                KEY_ENTER,
                f"safe-merge {selected_branch} into {picker.current}"))
        else:
            hints.append(Hint(
                KEY_ENTER,
                f"merge {selected_branch} into {picker.current} "
                "(safe-merge if it conflicts)"))
    elif picker.mode == "set_upstream":
        target = picker.current or "current branch"
        hints.append(Hint(
            KEY_ENTER, f"set {target} upstream → {selected_branch}"))
    else:
        if selected_branch == picker.current:
            # Enter on the row already-checked-out is a no-op; describe it
            # accurately rather than promising a "checkout" that won't run.
            hints.append(Hint(KEY_ENTER, "stay (already checked out)"))
        else:
            hints.append(Hint(KEY_ENTER, f"checkout {selected_branch}"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def open_branch_picker(state: State, mode: str = "switch") -> None:
    menu = state.action_menu
    if menu is None:
        return
    if mode == "set_upstream":
        # Set-upstream picker lists fully-qualified remote-tracking
        # refs (origin/main, upstream/dev, …) — `git branch
        # --set-upstream-to=` wants that form, not the local-only name.
        branches = list_remote_tracking_refs(menu.target_path)
        # Track the user's current branch so the action handler can
        # pass it through, and so the title can show "current → ref".
        _, cur_out, _ = (0, "", "")
        from core.git_ops import git as _git
        rc, cur_out, _ = _git(menu.target_path, ["branch", "--show-current"])
        current = cur_out.strip() if rc == 0 else ""
        # Default the cursor to origin/<current-branch> when present —
        # by far the most common upstream choice.
        guess = f"origin/{current}" if current else ""
        initial = 0
        for i, b in enumerate(branches):
            if b == guess:
                initial = i
                break
    else:
        branches, current = list_branches(menu.target_path)
        initial = 0
        if mode in ("merge", "safe_merge"):
            # Default the cursor to the first branch that ISN'T the
            # current one — Enter on a no-op row would just bounce, so
            # picking a useful initial saves a keystroke.
            for i, b in enumerate(branches):
                if b != current:
                    initial = i
                    break
        else:
            for i, b in enumerate(branches):
                if b == current:
                    initial = i
                    break
    state.branch_picker = BranchPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        branches=branches,
        current=current,
        selected=initial,
        mode=mode,
    )


def _title_lines(picker: BranchPicker, inner_w: int) -> "list[str]":
    """Title-area lines. `target_label` is the full repo display name
    (no `task_repo_label` truncation) — long names get their own
    indented line via wrap_label_value, end-truncated only when even
    that doesn't fit."""
    if picker.mode == "safe_merge":
        label = "Safe-merge in branch"
    elif picker.mode == "merge":
        label = "Merge in branch"
    elif picker.mode == "set_upstream":
        label = "Set upstream"
    else:
        label = "Switch branch"
    return wrap_label_value(label, picker.target_label, inner_w)


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
    has_create = _has_create_row(picker)

    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(picker, target_inner_w)

    create_rows = 1 if has_create else 0
    n_branches = max(1, len(picker.branches))
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

    if not picker.branches:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate("(no branches found)", inner_w),
                    sb | curses.A_DIM)
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(picker), attr=sb | curses.A_DIM)
        return

    n = len(picker.branches)
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

        name = picker.branches[idx]
        focused = (idx == picker.selected)
        is_current = (name == picker.current)
        marker = "* " if is_current else "  "
        prefix = "→ " if focused else marker
        text = end_truncate(prefix + name, inner_w).ljust(inner_w)
        attr = sb | curses.A_REVERSE if focused else sb
        if is_current and not focused:
            attr |= curses.A_BOLD
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(picker), attr=sb | curses.A_DIM)


def handle_branch_picker_key(state: State, key: int) -> None:
    picker = state.branch_picker
    if picker is None:
        return
    if key == 27:
        state.branch_picker = None
        return

    has_create = _has_create_row(picker)
    on_create = has_create and picker.selected == -1

    if on_create:
        if key == curses.KEY_DOWN:
            if picker.branches:
                picker.selected = 0
            return
        if key == curses.KEY_UP:
            return  # already at the top of the modal
        if key in (10, 13, curses.KEY_ENTER):
            name = picker.create_typed.strip()
            if not name or not is_safe_ref_arg(name):
                return
            kick_off_action(
                state, "create_branch",
                target_label=picker.target_label,
                target_path=picker.target_path,
                target_repo=picker.target_repo,
                target_parent=picker.target_parent,
                branch_arg=name,
            )
            state.branch_picker = None
            state.action_menu = None
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            picker.create_typed = picker.create_typed[:-1]
            return
        if 32 <= key < 127:
            ch = chr(key)
            # Reject leading dash so the resulting ref can't be parsed
            # as a git option in any position.
            if not picker.create_typed and ch == "-":
                return
            if ch in _VALID_NAME_CHARS:
                picker.create_typed += ch
            return
        return

    if not picker.branches:
        return
    if key == curses.KEY_UP:
        if picker.selected == 0 and has_create:
            picker.selected = -1
        else:
            picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.branches) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        branch = picker.branches[picker.selected]
        if picker.mode in ("merge", "safe_merge"):
            # Self-merge is a no-op git would refuse anyway — silently
            # consume the keystroke so a stray Enter on the current row
            # doesn't dismiss the modal without doing anything visible.
            if branch == picker.current:
                return
            # A clean fast-forward in plain "merge" mode keeps using the
            # simple ff_merge action; everything else (divergent merge, or
            # explicit safe-merge mode) goes through the conflict resolver,
            # which stashes a backup, drives the merge, and lets the user
            # pick a side per conflict.
            ff = (picker.mode == "merge"
                  and is_fast_forward_merge(picker.target_path, branch))
            if not ff:
                kick_off_safe_merge(
                    state,
                    target_label=picker.target_label,
                    target_path=picker.target_path,
                    target_repo=picker.target_repo,
                    target_parent=picker.target_parent,
                    target_child=picker.target_child,
                    merge_ref=branch)
                state.branch_picker = None
                state.action_menu = None
                return
            action_id = "ff_merge"
        elif picker.mode == "set_upstream":
            # `branch` is the full remote-tracking ref (origin/main, …);
            # the worker passes it through to `git branch -u <ref>`
            # which targets the user's current branch.
            if not picker.current:
                return  # detached → set-upstream wouldn't make sense
            action_id = "set_upstream"
        else:
            action_id = "switch_branch"
        kick_off_action(
            state, action_id,
            target_label=picker.target_label,
            target_path=picker.target_path,
            target_repo=picker.target_repo,
            target_parent=picker.target_parent,
            branch_arg=branch,
        )
        state.branch_picker = None
        state.action_menu = None
