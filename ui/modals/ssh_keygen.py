"""Create GitHub SSH keypair modal rendering."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.ssh_keygen import SshKeygenModal
from features.ssh_keygen.actions import handle_ssh_keygen_modal_key as handle_key
from features.ssh_keygen.projection import (
    CONFIRM_PROMPT,
    FIELD_BUTTON,
    FIELD_EMAIL,
    FIELD_PASSPHRASE,
    FIELD_PATH,
    LABEL_W,
    MODAL_W,
    N_FIELDS,
    PAD_BOTTOM,
    PAD_TOP,
    PAD_X,
    generate_blocked_reason,
    github_key_url,
    key_path_warning,
    passphrase_display,
)

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FG_DISABLED,
    PAIR_DLG_OK, PAIR_DLG_WARN,
)
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_TAB, KEY_UP_DOWN,
    Hint, render_hints,
)


def _hints(modal: SshKeygenModal) -> list:
    if modal.done:
        return [
            Hint(KEY_ENTER, "close"),
            Hint(KEY_ESC, "close"),
        ]
    if modal.working:
        return [Hint(KEY_ESC, "wait…")]
    if modal.preparing:
        return [Hint(KEY_ESC, "close")]
    if modal.confirm_empty_passphrase:
        return [
            Hint("y", "continue without passkey"),
            Hint("n", "set passkey"),
            Hint(KEY_ENTER, "no — set passkey"),
            Hint(KEY_ESC, "back to form"),
        ]
    if modal.edit_field:
        return [
            Hint("type", f"edit {modal.edit_field}"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, "save"),
            Hint(KEY_ESC, "cancel edit"),
        ]
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    if modal.selected == FIELD_EMAIL:
        hints.append(Hint(KEY_ENTER, "edit email"))
    elif modal.selected == FIELD_PATH:
        hints.append(Hint(KEY_ENTER, "edit path"))
    elif modal.selected == FIELD_PASSPHRASE:
        hints.append(Hint(KEY_ENTER, "edit passkey"))
    elif modal.selected == FIELD_BUTTON:
        if key_path_warning(modal):
            hints.append(Hint(KEY_ENTER, "path not empty"))
        elif blocked := generate_blocked_reason(modal):
            hints.append(Hint(KEY_ENTER, blocked))
        else:
            hints.append(Hint(KEY_ENTER, "generate key"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _draw_text_field(stdscr, line_y: int, inner_x: int, inner_w: int,
                     label: str, value: str, placeholder: str,
                     *, focused: bool, editing: bool, sb: int) -> None:
    label_part = f"{label}:".ljust(LABEL_W)
    display = value if value or not placeholder else placeholder
    if editing:
        display = f"{display}_"
    text = f"{label_part} {display}"
    attr = sb
    if focused:
        attr |= curses.A_REVERSE
        if not value and placeholder and not editing:
            attr |= curses.A_DIM
    safe_addstr(stdscr, line_y, inner_x,
                end_truncate(text, inner_w).ljust(inner_w), attr)


def _draw_passphrase_field(stdscr, line_y: int, inner_x: int, inner_w: int,
                           modal: SshKeygenModal, *, focused: bool,
                           editing: bool, sb: int) -> None:
    _draw_text_field(
        stdscr, line_y, inner_x, inner_w, "Passkey",
        passphrase_display(modal, editing=editing),
        "(optional)",
        focused=focused, editing=editing, sb=sb)


def _draw_generate_button(stdscr, line_y: int, inner_x: int, inner_w: int,
                          modal: SshKeygenModal) -> None:
    path_conflict = key_path_warning(modal)
    focused = (modal.selected == FIELD_BUTTON
               and not modal.confirm_empty_passphrase)

    if modal.working or modal.preparing:
        text = "  Loading..." if modal.preparing else "  Generating..."
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM
        if focused:
            attr |= curses.A_REVERSE
        safe_addstr(stdscr, line_y, inner_x,
                    end_truncate(text, inner_w).ljust(inner_w), attr)
        return

    if path_conflict:
        prefix = "→ " if focused else "  "
        main = f"{prefix}Generate key "
        suffix = "(path not empty)"
        grey = curses.color_pair(PAIR_DLG_FG_DISABLED) | curses.A_DIM
        orange = curses.color_pair(PAIR_DLG_WARN)
        if focused:
            grey |= curses.A_REVERSE
            orange |= curses.A_REVERSE
        main_show = end_truncate(main, inner_w)
        safe_addstr(stdscr, line_y, inner_x, main_show, grey)
        col = inner_x + len(main_show)
        suffix_show = end_truncate(suffix, max(0, inner_w - (col - inner_x)))
        if suffix_show:
            safe_addstr(stdscr, line_y, col, suffix_show, orange)
            col += len(suffix_show)
        pad = inner_w - (col - inner_x)
        if pad > 0:
            safe_addstr(stdscr, line_y, col, " " * pad, grey)
        return

    blocked = generate_blocked_reason(modal)
    prefix = "→ " if focused else "  "
    text = f"{prefix}Generate key"
    if blocked:
        attr = curses.color_pair(PAIR_DLG_FG_DISABLED) | curses.A_DIM
        if focused:
            attr |= curses.A_REVERSE
    elif focused:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD | curses.A_REVERSE
    else:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
    safe_addstr(stdscr, line_y, inner_x,
                end_truncate(text, inner_w).ljust(inner_w), attr)


def _title_lines(modal: SshKeygenModal, inner_w: int) -> list:
    if modal.done:
        return wrap_label_value(
            "SSH key created",
            str(modal.key_path or ""),
            inner_w,
        )
    return wrap_label_value(
        "Create GitHub SSH key",
        "ed25519 keypair for git@github.com",
        inner_w,
    )


def draw_ssh_keygen_modal(stdscr, state: State, sidebar_x: int) -> None:
    modal = state.ssh_keygen_modal
    if modal is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(1, MODAL_W - 2 * PAD_X)
    title_rows = _title_lines(modal, target_inner_w)

    if modal.done:
        body_rows = 6
    else:
        body_rows = N_FIELDS + 1
        if modal.confirm_empty_passphrase:
            body_rows += 1
    blank_after_title = 1
    hint_rows = 1
    desired_h = (
        PAD_TOP + len(title_rows) + blank_after_title
        + body_rows + hint_rows + PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + PAD_X
    inner_w = max(1, w - 2 * PAD_X)
    if inner_w != target_inner_w:
        title_rows = _title_lines(modal, inner_w)

    line = y + PAD_TOP
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    line += blank_after_title

    if modal.done:
        _draw_done_body(stdscr, modal, line, inner_x, inner_w, sb)
        render_hints(stdscr, y + h - PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(modal), attr=sb | curses.A_DIM)
        return

    line = _draw_form_body(stdscr, modal, line, inner_x, inner_w, sb)

    path_warn = key_path_warning(modal)
    if path_warn:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(path_warn, inner_w),
            curses.color_pair(PAIR_DLG_WARN))
    elif modal.error:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(modal.error, inner_w),
            curses.color_pair(PAIR_DLG_WARN))

    render_hints(stdscr, y + h - PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(modal), attr=sb | curses.A_DIM)


def _draw_done_body(
    stdscr,
    modal: SshKeygenModal,
    line: int,
    inner_x: int,
    inner_w: int,
    sb: int,
) -> None:
    safe_addstr(
        stdscr, line, inner_x,
        end_truncate("Add this public key at GitHub:", inner_w),
        sb | curses.A_DIM)
    line += 1
    safe_addstr(
        stdscr, line, inner_x,
        end_truncate(github_key_url(), inner_w),
        curses.color_pair(PAIR_DLG_CYAN))
    line += 2
    public_key = modal.public_key or "(missing .pub file)"
    safe_addstr(
        stdscr, line, inner_x,
        end_truncate(public_key, inner_w),
        curses.color_pair(PAIR_DLG_OK))
    line += 1
    if modal.error:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(modal.error, inner_w),
            curses.color_pair(PAIR_DLG_WARN))


def _draw_form_body(
    stdscr,
    modal: SshKeygenModal,
    line: int,
    inner_x: int,
    inner_w: int,
    sb: int,
) -> int:
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Email",
        modal.email, "you@example.com",
        focused=(modal.selected == FIELD_EMAIL and not modal.edit_field
                 and not modal.confirm_empty_passphrase),
        editing=(modal.edit_field == "email"),
        sb=sb)
    line += 1
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Key path",
        modal.key_path_text, modal.key_path_placeholder,
        focused=(modal.selected == FIELD_PATH and not modal.edit_field
                 and not modal.confirm_empty_passphrase),
        editing=(modal.edit_field == "path"),
        sb=sb)
    line += 1
    _draw_passphrase_field(
        stdscr, line, inner_x, inner_w, modal,
        focused=(modal.selected == FIELD_PASSPHRASE
                 and not modal.edit_field
                 and not modal.confirm_empty_passphrase),
        editing=(modal.edit_field == "passphrase"),
        sb=sb)
    line += 1

    _draw_generate_button(stdscr, line, inner_x, inner_w, modal)
    line += 1

    if modal.confirm_empty_passphrase:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(CONFIRM_PROMPT, inner_w),
            curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
        line += 1
    return line


def handle_ssh_keygen_modal_key(state: State, key: int) -> None:
    handle_key(state, key)
