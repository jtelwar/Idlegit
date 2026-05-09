"""Clone-repository modal — opened from the workspace menu's
'+ Clone repository…' row. Collects URL / destination / optional
branch / submodule toggle, validates, and dispatches `git clone` via
a daemon worker. The user is expected to Ctrl+R after the task lands
to bring the new repo into the workspace's row list — the alternative
(autoreload) would race with the worker and surfaces no benefit
worth the complexity."""
from __future__ import annotations

import curses
from pathlib import Path
from typing import List

from core.models import CloneModal, State
from core.workers import kick_off_clone

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_SPACE, KEY_TAB, KEY_UP_DOWN,
    Hint, render_hints,
)


_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 90
_LABEL_W = 18  # label column width

# Field ordering. Selected indices map onto these positions.
_FIELD_URL = 0
_FIELD_DEST = 1
_FIELD_BRANCH = 2
_FIELD_RECURSE = 3
_FIELD_BUTTON = 4
_N_FIELDS = 5


def _name_from_url(url: str) -> str:
    """Heuristic: strip everything up to and including the last '/' and
    drop a trailing '.git' if present. Falls back to "repo" so an
    empty / bizarre URL doesn't crash the dest path defaulting."""
    if not url:
        return ""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last.rstrip()
    if last.endswith(".git"):
        last = last[:-4]
    return last or "repo"


def _default_dest(modal: CloneModal) -> str:
    """Default destination path: <first workspace folder>/<repo-name-
    derived-from-url>. Used to seed the field once the user finishes
    typing a URL — never overwrites a value the user typed
    explicitly."""
    if not modal.workspace_folders:
        return ""
    name = _name_from_url(modal.url)
    if not name:
        return str(modal.workspace_folders[0])
    return str(modal.workspace_folders[0] / name)


def _hints(modal: CloneModal) -> list:
    if modal.edit_field:
        return [
            Hint("type", f"edit {modal.edit_field}"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, "save"),
            Hint(KEY_ESC, "cancel edit"),
        ]
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    sel = modal.selected
    if sel == _FIELD_URL:
        hints.append(Hint(KEY_ENTER, "edit url"))
    elif sel == _FIELD_DEST:
        hints.append(Hint(KEY_ENTER, "edit path"))
    elif sel == _FIELD_BRANCH:
        hints.append(Hint(KEY_ENTER, "edit branch"))
    elif sel == _FIELD_RECURSE:
        on = modal.recurse_submodules
        hints.append(Hint(KEY_SPACE,
                          "init submodules off" if on else "init submodules on"))
    elif sel == _FIELD_BUTTON:
        if _can_clone(modal):
            hints.append(Hint(KEY_ENTER, "run clone"))
        else:
            hints.append(Hint(KEY_ENTER, "(fill url + path first)"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _can_clone(modal: CloneModal) -> bool:
    if modal.cloning:
        return False
    if not modal.url or modal.url.startswith("-"):
        return False
    if not modal.dest_text.strip():
        return False
    return True


def open_clone_modal(state: State) -> None:
    """Install a CloneModal seeded from the active workspace's folders.
    Cursor lands on the URL field — the user almost always types that
    first, then either tabs through or accepts the auto-derived
    destination."""
    ws = state.active_workspace
    if ws is None:
        return
    state.clone_modal = CloneModal(
        workspace_name=ws.name,
        workspace_folders=list(ws.folders),
        selected=_FIELD_URL,
    )


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
        if not _can_clone(modal):
            attr |= curses.A_DIM
    elif not _can_clone(modal):
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
    body_rows = _N_FIELDS + 1
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
        focused=(modal.selected == _FIELD_URL and not modal.edit_field),
        edit_field_active=(modal.edit_field == "url"),
        sb=sb)
    line += 1

    # Destination — show auto-derived default in placeholder when empty.
    placeholder = _default_dest(modal) or "/path/to/local/clone"
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Local path",
        modal.dest_text, placeholder,
        focused=(modal.selected == _FIELD_DEST and not modal.edit_field),
        edit_field_active=(modal.edit_field == "dest"),
        sb=sb)
    line += 1

    # Branch.
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Branch (optional)",
        modal.branch, "(remote default)",
        focused=(modal.selected == _FIELD_BRANCH and not modal.edit_field),
        edit_field_active=(modal.edit_field == "branch"),
        sb=sb)
    line += 1

    # Submodules toggle.
    _draw_toggle(stdscr, line, inner_x, inner_w,
                 "Init submodules", modal.recurse_submodules,
                 focused=(modal.selected == _FIELD_RECURSE),
                 sb=sb)
    line += 1

    line += 1  # spacer above button

    _draw_button(stdscr, line, inner_x, inner_w, modal,
                 focused=(modal.selected == _FIELD_BUTTON),
                 sb=sb)
    line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(modal), attr=sb | curses.A_DIM)


