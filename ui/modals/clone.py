"""Clone-repository modal — opened from the workspace menu's
'+ Clone repository…' row. Collects URL / destination / optional
branch / submodule toggle, validates, and dispatches `git clone` via
a daemon worker. The user is expected to Ctrl+R after the task lands
to bring the new repo into the workspace's row list — the alternative
(autoreload) would race with the worker and surfaces no benefit
worth the complexity."""
from __future__ import annotations

import curses
from typing import List

from core.state.app import State
from core.state.clone import CloneModal
from features.clone_modal.actions import (
    handle_clone_modal_key as handle_clone_modal_key_action,
)
from features.clone_modal.projection import (
    FIELD_BRANCH,
    FIELD_BUTTON,
    FIELD_DEST,
    FIELD_RECURSE,
    FIELD_URL,
    N_FIELDS,
    can_clone,
    clone_modal_hint_specs,
    default_dest,
)

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import Hint, render_hints


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 90
_LABEL_W = 18  # label column width


def _hints(modal: CloneModal) -> list:
    return [Hint(keys, action)
            for keys, action in clone_modal_hint_specs(modal)]


def _title_lines(modal: CloneModal, inner_w: int) -> List[str]:
    return wrap_label_value("Clone repository",
                            f"workspace: {modal.workspace_name}",
                            inner_w)


def _draw_text_field(stdscr, y: int, inner_x: int, inner_w: int,
                     label: str, value: str, placeholder: str,
                     focused: bool, edit_field_active: bool,
                     sb: int) -> None:
    """One label-and-value field row. Edit-mode shows a `_` cursor at
    end of the value; nav-mode focused state reverse-videos the value
    cell so the user knows where Enter would land."""
    label_text = label.ljust(_LABEL_W)[:_LABEL_W]
    safe_addstr(stdscr, y, inner_x, label_text,
                sb | (curses.A_BOLD if focused else curses.A_DIM))
    val_x = inner_x + _LABEL_W + 2
    val_w = max(1, inner_w - (_LABEL_W + 2))
    if edit_field_active:
        body = f"{value}_"
        attr = sb | curses.A_REVERSE
    elif focused:
        body = value or placeholder
        attr = sb | curses.A_REVERSE
        if not value:
            attr |= curses.A_DIM
    else:
        body = value or placeholder
        attr = sb if value else (sb | curses.A_DIM)
    safe_addstr(stdscr, y, val_x,
                end_truncate(body, val_w).ljust(val_w), attr)


def _draw_toggle(stdscr, y: int, inner_x: int, inner_w: int,
                 label: str, on: bool, focused: bool, sb: int) -> None:
    box = "[x]" if on else "[ ]"
    text = f"{box}  {label}"
    attr = sb | curses.A_REVERSE if focused else sb
    safe_addstr(stdscr, y, inner_x,
                end_truncate(text, inner_w).ljust(inner_w), attr)


def _draw_button(stdscr, y: int, inner_x: int, inner_w: int,
                 modal: CloneModal, focused: bool, sb: int) -> None:
    if modal.cloning:
        text = "  → Cloning…"
    elif modal.error:
        text = f"  → Clone   ({modal.error})"
    else:
        text = "  → Clone"
    attr = sb
    if focused:
        attr |= curses.A_REVERSE
        if not can_clone(modal):
            attr |= curses.A_DIM
    elif not can_clone(modal):
        attr |= curses.A_DIM
    safe_addstr(stdscr, y, inner_x,
                end_truncate(text, inner_w).ljust(inner_w), attr)


def draw_clone_modal(stdscr, state: State, sidebar_x: int) -> None:
    modal = state.clone_modal
    if modal is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(modal, target_inner_w)

    # Body: 4 input rows + button = 5 rows; +1 spacer above button.
    body_rows = N_FIELDS + 1
    blank_after_title = 1
    blank_after_body = 1
    hint_rows = 1

    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + body_rows + blank_after_body + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)
    if inner_w != target_inner_w:
        title_rows = _title_lines(modal, inner_w)

    line = y + _PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    # URL.
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Repository URL",
        modal.url, "git@github.com:user/repo.git",
        focused=(modal.selected == FIELD_URL and not modal.edit_field),
        edit_field_active=(modal.edit_field == "url"),
        sb=sb)
    line += 1

    # Destination — show auto-derived default in placeholder when empty.
    placeholder = default_dest(modal) or "/path/to/local/clone"
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Local path",
        modal.dest_text, placeholder,
        focused=(modal.selected == FIELD_DEST and not modal.edit_field),
        edit_field_active=(modal.edit_field == "dest"),
        sb=sb)
    line += 1

    # Branch.
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Branch (optional)",
        modal.branch, "(remote default)",
        focused=(modal.selected == FIELD_BRANCH and not modal.edit_field),
        edit_field_active=(modal.edit_field == "branch"),
        sb=sb)
    line += 1

    # Submodules toggle.
    _draw_toggle(stdscr, line, inner_x, inner_w,
                 "Init submodules", modal.recurse_submodules,
                 focused=(modal.selected == FIELD_RECURSE),
                 sb=sb)
    line += 1

    line += 1  # spacer above button

    _draw_button(stdscr, line, inner_x, inner_w, modal,
                 focused=(modal.selected == FIELD_BUTTON),
                 sb=sb)
    line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(modal), attr=sb | curses.A_DIM)


def handle_clone_modal_key(state: State, key: int) -> None:
    handle_clone_modal_key_action(state, key)


__all__ = [
    "draw_clone_modal",
    "handle_clone_modal_key",
]
