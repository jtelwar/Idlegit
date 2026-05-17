"""Large multi-line commit-message editor modal.

Opened with Shift+Right on a dirty repo / submodule row. Binds the
textarea directly to the row's `.message` attribute so every keystroke
mutates the underlying Repo / ChildRef — the main panel's inline
field reads the same string and updates in lockstep.

Enter / Esc / Tab all close the modal and return focus to the row
that opened it (the row that was selected when the modal opened
remains selected on the main panel; the modal makes no change to
`state.selected`).

The header row mirrors the repos-panel styling: bold display name
then the branch in square brackets, in PAIR_BRANCH. The textarea
beneath is a bordered modal in the same chrome as the other
modals (workspace menu, app menu, …).

Newlines in `message` render as actual multi-line content; Enter is
bound to close, so newlines only appear via paste from outside or
from initial-state content that already contained them."""
from __future__ import annotations

import curses
from typing import List, Optional

from core.models import ChildRef, CommitMsgEditor, Repo, State

from ..colors import PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FIELD
from ..geometry import (
    draw_modal_fill, modal_geometry, safe_addstr, truncate,
)
from ..hints import KEY_ENTER, KEY_ESC, Hint, render_hints


# Standard modal sizing — wide enough for a 72-char commit body
# plus the modal padding. Height flexes to terminal height via the
# `min(...)` in `modal_geometry`, so this is a target, not a cap.
MODAL_W = 84
BODY_TARGET_ROWS = 12


# ---------- Cursor + line helpers ---------------------------------------


def _split_lines(text: str) -> List[str]:
    """Split on `\\n`, preserving empty trailing line so cursor at the
    end of the message has somewhere to land. Splitting `"foo\\n"` gives
    `["foo", ""]`, which is exactly what an editor wants for
    "blinking cursor at the start of the next line after the newline."""
    return text.split("\n")


def _cursor_to_row_col(text: str, cursor: int) -> "tuple[int, int]":
    """Flat cursor index → (row, col) over `text`. Clamps cursor into
    `[0, len(text)]`; out-of-range inputs round in to a safe value."""
    cursor = max(0, min(cursor, len(text)))
    before = text[:cursor]
    rows = before.split("\n")
    return len(rows) - 1, len(rows[-1])


def _row_col_to_cursor(text: str, row: int, col: int) -> int:
    """Inverse of `_cursor_to_row_col` — converts (row, col) back to a
    flat index, clamping `row` into the line range and `col` into the
    target line's length so callers can move "up one row, same column"
    without worrying about shorter lines above / below."""
    lines = _split_lines(text)
    if not lines:
        return 0
    row = max(0, min(row, len(lines) - 1))
    col = max(0, min(col, len(lines[row])))
    # Sum prior lines + their trailing `\n` separators, then the
    # column offset on the target line.
    return sum(len(line) + 1 for line in lines[:row]) + col


def _holder_message(holder) -> str:
    """Read the message off a Repo or submodule ChildRef. Defaults to
    "" when the holder hasn't been touched yet — both attr-accesses
    succeed (both dataclasses declare `message: str = ""`)."""
    return getattr(holder, "message", "") or ""


def _set_holder_message(holder, value: str) -> None:
    holder.message = value


# ---------- Open helpers -------------------------------------------------


def _focused_holder(state: State) -> "tuple[Optional[object], Optional[Repo], str, str]":
    """Return `(holder, parent, label, branch)` for whatever row is
    currently focused on the main panel — or all-None when the focus is
    on a non-editable row (title / workspace / subtree / focus on the
    task panel).

    `parent` is set only for submodule child rows; the modal uses it
    in the header (e.g. "ParentRepo / submodule") and the keypress
    dispatcher uses it as a fallback target. `label` and `branch`
    mirror the strings the repos panel prints for the same row."""
    if state.focused_panel != "repos":
        return None, None, "", ""
    if state.on_title_row or state.on_workspace_row:
        return None, None, "", ""
    cur = state.current_repo
    if cur is not None:
        return cur, None, cur.display_name, cur.branch
    cur_child = state.current_child
    if cur_child is not None and cur_child[1].kind == "submodule":
        parent_repo, child = cur_child
        nested = child.repo.display_name
        # Match how the repos panel reads — submodule's own name first,
        # parent as context after a slash, so the header looks the same
        # as the (parent → submodule) row layout on the main screen.
        label = f"{parent_repo.display_name} / {nested}"
        branch = child.branch or child.repo.branch
        return child, parent_repo, label, branch
    return None, None, "", ""


