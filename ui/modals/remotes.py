"""Remotes manager — sub-modal of the action menu.

Two-mode UI: NAV mode (default) navigates rows and supports R / D
shortcuts; EDIT mode types into a focused field. All edits are local
to the modal — closing with pending changes shows a confirmation
prompt and dispatches a single batched task to apply them."""
from __future__ import annotations

import curses
from typing import List

from core.state.app import State
from core.state.remotes import RemoteRow, RemotesModal
from core.git_ops import list_remotes
from core.workers import _compute_remote_ops, kick_off_remote_changes

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_WARN
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr, wrap_label_value,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_TAB, KEY_UP_DOWN, Hint,
    render_hints,
)


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 100
_NAME_COL_W = 18  # remote-name column width within the modal


# Same allowlist used by branch_name_prompt — remote names follow the
# same shape as branch names for our purposes (no leading dash, ASCII
# alnum + a few punct marks).
_VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def _add_row_index(modal: RemotesModal) -> int:
    """Index of the '+ Add new remote' placeholder row — always at
    the bottom of the list."""
    return len(modal.rows)


def _on_add_row(modal: RemotesModal) -> bool:
    return modal.selected == _add_row_index(modal)


def _focused_row(modal: RemotesModal):
    if 0 <= modal.selected < len(modal.rows):
        return modal.rows[modal.selected]
    return None


def _has_pending_changes(modal: RemotesModal) -> bool:
    return bool(_compute_remote_ops(modal.rows))


def _hints(modal: RemotesModal) -> list:
    if modal.confirming:
        return [
            Hint("y", "apply"),
            Hint("n", "discard"),
            Hint(KEY_ESC, "back to edit"),
        ]
    if modal.edit_field:
        return [
            Hint("type", f"edit {modal.edit_field}"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, "save"),
            Hint(KEY_ESC, "cancel edit"),
        ]
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    row = _focused_row(modal)
    if _on_add_row(modal):
        hints.append(Hint(KEY_ENTER, "add new remote"))
    elif row is not None:
        if row.to_delete:
            hints.append(Hint("d", "undo delete"))
        else:
            hints.append(Hint(KEY_ENTER, "edit url"))
            hints.append(Hint("r", "rename"))
            hints.append(Hint("d", "delete"))
    pending = _has_pending_changes(modal)
    close_label = "apply changes" if pending else "close"
    hints.append(Hint(KEY_TAB, close_label))
    hints.append(Hint(KEY_ESC, close_label))
    return hints


def open_remotes_modal(state: State) -> None:
    """Build and install the modal, sourcing the current remote list
    from the focused repo's working tree. Cursor lands on the first
    remote when there is one, else on the '+ Add new remote'
    placeholder so an empty-remote-list repo is still usable."""
    menu = state.action_menu
    if menu is None:
        return
    rows: List[RemoteRow] = []
    for name, url in list_remotes(menu.target_path):
        rows.append(RemoteRow(
            original_name=name, original_url=url,
            name=name, url=url,
        ))
    state.remotes_modal = RemotesModal(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        rows=rows,
        selected=0 if rows else 0,  # 0 == add-row when rows is empty
    )


def _title_lines(modal: RemotesModal, inner_w: int) -> List[str]:
    return wrap_label_value("Remotes", modal.target_label, inner_w)


def _draw_remote_row(stdscr, y: int, inner_x: int, inner_w: int,
                     row: RemoteRow, focused: bool, edit_field: str,
                     sb: int) -> None:
    """One existing-or-pending remote row. Layout:
       prefix(2)  name(18)  url(rest)
    Edit mode reverse-videos just the field being typed into; nav
    focus reverse-videos the whole row."""
    prefix = "→ " if focused else "  "
    name_text = row.name or "(unnamed)"
    url_text = row.url or "(no url)"
    name_w = _NAME_COL_W

    # Compose attrs.
    base = sb
    if row.to_delete:
        base = sb | curses.A_DIM
    if row.is_new and not row.to_delete:
        base = sb  # new rows render in default attr
    name_attr = base
    url_attr = base
    if focused and not edit_field:
        name_attr |= curses.A_REVERSE
        url_attr |= curses.A_REVERSE
    if focused and edit_field == "name":
        name_attr = sb | curses.A_REVERSE
    if focused and edit_field == "url":
        url_attr = sb | curses.A_REVERSE

    # Render the prefix.
    safe_addstr(stdscr, y, inner_x, prefix, base)
    # Name column. Show typing cursor when edit_field == "name".
    if focused and edit_field == "name":
        body = f"{row.name}_"
    else:
        body = name_text
    name_cell = end_truncate(body, name_w).ljust(name_w)
    safe_addstr(stdscr, y, inner_x + 2, name_cell, name_attr)
    # URL column. Show typing cursor when edit_field == "url".
    url_x = inner_x + 2 + name_w + 2
    url_w = max(1, inner_w - (url_x - inner_x))
    if focused and edit_field == "url":
        body = f"{row.url}_"
    else:
        body = url_text
    # Suffix annotations (right-aligned tag): "(deleted)" / "(new)".
    tag = ""
    if row.to_delete:
        tag = "  (deleted)"
    elif row.is_new:
        tag = "  (new)"
    cell = end_truncate(body, max(1, url_w - len(tag)))
    cell = (cell + tag).ljust(url_w)
    safe_addstr(stdscr, y, url_x, cell, url_attr)