def _enter_edit(modal: CloneModal, field: str) -> None:
    modal.edit_field = field
    if field == "url":
        modal.edit_pre_value = modal.url
    elif field == "dest":
        modal.edit_pre_value = modal.dest_text
    elif field == "branch":
        modal.edit_pre_value = modal.branch
    modal.error = ""


def _commit_edit(modal: CloneModal) -> None:
    """Save the in-buffer value and exit edit mode. When the user just
    edited the URL, auto-derive a default destination if they hadn't
    typed one yet — same behaviour as a typical GUI clone dialog."""
    if modal.edit_field == "url" and not modal.dest_text.strip():
        modal.dest_text = _default_dest(modal)
    modal.edit_field = ""
    modal.edit_pre_value = ""


def _cancel_edit(modal: CloneModal) -> None:
    if modal.edit_field == "url":
        modal.url = modal.edit_pre_value
    elif modal.edit_field == "dest":
        modal.dest_text = modal.edit_pre_value
    elif modal.edit_field == "branch":
        modal.branch = modal.edit_pre_value
    modal.edit_field = ""
    modal.edit_pre_value = ""


def _handle_typing(modal: CloneModal, key: int) -> None:
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if modal.edit_field == "url":
            modal.url = modal.url[:-1]
        elif modal.edit_field == "dest":
            modal.dest_text = modal.dest_text[:-1]
        elif modal.edit_field == "branch":
            modal.branch = modal.branch[:-1]
        return
    if 32 <= key < 127:
        ch = chr(key)
        if modal.edit_field == "url":
            modal.url += ch
        elif modal.edit_field == "dest":
            modal.dest_text += ch
        elif modal.edit_field == "branch":
            # Branch names follow the same allowlist as elsewhere.
            allowed = (
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789-_./"
            )
            if not modal.branch and ch == "-":
                return
            if ch in allowed:
                modal.branch += ch


def _try_clone(state: State) -> None:
    modal = state.clone_modal
    if modal is None:
        return
    if not _can_clone(modal):
        return
    dest = Path(modal.dest_text.strip()).expanduser()
    if dest.exists() and any(dest.iterdir()):
        modal.error = "destination exists and is not empty"
        return
    modal.error = ""
    modal.cloning = True

    def on_done(ok: bool, msg: str) -> None:
        # Worker thread — set fields without touching curses.
        modal.cloning = False
        if not ok:
            modal.error = msg or "clone failed"
            return
        # Success: drop the modal so the user lands back on the
        # workspace menu. The discovery refresh is owned by the user
        # (Ctrl+R) — autoreload would race with the worker.
        state.clone_modal = None

    kick_off_clone(state, modal.url.strip(), dest,
                   modal.branch.strip(),
                   modal.recurse_submodules,
                   on_done=on_done)


def handle_clone_modal_key(state: State, key: int) -> None:
    modal = state.clone_modal
    if modal is None:
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
    if key in (27, 9):  # Esc / Tab — close.
        # If a clone is in flight, the worker keeps running; we just
        # detach the modal. The sidebar task tracks the rest.
        state.clone_modal = None
        return
    if key == curses.KEY_UP:
        modal.selected = max(0, modal.selected - 1)
        return
    if key == curses.KEY_DOWN:
        modal.selected = min(_N_FIELDS - 1, modal.selected + 1)
        return

    sel = modal.selected
    if sel == _FIELD_URL and key in (10, 13, curses.KEY_ENTER):
        _enter_edit(modal, "url")
        return
    if sel == _FIELD_DEST and key in (10, 13, curses.KEY_ENTER):
        # Pre-populate the dest with the auto-derived default the
        # placeholder is showing — saves the user from retyping it.
        if not modal.dest_text.strip():
            modal.dest_text = _default_dest(modal)
        _enter_edit(modal, "dest")
        return
    if sel == _FIELD_BRANCH and key in (10, 13, curses.KEY_ENTER):
        _enter_edit(modal, "branch")
        return
    if sel == _FIELD_RECURSE and key in (
            ord(" "), 10, 13, curses.KEY_ENTER):
        modal.recurse_submodules = not modal.recurse_submodules
        return
    if sel == _FIELD_BUTTON and key in (10, 13, curses.KEY_ENTER):
        _try_clone(state)
        return


__all__ = [
    "open_clone_modal",
    "draw_clone_modal",
    "handle_clone_modal_key",
]