def _holder_is_editable(holder) -> bool:
    """The modal only opens on rows where the commit message is
    actually usable: a dirty Repo, a Repo with an in-progress message,
    a dirty submodule ChildRef, or a submodule ChildRef with an
    in-progress message. Clean rows with no pending message wouldn't
    have anything to edit."""
    if isinstance(holder, Repo):
        return holder.is_dirty or bool(holder.message)
    if isinstance(holder, ChildRef):
        return holder.kind == "submodule" and (
            holder.dirty or bool(holder.message))
    return False


def open_commit_msg_editor(state: State) -> bool:
    """Install the modal on `state.commit_msg_editor` if the currently
    focused row has an editable commit message. No-op (returns False)
    when the focused row is the title / workspace / a clean repo / a
    subtree / a refreshing row — the binding is "Shift+Right on any
    dirty repo row" and silently ignores everything else."""
    holder, parent, label, branch = _focused_holder(state)
    if holder is None:
        return False
    if getattr(holder, "refreshing", False):
        return False
    if not _holder_is_editable(holder):
        return False
    msg = _holder_message(holder)
    state.commit_msg_editor = CommitMsgEditor(
        holder=holder,
        parent=parent,
        label=label,
        branch=branch or "",
        cursor=len(msg),
        scroll=0,
    )
    return True


# ---------- Hints --------------------------------------------------------


def _hints() -> list:
    # Tab deliberately omitted — the modal opens with Shift+Right, so
    # the project's "Tab opens / Tab closes" rule doesn't apply here.
    # Close is Esc or Enter only.
    return [
        Hint(KEY_ENTER, "close"),
        Hint(KEY_ESC, "close"),
    ]


# ---------- Handle -------------------------------------------------------


def handle_commit_msg_editor_key(state: State, key: int) -> None:
    """Dispatch a keypress against the open editor. The textarea binds
    directly to `editor.holder.message` so every mutation is visible to
    the rest of the app immediately — no `apply` step.

    Enter / Esc / Tab all close. Newlines aren't insertable from the
    keyboard for that reason; multi-line content arrives via paste or
    from a pre-existing message that already contained `\\n`."""
    editor = state.commit_msg_editor
    if editor is None:
        return

    # Close on Enter / Esc / Tab.
    if key in (27, 10, 13, curses.KEY_ENTER):
        # Tab intentionally NOT included — the modal opens with
        # Shift+Right, so per the "Tab opens / Tab closes" project
        # convention it should not also close on Tab.
        state.commit_msg_editor = None
        return

    msg = _holder_message(editor.holder)
    cursor = max(0, min(editor.cursor, len(msg)))

    # ---- Cursor navigation ----
    if key == curses.KEY_LEFT:
        editor.cursor = max(0, cursor - 1)
        return
    if key == curses.KEY_RIGHT:
        editor.cursor = min(len(msg), cursor + 1)
        return
    if key == curses.KEY_UP:
        editor.cursor = _move_display_vertical(msg, cursor, -1,
                                               _wrap_width(editor))
        return
    if key == curses.KEY_DOWN:
        editor.cursor = _move_display_vertical(msg, cursor, +1,
                                               _wrap_width(editor))
        return
    if key in (curses.KEY_HOME, 1):  # Home / Ctrl+A — line start
        row, _ = _cursor_to_row_col(msg, cursor)
        editor.cursor = _row_col_to_cursor(msg, row, 0)
        return
    if key in (curses.KEY_END, 5):  # End / Ctrl+E — line end
        row, _ = _cursor_to_row_col(msg, cursor)
        lines = _split_lines(msg)
        editor.cursor = _row_col_to_cursor(msg, row, len(lines[row]))
        return

    # ---- Deletion ----
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cursor > 0:
            _set_holder_message(
                editor.holder, msg[: cursor - 1] + msg[cursor:])
            editor.cursor = cursor - 1
        return
    if key == curses.KEY_DC:  # forward delete
        if cursor < len(msg):
            _set_holder_message(
                editor.holder, msg[:cursor] + msg[cursor + 1:])
        return

    # ---- Printable insert ----
    # ASCII printables only — high-bit / control codes get dropped so
    # an accidental paste of control chars doesn't garble the message.
    if 32 <= key < 127:
        ch = chr(key)
        _set_holder_message(
            editor.holder, msg[:cursor] + ch + msg[cursor:])
        editor.cursor = cursor + 1
        return


# ---------- Draw ---------------------------------------------------------


def _wrap_logical_line(line: str, width: int) -> List[str]:
    """Hard-wrap a single logical line into display rows of at most
    `width` cells. No word boundary respect — paths and shas should
    not be split on whitespace, and the modal width (~80 cells) is
    usually generous enough that wrap-at-spaces doesn't pay for the
    surprise. Always returns at least one row so an empty logical
    line still occupies a display row (the cursor can sit there)."""
    if width <= 0:
        return [""]
    if not line:
        return [""]
    rows: List[str] = []
    i = 0
    while i < len(line):
        rows.append(line[i: i + width])
        i += width
    return rows