def _draw_add_row(stdscr, y: int, inner_x: int, inner_w: int,
                  focused: bool, sb: int) -> None:
    text = "+ Add new remote"
    attr = (sb | curses.A_REVERSE) if focused else (sb | curses.A_DIM)
    safe_addstr(stdscr, y, inner_x,
                end_truncate(text, inner_w).ljust(inner_w), attr)


def _summary(modal: RemotesModal) -> List[str]:
    """One-line-per-op summary for the confirmation prompt."""
    ops = _compute_remote_ops(modal.rows)
    out: List[str] = []
    for op in ops:
        if op[0] == "remove":
            out.append(f"  - remove {op[1]}")
        elif op[0] == "rename":
            out.append(f"  - rename {op[1]} → {op[2]}")
        elif op[0] == "set_url":
            out.append(f"  - set-url {op[1]} → {op[2]}")
        elif op[0] == "add":
            out.append(f"  - add {op[1]} → {op[2]}")
    return out


def draw_remotes_modal(stdscr, state: State, sidebar_x: int) -> None:
    modal = state.remotes_modal
    if modal is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(modal, target_inner_w)

    body_rows = max(1, len(modal.rows) + 1)  # +1 for the add-row
    blank_after_title = 1
    blank_after_list = 1
    hint_rows = 1
    confirm_rows = 0
    summary_lines: List[str] = []
    if modal.confirming:
        summary_lines = _summary(modal)
        # 1 prompt + N op lines + 1 blank
        confirm_rows = 1 + len(summary_lines) + 1

    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + body_rows + blank_after_list
        + confirm_rows + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    if inner_w != target_inner_w:
        title_rows = _title_lines(modal, inner_w)

    # Title.
    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    # Body — fixed_rows below the body region; scroll the rest.
    fixed_rows = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + blank_after_list + confirm_rows + hint_rows + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)

    n_total = len(modal.rows) + 1  # rows + add-row
    modal.scroll = clamp_scroll(
        modal.selected, modal.scroll, n_total, visible_rows)
    end = min(n_total, modal.scroll + visible_rows)

    for slot in range(visible_rows):
        idx = modal.scroll + slot
        if idx >= n_total:
            break
        row_y = line + slot

        if slot == 0 and modal.scroll > 0:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 modal.scroll, "up", sb | curses.A_DIM)
            continue
        if slot == visible_rows - 1 and end < n_total:
            draw_scroll_overflow(stdscr, row_y, inner_x, inner_w,
                                 n_total - end + 1, "down",
                                 sb | curses.A_DIM)
            continue

        focused = (idx == modal.selected) and not modal.confirming
        if idx < len(modal.rows):
            edit_field = modal.edit_field if focused else ""
            _draw_remote_row(stdscr, row_y, inner_x, inner_w,
                             modal.rows[idx], focused, edit_field, sb)
        else:
            _draw_add_row(stdscr, row_y, inner_x, inner_w, focused, sb)

    line += visible_rows + blank_after_list

    # Confirmation overlay (just an inline summary block, not a separate
    # modal — the keys are eaten by the prompt while `confirming` is
    # true, so the user can't navigate underneath it).
    if modal.confirming:
        prompt = (f"Apply {len(summary_lines)} change(s)? "
                  "[y]es  [n]o  [Esc] cancel")
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(prompt, inner_w),
                    curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
        line += 1
        for s in summary_lines:
            safe_addstr(stdscr, line, inner_x,
                        end_truncate(s, inner_w), sb)
            line += 1
        line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(modal), attr=sb | curses.A_DIM)


def _close_or_confirm(state: State) -> None:
    """Common close path: enter confirmation if there are pending
    changes, otherwise drop the modal immediately."""
    modal = state.remotes_modal
    if modal is None:
        return
    if _has_pending_changes(modal):
        modal.confirming = True
        return
    state.remotes_modal = None


def _apply_and_close(state: State) -> None:
    modal = state.remotes_modal
    if modal is None:
        return
    kick_off_remote_changes(
        state, modal.rows,
        target_label=modal.target_label,
        target_path=modal.target_path,
        target_repo=modal.target_repo,
    )
    state.remotes_modal = None


