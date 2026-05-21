"""Create GitHub SSH keypair — opened from the app menu."""
from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import Optional

from core.models import SshKeygenModal, State
from core.ssh import (
    create_ed25519_keypair,
    default_ed25519_path,
    ensure_ssh_agent,
    github_new_key_url,
    git_user_email,
    key_path_conflict_message,
    read_public_key,
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

_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2
_MODAL_W = 78
_LABEL_W = 14

_FIELD_EMAIL = 0
_FIELD_PATH = 1
_FIELD_PASSPHRASE = 2
_FIELD_BUTTON = 3
_N_FIELDS = 4

_CONFIRM_PROMPT = "Continue without setting a passkey? [y/N]"


def open_ssh_keygen_modal(state: State) -> None:
    email = git_user_email()
    default_path = default_ed25519_path()
    state.app_menu = None
    state.ssh_keygen_modal = SshKeygenModal(
        email=email,
        key_path_text=str(default_path),
    )


def _hints(modal: SshKeygenModal) -> list:
    if modal.done:
        return [
            Hint(KEY_ENTER, "close"),
            Hint(KEY_ESC, "close"),
        ]
    if modal.working:
        return [Hint(KEY_ESC, "wait…")]
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
    if modal.selected == _FIELD_EMAIL:
        hints.append(Hint(KEY_ENTER, "edit email"))
    elif modal.selected == _FIELD_PATH:
        hints.append(Hint(KEY_ENTER, "edit path"))
    elif modal.selected == _FIELD_PASSPHRASE:
        hints.append(Hint(KEY_ENTER, "edit passkey"))
    elif modal.selected == _FIELD_BUTTON:
        if key_path_conflict_message(modal.key_path_text):
            hints.append(Hint(KEY_ENTER, "path not empty"))
        elif blocked := _generate_blocked_reason(modal):
            hints.append(Hint(KEY_ENTER, blocked))
        else:
            hints.append(Hint(KEY_ENTER, "generate key"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _draw_text_field(stdscr, line_y: int, inner_x: int, inner_w: int,
                     label: str, value: str, placeholder: str,
                     *, focused: bool, editing: bool, sb: int) -> None:
    label_part = f"{label}:".ljust(_LABEL_W)
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


def _passphrase_display(modal: SshKeygenModal, *, editing: bool) -> str:
    if editing:
        return modal.passphrase
    if modal.passphrase:
        return "*" * len(modal.passphrase)
    return ""


def _draw_passphrase_field(stdscr, line_y: int, inner_x: int, inner_w: int,
                           modal: SshKeygenModal, *, focused: bool,
                           editing: bool, sb: int) -> None:
    _draw_text_field(
        stdscr, line_y, inner_x, inner_w, "Passkey",
        _passphrase_display(modal, editing=editing),
        "(optional)",
        focused=focused, editing=editing, sb=sb)


def _generate_blocked_reason(modal: SshKeygenModal) -> Optional[str]:
    """Non-empty when Generate must not run (existing key, bad path)."""
    conflict = key_path_conflict_message(modal.key_path_text)
    if conflict:
        return conflict
    text = modal.key_path_text.strip()
    if not text:
        return "key path is required"
    try:
        Path(text).expanduser()
    except (TypeError, ValueError):
        return "invalid key path"
    return None


def _draw_generate_button(stdscr, line_y: int, inner_x: int, inner_w: int,
                          modal: SshKeygenModal) -> None:
    """Generate row — grey + orange ``(path not empty)`` when blocked."""
    path_conflict = key_path_conflict_message(modal.key_path_text)
    focused = (modal.selected == _FIELD_BUTTON
               and not modal.confirm_empty_passphrase)

    if modal.working:
        text = "  Generating…"
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

    blocked = _generate_blocked_reason(modal)
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
    target_inner_w = max(1, _MODAL_W - 2 * _PAD_X)
    title_rows = _title_lines(modal, target_inner_w)

    if modal.done:
        body_rows = 6
    else:
        body_rows = _N_FIELDS + 1
        if modal.confirm_empty_passphrase:
            body_rows += 1
    blank_after_title = 1
    hint_rows = 1
    desired_h = (
        _PAD_TOP + len(title_rows) + blank_after_title
        + body_rows + hint_rows + _PAD_BOTTOM
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

    if modal.done:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate("Add this public key at GitHub:", inner_w),
            sb | curses.A_DIM)
        line += 1
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(github_new_key_url(), inner_w),
            curses.color_pair(PAIR_DLG_CYAN))
        line += 1
        line += 1
        pk = modal.public_key or "(missing .pub file)"
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(pk, inner_w),
            curses.color_pair(PAIR_DLG_OK))
        line += 1
        if modal.error:
            safe_addstr(
                stdscr, line, inner_x,
                end_truncate(modal.error, inner_w),
                curses.color_pair(PAIR_DLG_WARN))
            line += 1
        render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                     _hints(modal), attr=sb | curses.A_DIM)
        return

    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Email",
        modal.email, "you@example.com",
        focused=(modal.selected == _FIELD_EMAIL and not modal.edit_field
                 and not modal.confirm_empty_passphrase),
        editing=(modal.edit_field == "email"),
        sb=sb)
    line += 1
    _draw_text_field(
        stdscr, line, inner_x, inner_w, "Key path",
        modal.key_path_text, str(default_ed25519_path()),
        focused=(modal.selected == _FIELD_PATH and not modal.edit_field
                 and not modal.confirm_empty_passphrase),
        editing=(modal.edit_field == "path"),
        sb=sb)
    line += 1
    _draw_passphrase_field(
        stdscr, line, inner_x, inner_w, modal,
        focused=(modal.selected == _FIELD_PASSPHRASE
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
            end_truncate(_CONFIRM_PROMPT, inner_w),
            curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
        line += 1

    path_warn = key_path_conflict_message(modal.key_path_text)
    if path_warn:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(path_warn, inner_w),
            curses.color_pair(PAIR_DLG_WARN))
        line += 1
    elif modal.error:
        safe_addstr(
            stdscr, line, inner_x,
            end_truncate(modal.error, inner_w),
            curses.color_pair(PAIR_DLG_WARN))
        line += 1

    render_hints(stdscr, y + h - _PAD_BOTTOM - 1, inner_x, inner_w,
                 _hints(modal), attr=sb | curses.A_DIM)


def _kick_off_generate(state: State, modal: SshKeygenModal) -> None:
    blocked = _generate_blocked_reason(modal)
    if blocked:
        modal.error = blocked
        return
    email = modal.email.strip() or "idlegit"
    passphrase = modal.passphrase
    key_path = Path(modal.key_path_text.strip()).expanduser()

    modal.working = True
    modal.error = ""
    modal.confirm_empty_passphrase = False

    def worker() -> None:
        if state.auto_start_ssh_agent:
            ensure_ssh_agent(True)
        ok, msg = create_ed25519_keypair(
            key_path, email, passphrase=passphrase)
        pubkey, read_err = read_public_key(key_path)
        modal.working = False
        if ok:
            modal.done = True
            modal.key_path = key_path
            modal.public_key = pubkey or ""
            modal.error = msg
        else:
            modal.error = msg or "key generation failed"
        if read_err and modal.done:
            modal.error = read_err

    threading.Thread(target=worker, daemon=True).start()


def _request_generate(state: State, modal: SshKeygenModal) -> None:
    """Validate fields; empty passkey requires an explicit y/N confirm."""
    if modal.edit_field:
        return
    if not modal.passphrase:
        modal.confirm_empty_passphrase = True
        modal.selected = _FIELD_PASSPHRASE
        return
    _kick_off_generate(state, modal)


def _cancel_empty_passphrase_confirm(modal: SshKeygenModal) -> None:
    modal.confirm_empty_passphrase = False
    modal.selected = _FIELD_PASSPHRASE
    modal.edit_pre_value = modal.passphrase
    modal.edit_field = "passphrase"


def _handle_empty_passphrase_confirm(state: State, key: int) -> None:
    modal = state.ssh_keygen_modal
    if modal is None:
        return
    if key in (ord("y"), ord("Y")):
        _kick_off_generate(state, modal)
        return
    if key in (ord("n"), ord("N"), 10, 13, curses.KEY_ENTER):
        _cancel_empty_passphrase_confirm(modal)
        return
    if key == 27:
        modal.confirm_empty_passphrase = False
        return


def handle_ssh_keygen_modal_key(state: State, key: int) -> None:
    modal = state.ssh_keygen_modal
    if modal is None:
        return

    if modal.done:
        if key in (27, 9, 10, 13, curses.KEY_ENTER):
            state.ssh_keygen_modal = None
        return

    if modal.working:
        return

    if modal.confirm_empty_passphrase:
        _handle_empty_passphrase_confirm(state, key)
        return

    if key in (27, 9):
        state.ssh_keygen_modal = None
        return

    if modal.edit_field:
        if key in (10, 13, curses.KEY_ENTER):
            if modal.edit_field == "passphrase" and not modal.passphrase:
                modal.edit_field = ""
                modal.confirm_empty_passphrase = True
                modal.selected = _FIELD_PASSPHRASE
                return
            modal.edit_field = ""
            return
        if key == 27:
            if modal.edit_field == "email":
                modal.email = modal.edit_pre_value
            elif modal.edit_field == "path":
                modal.key_path_text = modal.edit_pre_value
            elif modal.edit_field == "passphrase":
                modal.passphrase = modal.edit_pre_value
            modal.edit_field = ""
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if modal.edit_field == "email":
                modal.email = modal.email[:-1]
            elif modal.edit_field == "path":
                modal.key_path_text = modal.key_path_text[:-1]
                if key_path_conflict_message(modal.key_path_text):
                    modal.error = ""
            elif modal.edit_field == "passphrase":
                modal.passphrase = modal.passphrase[:-1]
            return
        if 32 <= key < 127:
            ch = chr(key)
            if modal.edit_field == "email":
                modal.email += ch
            elif modal.edit_field == "path":
                modal.key_path_text += ch
                if key_path_conflict_message(modal.key_path_text):
                    modal.error = ""
            elif modal.edit_field == "passphrase":
                modal.passphrase += ch
        return

    if key == curses.KEY_UP:
        modal.selected = max(0, modal.selected - 1)
        return
    if key == curses.KEY_DOWN:
        modal.selected = min(_N_FIELDS - 1, modal.selected + 1)
        return

    if key in (10, 13, curses.KEY_ENTER):
        if modal.selected == _FIELD_EMAIL:
            modal.edit_pre_value = modal.email
            modal.edit_field = "email"
            return
        if modal.selected == _FIELD_PATH:
            modal.edit_pre_value = modal.key_path_text
            modal.edit_field = "path"
            return
        if modal.selected == _FIELD_PASSPHRASE:
            modal.edit_pre_value = modal.passphrase
            modal.edit_field = "passphrase"
            return
        if modal.selected == _FIELD_BUTTON:
            if not _generate_blocked_reason(modal):
                _request_generate(state, modal)
        return