def _build_display_rows(msg: str, width: int
                       ) -> "tuple[List[str], List[tuple[int, int]]]":
    """Return `(display_rows, origins)` where `origins[d]` is the
    `(logical_row, char_offset_within_logical_row)` that display row
    `d` starts at. Used by the draw routine to map the flat cursor
    index back to a screen coordinate without re-splitting on every
    redraw frame."""
    display_rows: List[str] = []
    origins: List[tuple[int, int]] = []
    logical = _split_lines(msg)
    for lr, line in enumerate(logical):
        pieces = _wrap_logical_line(line, width)
        offset = 0
        for piece in pieces:
            display_rows.append(piece)
            origins.append((lr, offset))
            offset += len(piece)
    return display_rows, origins


def _cursor_display_position(msg: str, cursor: int, width: int
                             ) -> "tuple[int, int]":
    """Map flat-cursor → (display_row, column_in_row), accounting for
    hard-wrap. The cursor lives on the display row whose origin spans
    its logical column."""
    row, col = _cursor_to_row_col(msg, cursor)
    display_rows, origins = _build_display_rows(msg, width)
    # Walk forward to find the display row corresponding to this
    # logical (row, col). Most messages are short — this is a few
    # iterations at worst.
    target_d = 0
    for d, (lr, offset) in enumerate(origins):
        if lr != row:
            continue
        end = offset + len(display_rows[d])
        # Cursor at exactly `end` belongs to the next display row
        # for this logical line if one exists (so the user can sit
        # at the seam between wrapped halves).
        if offset <= col < end:
            return d, col - offset
        if col == end:
            target_d = d
            # Continue — the next display row may also belong to
            # the same logical line; prefer landing at column 0
            # there over column == width here.
            if (d + 1 < len(origins) and origins[d + 1][0] == row):
                continue
            return d, col - offset
    return target_d, col


def _wrap_width(editor: CommitMsgEditor) -> int:
    """The wrap width used by the last draw frame, stashed on the editor
    so key handlers can move the cursor in display-row space. Defaults
    to the modal's nominal inner width when the modal hasn't drawn yet
    (defensive — handlers never fire before the first draw in normal
    flow, but the fallback keeps the helpers total when called from
    tests that drive keys without a curses screen)."""
    w = getattr(editor, "_wrap_width", 0)
    if w > 0:
        return w
    return max(1, MODAL_W - 4)


def _move_display_vertical(msg: str, cursor: int, delta: int,
                           width: int) -> int:
    """Move the flat cursor by `delta` display rows (`-1` = Up,
    `+1` = Down), preserving the visual column. Works on hard-wrapped
    rows, so a single long logical line that wraps three times feels
    like a three-row block — Up from the bottom-wrapped row lands on
    the middle-wrapped row, not jumping to the start of the message.

    Clamps to the cursor's current visual column on the target row,
    matching how every other multi-line editor handles vertical motion
    onto shorter lines. Above-the-top → flat cursor 0; below-the-end →
    flat cursor `len(msg)`."""
    cur_d, cur_col = _cursor_display_position(msg, cursor, width)
    display_rows, origins = _build_display_rows(msg, width)
    target_d = cur_d + delta
    if target_d < 0:
        return 0
    if target_d >= len(display_rows):
        return len(msg)
    logical_row, char_offset = origins[target_d]
    new_col_in_row = min(cur_col, len(display_rows[target_d]))
    return _row_col_to_cursor(msg, logical_row, char_offset + new_col_in_row)