def _enter_edit(modal: RemotesModal, field: str) -> None:
    row = _focused_row(modal)
    if row is None:
        return
    modal.edit_field = field
    modal.edit_pre_value = row.name if field == "name" else row.url


def _commit_edit(modal: RemotesModal) -> None:
    """Exit edit mode keeping the buffer changes."""
    modal.edit_field = ""
    modal.edit_pre_value = ""


def _cancel_edit(modal: RemotesModal) -> None:
    """Exit edit mode reverting the field to its pre-edit value."""
    row = _focused_row(modal)
    if row is None:
        modal.edit_field = ""
        modal.edit_pre_value = ""
        return
    if modal.edit_field == "name":
        row.name = modal.edit_pre_value
    elif modal.edit_field == "url":
        row.url = modal.edit_pre_value
    # If we're cancelling the very first edit on a brand-new row,
    # discarding the row outright matches what the user expects (they
    # opened it then bailed). Detect by both name and url empty.
    if row.is_new and not row.name and not row.url:
        try:
            modal.rows.remove(row)
        except ValueError:
            pass
        if modal.selected >= len(modal.rows):
            modal.selected = len(modal.rows)  # land on add-row
    modal.edit_field = ""
    modal.edit_pre_value = ""


def _add_new_row(modal: RemotesModal) -> None:
    """Insert a fresh pending-add row at the end of the list (just
    above the placeholder), focus it, and enter name-edit mode so
    the user can start typing immediately."""
    new_row = RemoteRow(is_new=True)
    modal.rows.append(new_row)
    modal.selected = len(modal.rows) - 1
    _enter_edit(modal, "name")


def _handle_typing(modal: RemotesModal, key: int) -> None:
    row = _focused_row(modal)
    if row is None:
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if modal.edit_field == "name":
            row.name = row.name[:-1]
        elif modal.edit_field == "url":
            row.url = row.url[:-1]
        return
    if 32 <= key < 127:
        ch = chr(key)
        if modal.edit_field == "name":
            if not row.name and ch == "-":
                return
            if ch in _VALID_NAME_CHARS:
                row.name += ch
        elif modal.edit_field == "url":
            # URLs allow ":" "@" and a few more chars beyond the name
            # allowlist. Accept any printable ASCII char so the user
            # can paste a real git URL without us silently filtering.
            row.url += ch


def _handle_confirming(state: State, key: int) -> None:
    modal = state.remotes_modal
    if modal is None:
        return
    if key in (ord("y"), ord("Y")):
        _apply_and_close(state)
        return
    if key in (ord("n"), ord("N")):
        state.remotes_modal = None
        return
    if key == 27:
        modal.confirming = False
        return


def handle_remotes_modal_key(state: State, key: int) -> None:
    modal = state.remotes_modal
    if modal is None:
        return

    if modal.confirming:
        _handle_confirming(state, key)
        return

    if modal.edit_field:
        if key in (10, 13, curses.KEY_ENTER):
            _commit_edit(modal)
            return
        if key == 27:
            _cancel_edit(modal)
            return
        _handle_typing(modal, key)
        return

    # Nav mode.
    if key in (27, 9):  # Esc / Tab — close, with confirmation if dirty
        _close_or_confirm(state)
        return

    n_rows = len(modal.rows)
    n_total = n_rows + 1  # +1 for the add row

    if key == curses.KEY_UP:
        modal.selected = max(0, modal.selected - 1)
        return
    if key == curses.KEY_DOWN:
        modal.selected = min(n_total - 1, modal.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        modal.selected = max(0, modal.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        modal.selected = min(n_total - 1, modal.selected + 10)
        return

    if _on_add_row(modal):
        if key in (10, 13, curses.KEY_ENTER):
            _add_new_row(modal)
        return

    row = _focused_row(modal)
    if row is None:
        return

    if key in (10, 13, curses.KEY_ENTER):
        if row.to_delete:
            return  # no edits on a deleted row
        _enter_edit(modal, "url")
        return
    if key in (ord("r"), ord("R")):
        if row.to_delete:
            return
        _enter_edit(modal, "name")
        return
    if key in (ord("d"), ord("D")):
        if row.is_new:
            # Brand-new row never existed on disk — drop it outright
            # rather than carrying a pending-delete flag for a no-op.
            modal.rows.remove(row)
            if modal.selected >= len(modal.rows):
                modal.selected = len(modal.rows)
            return
        row.to_delete = not row.to_delete
        return


__all__ = [
    "open_remotes_modal",
    "draw_remotes_modal",
    "handle_remotes_modal_key",
]