def draw_commit_msg_editor(stdscr, state: State, sidebar_x: int) -> None:
    """Render the editor on top of the main screen. Modal chrome
    matches every other modal: bordered fill in `PAIR_DLG_FG`,
    bold header, hints footer. The inset textarea uses
    `PAIR_DLG_FIELD` for a slightly darker bg on terminals whose
    theme renders `COLOR_BLACK` as anything other than true black.

    Header layout mirrors the repos panel — display name in bold,
    branch in `[…]` coloured with `PAIR_DLG_CYAN` — so the user sees
    the same row identity they were focused on before opening."""
    editor = state.commit_msg_editor
    if editor is None:
        return

    # blank-top (1) + header (1) + blank (1) + textarea (body_h)
    # + blank (1) + footer (1) + blank-bottom (1).
    body_h = max(4, BODY_TARGET_ROWS)
    content_h = 1 + 1 + 1 + body_h + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4
    # Stash the wrap width so the key handler's vertical-move helpers
    # can step through display rows (not logical rows) — otherwise Up
    # on a wrapped long line jumps to the start of the message instead
    # of the previous visible row.
    editor._wrap_width = inner_w

    # ---- Header: name + branch (matches repos-panel styling) ----
    # Header text inherits the lighter panel bg via `sb`; the branch
    # tag uses PAIR_DLG_CYAN whose bg is dlg_bg — slightly different
    # from PAIR_DLG_PANEL_LIGHT but close enough to read as a chip,
    # and overriding it with the panel pair would lose the cyan
    # accent that matches the repos-panel branch style.
    name_max = max(8, inner_w - 16)
    name_text = truncate(editor.label, name_max, "middle")
    safe_addstr(stdscr, y + 1, inner_x, name_text,
                curses.A_BOLD | sb)
    if editor.branch:
        branch_text = f" [{editor.branch}]"
        branch_x = inner_x + min(len(name_text), name_max)
        # Clip the branch tag to whatever inner width is left so we
        # never write past the right border on a narrow terminal.
        remaining = max(0, inner_w - (branch_x - inner_x))
        safe_addstr(stdscr, y + 1, branch_x, branch_text[:remaining],
                    curses.color_pair(PAIR_DLG_CYAN))

    # ---- Textarea body ----
    text_y0 = y + 3
    text_h = body_h
    msg = _holder_message(editor.holder)
    # The textarea content shares the modal's inner width verbatim —
    # no border inside the border, just the bordered modal background.
    display_rows, origins = _build_display_rows(msg, inner_w)

    cur_d, cur_col = _cursor_display_position(msg, editor.cursor, inner_w)
    # Scroll: keep the cursor display row inside the visible window.
    if cur_d < editor.scroll:
        editor.scroll = cur_d
    elif cur_d >= editor.scroll + text_h:
        editor.scroll = cur_d - text_h + 1
    editor.scroll = max(0, min(editor.scroll,
                               max(0, len(display_rows) - text_h)))

    # Paint every textarea row (including empty ones at the bottom)
    # in PAIR_DLG_FIELD so the field shows as a uniform darker block
    # against the surrounding panel chrome. Pre-filling every row
    # also stops the modal's panel bg from bleeding through on lines
    # past the end of the message — without the ljust+fill, an empty
    # tail would render in `sb` and the field would look ragged.
    field_attr = curses.color_pair(PAIR_DLG_FIELD)
    if curses.COLORS < 256:
        # 8-color terminals can't render a meaningfully-darker bg, so
        # fall back to an underline-per-row hint that the area is a
        # field. Skip A_UNDERLINE on 256-color where the bg already
        # carries the signal — stacking both reads as cluttered.
        field_attr |= curses.A_UNDERLINE
    for i in range(text_h):
        line_y = text_y0 + i
        d_idx = editor.scroll + i
        if d_idx < len(display_rows):
            line_text = display_rows[d_idx]
        else:
            line_text = ""
        safe_addstr(stdscr, line_y, inner_x,
                    line_text[:inner_w].ljust(inner_w), field_attr)

    # ---- Footer hints ----
    render_hints(stdscr, y + h - 2, inner_x, inner_w, _hints(),
                 attr=sb | curses.A_DIM)

    # Stash the screen coords for the cursor on the editor so the
    # post-modal cursor block in draw_main can position the hardware
    # cursor AFTER the sidebar redraw — otherwise the unconditional
    # `curs_set(0)` for "modal active" would hide whatever we set here.
    if 0 <= cur_d - editor.scroll < text_h:
        editor._cursor_screen_y = text_y0 + (cur_d - editor.scroll)
        editor._cursor_screen_x = inner_x + min(cur_col, inner_w - 1)
    else:
        editor._cursor_screen_y = -1
        editor._cursor_screen_x = -1


def apply_commit_msg_editor_cursor(stdscr, state: State) -> bool:
    """Re-assert the hardware cursor position for the editor after
    every other panel (including the sidebar) has painted. Called as
    the last act of `draw_main`'s cursor block so the cursor lands on
    the textarea even though the rest of the main-screen logic would
    otherwise hide it ("modal active" defaults to `curs_set(0)`).

    Returns True iff the cursor was set, so the caller knows not to
    fall through to its own `curs_set(0)`."""
    editor = state.commit_msg_editor
    if editor is None:
        return False
    cy = getattr(editor, "_cursor_screen_y", -1)
    cx = getattr(editor, "_cursor_screen_x", -1)
    if cy < 0 or cx < 0:
        return False
    try:
        stdscr.move(cy, cx)
        curses.curs_set(1)
        return True
    except curses.error:
        return False
